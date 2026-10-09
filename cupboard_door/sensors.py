"""Inputs that trigger the door: the book lever and the temperature sensor."""

from __future__ import annotations

import glob
import logging
import os
import re
import threading
import time
from collections import deque
from typing import Callable, Optional, Protocol

from .config import ConfigStore

log = logging.getLogger(__name__)


# =========================================================== book lever ====
class Debouncer:
    """Turns a noisy switch reading into a clean one.

    The clean (stable) value only changes once the raw reading has held the
    new value for ``debounce_s`` without a break. Shorter blips are ignored.
    """

    def __init__(self, initial: bool) -> None:
        self.stable = initial
        self._change_started: Optional[float] = None

    def update(self, raw: bool, now: float, debounce_s: float) -> bool:
        """Feed one reading. Returns True if the stable value just changed."""
        if raw == self.stable:
            self._change_started = None  # the blip ended, start over
            return False
        if self._change_started is None:
            self._change_started = now
        if now - self._change_started >= debounce_s:
            self.stable = raw
            self._change_started = None
            return True
        return False


class BookPullDetector:
    """Spots a book pull: the lever going from pressed to released.

    One pull gives one trigger. The trigger fires on the release edge only,
    so leaving the book out does not repeat it, and a lockout ignores any
    further pulls for ``lockout_s`` seconds after a trigger.
    """

    def __init__(self, initially_pressed: bool) -> None:
        self.debouncer = Debouncer(initially_pressed)
        self._last_trigger: Optional[float] = None

    def update(self, raw_pressed: bool, now: float, debounce_s: float, lockout_s: float) -> bool:
        if not self.debouncer.update(raw_pressed, now, debounce_s):
            return False
        if self.debouncer.stable:
            return False  # book put back, nothing to do
        if self._last_trigger is not None and now - self._last_trigger < lockout_s:
            log.info("Book pull ignored (lockout, %.1f s since the last one)", now - self._last_trigger)
            return False
        self._last_trigger = now
        return True


class BookMonitor(threading.Thread):
    """Polls the book lever every few milliseconds and calls ``on_pull``."""

    POLL_S = 0.005

    def __init__(
        self,
        read_pressed: Callable[[], bool],
        store: ConfigStore,
        on_pull: Callable[[], None],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(name="book-monitor", daemon=True)
        self._read = read_pressed
        self._store = store
        self._on_pull = on_pull
        self._clock = clock
        self._stop_event = threading.Event()
        self.detector = BookPullDetector(read_pressed())
        self.heartbeat = clock()

    def poll_once(self) -> None:
        now = self._clock()
        self.heartbeat = now
        cfg = self._store.config
        if self.detector.update(self._read(), now, cfg.book_debounce_ms / 1000.0, cfg.book_lockout_s):
            log.info("Book pulled")
            self._on_pull()

    def run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.poll_once()
            except Exception:
                log.exception("Book monitor error")
                self._stop_event.wait(1.0)
            self._stop_event.wait(self.POLL_S)

    def stop(self) -> None:
        self._stop_event.set()


# ========================================================== temperature ====
class SensorError(Exception):
    """A temperature reading failed (missing sensor, CRC failure, bad data)."""


def parse_w1_slave(text: str) -> float:
    """Read degrees C from the contents of a DS18B20 w1_slave file.

    The file looks like this:
        72 01 4b 46 7f ff 0e 10 57 : crc=57 YES
        72 01 4b 46 7f ff 0e 10 57 t=23125
    The first line must end in YES (the checksum passed). t= is in 1/1000 C.
    """
    lines = text.strip().splitlines()
    if len(lines) < 2:
        raise SensorError("short read from sensor")
    if not lines[0].strip().endswith("YES"):
        raise SensorError("CRC check failed")
    match = re.search(r"t=(-?\d+)", lines[1])
    if not match:
        raise SensorError("no temperature in sensor data")
    value = int(match.group(1)) / 1000.0
    if value == 85.0:
        # 85.000 is the value the chip holds after a power glitch, not a real reading.
        raise SensorError("sensor returned its power-on value (85 C)")
    if not -55.0 <= value <= 125.0:
        raise SensorError(f"reading out of range: {value}")
    return value


class TemperatureSensor(Protocol):
    name: str

    def available(self) -> bool: ...

    def read(self) -> float: ...


class DS18B20Sensor:
    """A DS18B20 on the Linux 1-Wire bus (/sys/bus/w1/devices/28-*/w1_slave)."""

    def __init__(self, devices_dir: str = "/sys/bus/w1/devices") -> None:
        self.devices_dir = devices_dir
        self._path: Optional[str] = None
        self.name = ""

    def available(self) -> bool:
        if self._path and os.path.exists(self._path):
            return True
        found = sorted(glob.glob(os.path.join(self.devices_dir, "28-*", "w1_slave")))
        self._path = found[0] if found else None
        self.name = os.path.basename(os.path.dirname(self._path)) if self._path else ""
        return self._path is not None

    def read(self) -> float:
        if not self._path:
            raise SensorError("sensor not found")
        try:
            with open(self._path, encoding="ascii", errors="replace") as handle:
                text = handle.read()
        except OSError as exc:
            self._path = None  # look for it again next time
            raise SensorError(f"could not read sensor: {exc}") from exc
        return parse_w1_slave(text)


class FakeSensor:
    """Pretend sensor for --simulate and the tests."""

    def __init__(self, temperature: float = 21.0, missing: bool = False) -> None:
        self.temperature = temperature
        self.missing = missing
        self.fail_reads = 0  # number of upcoming reads that fail a CRC check
        self.name = "simulated"

    def available(self) -> bool:
        return not self.missing

    def read(self) -> float:
        if self.missing:
            raise SensorError("sensor not found")
        if self.fail_reads > 0:
            self.fail_reads -= 1
            raise SensorError("CRC check failed")
        return self.temperature


class TempWindow:
    """Remembers recent readings and spots a fast rise or fall.

    A trigger happens when the newest reading is at least ``delta_c`` above
    the lowest, or below the highest, reading taken in the last ``window_s``
    seconds.
    """

    def __init__(self) -> None:
        self.samples: deque[tuple[float, float]] = deque()

    def add(self, now: float, temp_c: float, delta_c: float, window_s: float) -> Optional[float]:
        """Add a reading. Returns the change in C (+ rise, - fall) if it triggers."""
        self.samples.append((now, temp_c))
        while self.samples and now - self.samples[0][0] > window_s:
            self.samples.popleft()
        temps = [t for _, t in self.samples]
        lowest, highest = min(temps), max(temps)
        tiny = 1e-9  # so 21.0 -> 24.0 counts as exactly 3.0
        if temp_c - lowest >= delta_c - tiny:
            return round(temp_c - lowest, 3)
        if highest - temp_c >= delta_c - tiny:
            return round(temp_c - highest, 3)
        return None

    def clear(self) -> None:
        self.samples.clear()


class TemperatureMonitor(threading.Thread):
    """Reads the sensor on a timer, keeps the window and calls ``on_trigger``.

    At start-up it waits up to ``sensor_wait_s`` for the sensor to appear,
    checking every 2 s. After that it carries on without it and checks again
    every 30 s, so a sensor plugged in later is picked up. Read errors are
    logged and skipped; they never stop the program.
    """

    FIND_RETRY_S = 2.0
    MISSING_RETRY_S = 30.0
    FAILS_BEFORE_OFFLINE = 3

    def __init__(
        self,
        sensor: TemperatureSensor,
        store: ConfigStore,
        on_trigger: Optional[Callable[[float, float], None]] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(name="temperature-monitor", daemon=True)
        self.sensor = sensor
        self._store = store
        self._on_trigger = on_trigger
        self._clock = clock
        self._stop_event = threading.Event()
        self.window = TempWindow()
        self.started_at = clock()
        self.heartbeat = self.started_at
        self.temperature: Optional[float] = None
        self.sensor_ok = False
        self.sensor_found = False
        self.consecutive_errors = 0
        self.last_error = ""
        self._said_waiting = False
        self._said_missing = False

    def poll_once(self) -> float:
        """Do one round of work. Returns how long to wait before the next one."""
        now = self._clock()
        self.heartbeat = now
        cfg = self._store.config

        if not self.sensor.available():
            if self.sensor_found:
                log.warning("Temperature sensor has disappeared")
            self.sensor_found = False
            self.sensor_ok = False
            self.temperature = None
            if now - self.started_at < self._store.config.sensor_wait_s:
                if not self._said_waiting:
                    log.info("Waiting up to %d s for the temperature sensor", cfg.sensor_wait_s)
                    self._said_waiting = True
                return self.FIND_RETRY_S
            if not self._said_missing:
                log.warning(
                    "Temperature sensor not found. Carrying on without it and checking "
                    "again every %d s. The temperature trigger is off until it appears.",
                    self.MISSING_RETRY_S,
                )
                self._said_missing = True
            self.last_error = "sensor not found"
            return self.MISSING_RETRY_S

        if not self.sensor_found:
            log.info("Temperature sensor found: %s", self.sensor.name)
            self.sensor_found = True
            self._said_missing = False

        try:
            temp = self.sensor.read()
        except SensorError as exc:
            self.consecutive_errors += 1
            self.last_error = str(exc)
            # Log the first failure and then only now and then, to keep the journal small.
            if self.consecutive_errors == 1 or self.consecutive_errors % 30 == 0:
                log.warning("Temperature read failed (%s), %d in a row", exc, self.consecutive_errors)
            if self.consecutive_errors >= self.FAILS_BEFORE_OFFLINE:
                self.sensor_ok = False
                self.temperature = None
            return cfg.temp_poll_s

        if self.consecutive_errors:
            log.info("Temperature reads working again")
        self.consecutive_errors = 0
        self.last_error = ""
        self.sensor_ok = True
        self.temperature = temp

        change = self.window.add(now, temp, cfg.temp_delta_c, cfg.temp_window_s)
        if change is not None:
            self.window.clear()  # start a fresh window so one change gives one trigger
            log.info(
                "Temperature %s by %.1f C within %d s (now %.1f C)",
                "rose" if change > 0 else "fell", abs(change), cfg.temp_window_s, temp,
            )
            if self._on_trigger:
                self._on_trigger(change, temp)
        return cfg.temp_poll_s

    def run(self) -> None:
        while not self._stop_event.is_set():
            try:
                delay = self.poll_once()
            except Exception:
                log.exception("Temperature monitor error")
                delay = 10.0
            self._stop_event.wait(delay)

    def stop(self) -> None:
        self._stop_event.set()

    def status(self) -> dict[str, object]:
        return {
            "temperature_c": None if self.temperature is None else round(self.temperature, 2),
            "sensor_ok": self.sensor_ok,
            "sensor_found": self.sensor_found,
            "sensor_name": self.sensor.name if self.sensor_found else "",
            "sensor_error": self.last_error,
        }
