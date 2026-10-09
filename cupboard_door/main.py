"""Start-up, shutdown, systemd notifications and the command line."""

from __future__ import annotations

import argparse
import logging
import os
import signal
import socket
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any, Optional

from .config import ConfigStore
from .door import Door
from .hardware import Hardware, HardwareError, SimHardware, make_real_factory
from .sensors import BookMonitor, DS18B20Sensor, FakeSensor, TemperatureMonitor, TemperatureSensor

log = logging.getLogger("cupboard_door")

DEFAULT_CONFIG = os.environ.get("CUPBOARD_DOOR_CONFIG", "door_config.json")


# ------------------------------------------------------------------ logging
class JournalFormatter(logging.Formatter):
    """Adds a <N> priority prefix that journald understands, so that
    ``journalctl -p warning`` shows only warnings and errors. journald adds
    its own timestamps."""

    PRIORITY = {logging.DEBUG: 7, logging.INFO: 6, logging.WARNING: 4, logging.ERROR: 3, logging.CRITICAL: 2}

    def format(self, record: logging.LogRecord) -> str:
        prefix = f"<{self.PRIORITY.get(record.levelno, 6)}>"
        return prefix + super().format(record).replace("\n", "\n" + prefix)


def setup_logging(level: str = "INFO") -> None:
    # Logs go to stdout/stderr only. Under systemd that means journald, and
    # nothing is written to log files on the SD card by this program.
    handler = logging.StreamHandler(sys.stderr)
    if os.environ.get("JOURNAL_STREAM"):
        handler.setFormatter(JournalFormatter("%(name)s: %(message)s"))
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(getattr(logging, level.upper(), logging.INFO))


# ------------------------------------------------------------------ systemd
def sd_notify(message: str) -> bool:
    """Send a message to systemd (READY=1, WATCHDOG=1, ...). Does nothing when
    not started by systemd. Written by hand to avoid an extra dependency."""
    address = os.environ.get("NOTIFY_SOCKET")
    if not address:
        return False
    if address.startswith("@"):
        address = "\0" + address[1:]  # abstract socket
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(address)
            sock.sendall(message.encode())
        return True
    except OSError as exc:
        log.debug("sd_notify failed: %s", exc)
        return False


def watchdog_interval() -> Optional[float]:
    """How often to ping the systemd watchdog (half of WatchdogSec), or None."""
    usec = os.environ.get("WATCHDOG_USEC")
    pid = os.environ.get("WATCHDOG_PID")
    if not usec or (pid and pid != str(os.getpid())):
        return None
    try:
        return max(1.0, int(usec) / 1_000_000 / 2)
    except ValueError:
        return None


# ------------------------------------------------------------------- system
@dataclass
class System:
    store: ConfigStore
    hw: Hardware
    door: Door
    temp_monitor: TemperatureMonitor
    book_monitor: BookMonitor
    simulate: bool = False
    sim_hw: Optional[SimHardware] = None
    fake_sensor: Optional[FakeSensor] = None
    web: Optional[Any] = None

    def start(self, port: Optional[int] = None, web: bool = True) -> None:
        self.book_monitor.start()
        self.temp_monitor.start()
        if web:
            from .web import WebServer, create_app  # Flask is imported only after the pins are safe

            self.web = WebServer(create_app(self), port or self.store.config.web_port)
            self.web.start()

    def stop(self) -> None:
        self.book_monitor.stop()
        self.temp_monitor.stop()
        self.door.shutdown()
        if self.web is not None:
            self.web.stop()
        self.hw.close()

    def health_problems(self) -> list[str]:
        """Reasons not to ping the watchdog. Empty means all is well."""
        problems = []
        now = time.monotonic()
        threads = [self.book_monitor, self.temp_monitor] + ([self.web] if self.web else [])
        for thread in threads:
            if not thread.is_alive():
                problems.append(f"{thread.name} thread has stopped")
        if now - self.book_monitor.heartbeat > 10:
            problems.append("book monitor is not responding")
        temp_limit = max(self.store.config.temp_poll_s, TemperatureMonitor.MISSING_RETRY_S) + 60
        if now - self.temp_monitor.heartbeat > temp_limit:
            problems.append("temperature monitor is not responding")
        if self.door.is_moving and self.door.seconds_since_last_step() > 10:
            problems.append("a door move has stalled")
        return problems

    def status_line(self) -> str:
        s = self.door.status()
        temp = self.temp_monitor.temperature
        text = f"Door {s['state']}"
        if s["fault"]:
            text += " (FAULT)"
        text += ", " + ("no temperature" if temp is None else f"{temp:.1f} C")
        return text


def build_system(
    store: ConfigStore,
    simulate: bool = False,
    sim_start_open: bool = False,
    sensor: Optional[TemperatureSensor] = None,
) -> System:
    """Set up the pins (EN first, driver off), read the start-up door state and
    create the monitors. Threads are not started here."""
    cfg = store.config
    sim_hw: Optional[SimHardware] = None
    fake: Optional[FakeSensor] = None
    if simulate:
        # Start half open to act out a power cut in the middle of a move.
        start = cfg.steps_to_open // 2 if sim_start_open else 0
        sim_hw = SimHardware(cfg.pins, physical_open_level=cfg.open_dir, start_position=start)
        hw: Hardware = sim_hw
        if sensor is None:
            fake = FakeSensor()
            sensor = fake
        elif isinstance(sensor, FakeSensor):
            fake = sensor
    else:
        hw = Hardware(cfg.pins, make_real_factory(cfg.gpio_chip))
        if sensor is None:
            sensor = DS18B20Sensor(cfg.w1_devices_dir)

    door = Door(hw, store)
    door.initialise_state()

    def on_temperature(change: float, temp: float) -> None:
        door.request("auto_open", source=f"Temperature trigger ({change:+.1f} C)")

    def on_book_pull() -> None:
        door.request("toggle", source="Book pull")

    return System(
        store=store,
        hw=hw,
        door=door,
        temp_monitor=TemperatureMonitor(sensor, store, on_trigger=on_temperature),
        book_monitor=BookMonitor(hw.book_pressed, store, on_pull=on_book_pull),
        simulate=simulate,
        sim_hw=sim_hw,
        fake_sensor=fake,
    )


def supervise(system: System, stop: threading.Event) -> None:
    """Main loop: keep systemd's watchdog fed while every part is healthy."""
    interval = watchdog_interval()
    if interval:
        log.info("systemd watchdog is on, checking in every %.0f s", interval)
    last_ping = 0.0
    last_status = ""
    while not stop.wait(1.0):
        line = system.status_line()
        if line != last_status:
            sd_notify("STATUS=" + line)
            last_status = line
        now = time.monotonic()
        if interval and now - last_ping >= interval:
            problems = system.health_problems()
            if problems:
                # Stop feeding the watchdog; systemd will restart us.
                log.error("Health check failed, systemd will restart the service: %s", "; ".join(problems))
            else:
                sd_notify("WATCHDOG=1")
                last_ping = now


# ---------------------------------------------------------------------- cli
def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="cupboard-door", description="Automated cupboard door controller")
    parser.add_argument("--config", default=DEFAULT_CONFIG, help="path to door_config.json (default: %(default)s)")
    parser.add_argument("--simulate", action="store_true", help="mock GPIO and a fake temperature sensor")
    parser.add_argument("--test", action="store_true", help="terminal test mode for tuning")
    parser.add_argument("--port", type=int, default=None, help="web port (default: from config, 8080)")
    parser.add_argument(
        "--sim-start-open", action="store_true",
        help="with --simulate: start with the door part open, as after a power cut mid-move",
    )
    parser.add_argument("--log-level", default="INFO", help="DEBUG, INFO, WARNING or ERROR")
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    setup_logging(args.log_level)
    store = ConfigStore(args.config)
    store.load()

    if args.test:
        from .testmode import run_test_mode

        return run_test_mode(store, simulate=args.simulate, sim_start_open=args.sim_start_open)

    stop = threading.Event()

    def on_signal(signum: int, _frame: Any) -> None:
        log.info("Received %s, shutting down", signal.Signals(signum).name)
        stop.set()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    try:
        system = build_system(store, simulate=args.simulate, sim_start_open=args.sim_start_open)
    except HardwareError as exc:
        log.error("%s", exc)
        return 1

    log.info("Cupboard door controller started%s", " (SIMULATION)" if args.simulate else "")
    try:
        system.start(port=args.port)
        sd_notify("READY=1")
        supervise(system, stop)
    finally:
        sd_notify("STOPPING=1")
        system.stop()
        log.info("Motor disabled and GPIO released. Goodbye.")
    return 0
