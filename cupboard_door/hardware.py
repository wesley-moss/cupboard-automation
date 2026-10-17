"""GPIO access for the A4988 driver and the two switches.

Everything that touches a pin lives here, so the rest of the program can run
against real pins (gpiozero + lgpio on the Pi 5) or mock pins (gpiozero's
MockFactory) without any changes.
"""

from __future__ import annotations

import glob
import grp
import logging
import os
import pwd
import stat
import threading
import time
from typing import Any, Optional

log = logging.getLogger(__name__)

STEP_PULSE_S = 3e-6  # A4988 needs at least 1 us high on STEP; 3 us gives margin


class HardwareError(RuntimeError):
    """The GPIO pins could not be set up."""


def busy_wait(seconds: float) -> None:
    end = time.perf_counter() + seconds
    while time.perf_counter() < end:
        pass


def _is_header_chip(label: str) -> bool:
    """True for the chip wired to the 40-pin header: "pinctrl-rp1" on a Pi 5,
    "pinctrl-bcm2711" / "pinctrl-bcm2835" on older Pis. The Pi 5 also has
    other GPIO chips (for example "gpio-brcmstb@...") that must not be used."""
    return "rp1" in label or label.startswith("pinctrl-bcm")


def _chip_numbers() -> list[int]:
    numbers = set()
    for path in glob.glob("/dev/gpiochip*"):
        suffix = path[len("/dev/gpiochip"):]
        if suffix.isdigit():
            numbers.add(int(suffix))
    return sorted(numbers)


def gpio_diagnostics() -> str:
    """Who we are and who may open each /dev/gpiochip, for error messages."""
    try:
        user = pwd.getpwuid(os.getuid()).pw_name
    except KeyError:
        user = str(os.getuid())
    names = []
    for gid in sorted(set(os.getgroups()) | {os.getgid()}):
        try:
            names.append(grp.getgrgid(gid).gr_name)
        except KeyError:
            names.append(str(gid))
    lines = [f"running as user '{user}', groups: {', '.join(names)}"]
    paths = sorted(glob.glob("/dev/gpiochip*"))
    if not paths:
        lines.append("no /dev/gpiochip* devices exist")
    for path in paths:
        try:
            st = os.stat(path)
            group = grp.getgrgid(st.st_gid).gr_name
            owner = pwd.getpwuid(st.st_uid).pw_name
            target = f" -> {os.path.realpath(path)}" if os.path.islink(path) else ""
            can = "yes" if os.access(path, os.R_OK | os.W_OK) else "NO"
            lines.append(
                f"{path}{target}: owner {owner}, group {group}, mode {stat.filemode(st.st_mode)}, "
                f"we can open it: {can}"
            )
        except (OSError, KeyError) as exc:
            lines.append(f"{path}: {exc}")
    return "\n  ".join(lines)


def find_header_chip(lgpio_mod: Any = None) -> int:
    """Find the gpiochip number for the 40-pin header by its label.

    The number differs between Pi 5 kernels (4 on early ones, 0 on later
    ones), so we look for the label instead of guessing.
    """
    if lgpio_mod is None:
        import lgpio as lgpio_mod
    problems = []
    for number in _chip_numbers():
        try:
            handle = lgpio_mod.gpiochip_open(number)
        except Exception as exc:
            problems.append(f"gpiochip{number}: {exc}")
            continue
        try:
            info = lgpio_mod.gpio_get_chip_info(handle)
            label = str(info[3]) if isinstance(info, (list, tuple)) and len(info) > 3 else ""
        finally:
            lgpio_mod.gpiochip_close(handle)
        if _is_header_chip(label):
            log.info("Using /dev/gpiochip%d (%s) for the GPIO header", number, label)
            return number
        problems.append(f"gpiochip{number}: label '{label}' is not the GPIO header")
    raise HardwareError(
        "Could not open the GPIO header chip.\n  "
        + ("\n  ".join(problems) or "no gpiochips found")
        + "\n  " + gpio_diagnostics()
        + "\n  If 'we can open it' says NO, the user needs to be in the group shown, "
        "or re-run install.sh, which adds a rule giving the gpio group access."
    )


def make_real_factory(chip: Optional[int] = None) -> Any:
    """The lgpio pin factory, which works on the Pi 5 (RPi.GPIO does not)."""
    from gpiozero.pins.lgpio import LGPIOFactory

    if chip is None:
        chip = find_header_chip()
    try:
        return LGPIOFactory(chip=chip)
    except Exception as exc:
        raise HardwareError(
            f"Could not open /dev/gpiochip{chip} ({exc}).\n  {gpio_diagnostics()}"
        ) from exc


class Hardware:
    """STEP, DIR and EN outputs plus the door-closed and book lever inputs."""

    def __init__(self, pins: dict[str, int], pin_factory: Any = None) -> None:
        from gpiozero import DigitalInputDevice, DigitalOutputDevice

        self._devices: list[Any] = []
        self._closed = False
        try:
            # EN is set up first, and is driven high (driver OFF) the instant it
            # becomes an output. Until then the external 10k pull-up holds it high.
            self._en = self._keep(
                DigitalOutputDevice(pins["en"], initial_value=True, pin_factory=pin_factory)
            )
            self._step = self._keep(
                DigitalOutputDevice(pins["step"], initial_value=False, pin_factory=pin_factory)
            )
            self._dir = self._keep(
                DigitalOutputDevice(pins["dir"], initial_value=False, pin_factory=pin_factory)
            )
            # Switches: COM to GND, NO to the pin, internal pull-up. Pressed reads
            # low, which gpiozero reports as "active" when pull_up=True.
            self._door_switch = self._keep(
                DigitalInputDevice(pins["door_closed"], pull_up=True, pin_factory=pin_factory)
            )
            self._book_switch = self._keep(
                DigitalInputDevice(pins["book_lever"], pull_up=True, pin_factory=pin_factory)
            )
        except Exception as exc:
            self.close()
            raise HardwareError(
                f"Could not set up the GPIO pins ({type(exc).__name__}: {exc}). "
                "Check that nothing else is using these pins (another script, test mode, "
                "or the service while you run it by hand: sudo systemctl stop cupboard-door), "
                "and that the user running this is in the gpio group (groups <user>)."
            ) from exc
        # Talk to the STEP pin directly to keep the per-step overhead small.
        self._step_pin = self._step.pin
        self._dir_level = 0

    def _keep(self, device: Any) -> Any:
        self._devices.append(device)
        return device

    # Motor driver -----------------------------------------------------------
    def enable_driver(self) -> None:
        self._en.off()  # /ENABLE low = driver on

    def disable_driver(self) -> None:
        if not self._closed:  # after close() the pin is released and the 10k pull-up holds EN high
            self._en.on()  # /ENABLE high = driver off, motor cold

    @property
    def driver_enabled(self) -> bool:
        return False if self._closed else not self._en.value

    def set_direction(self, level: int) -> None:
        self._dir_level = 1 if level else 0
        self._dir.value = self._dir_level
        busy_wait(2e-6)  # A4988 wants DIR stable 200 ns before a STEP edge

    def step_pulse(self) -> None:
        self._step_pin.state = True
        busy_wait(STEP_PULSE_S)
        self._step_pin.state = False

    # Switches ---------------------------------------------------------------
    def door_closed_pressed(self) -> bool:
        return bool(self._door_switch.is_active)

    def book_pressed(self) -> bool:
        return bool(self._book_switch.is_active)

    # Shutdown ---------------------------------------------------------------
    def close(self) -> None:
        """Turn the driver off, then release every pin. Safe to call twice."""
        if self._closed:
            return
        self._closed = True
        try:
            if self._devices:
                self._devices[0].on()  # EN high before anything else
        except Exception:
            pass
        for device in reversed(self._devices):
            try:
                device.close()
            except Exception:
                pass
        self._devices = []


class SimHardware(Hardware):
    """Mock pins plus a pretend door, so everything runs on a laptop.

    The pretend door counts steps from fully closed. Its closed switch is
    pressed at position 0 or below, and it cannot be driven past a hard stop
    a few steps beyond closed (like a real door frame). ``physical_open_level``
    is the DIR level that really opens this pretend door; if open_dir in the
    config does not match it, the door goes the wrong way, just like real
    hardware wired backwards.
    """

    HARD_STOP = -5

    def __init__(
        self,
        pins: dict[str, int],
        physical_open_level: int = 1,
        start_position: int = 0,
        switch_broken: bool = False,
    ) -> None:
        from gpiozero.pins.mock import MockFactory

        self._factory = MockFactory()
        super().__init__(pins, pin_factory=self._factory)
        self._pins = pins
        self._sim_lock = threading.Lock()
        self.physical_open_level = physical_open_level
        self.position = start_position
        self.switch_broken = switch_broken  # never reads pressed
        self.switch_stuck = False  # always reads pressed
        self.steps_while_disabled = 0
        self.open_steps = 0   # steps made in the opening direction
        self.close_steps = 0  # steps made in the closing direction
        self._update_switch()
        self.set_book(True)  # a book starts resting on the lever

    def _mock_pin(self, name: str) -> Any:
        return self._factory.pin(self._pins[name])

    def _update_switch(self) -> None:
        pressed = (self.position <= 0 and not self.switch_broken) or self.switch_stuck
        pin = self._mock_pin("door_closed")
        if pressed:
            pin.drive_low()
        else:
            pin.drive_high()

    def step_pulse(self) -> None:
        super().step_pulse()
        with self._sim_lock:
            if not self.driver_enabled:
                self.steps_while_disabled += 1  # would be a bug: motor is not powered
                return
            if self._dir_level == self.physical_open_level:
                self.open_steps += 1
                self.position += 1
            else:
                self.close_steps += 1
                self.position = max(self.HARD_STOP, self.position - 1)
            self._update_switch()

    def set_switch_broken(self, broken: bool) -> None:
        with self._sim_lock:
            self.switch_broken = broken
            self._update_switch()

    def set_switch_stuck(self, stuck: bool) -> None:
        with self._sim_lock:
            self.switch_stuck = stuck
            self._update_switch()

    def set_book(self, pressed: bool) -> None:
        if self._closed:
            return  # a late timer after shutdown
        pin = self._mock_pin("book_lever")
        if pressed:
            pin.drive_low()
        else:
            pin.drive_high()

    def pull_book(self, hold_s: float = 0.5) -> None:
        """Pretend someone pulls the book, then puts it back after hold_s."""
        self.set_book(False)
        timer = threading.Timer(hold_s, self.set_book, args=(True,))
        timer.daemon = True
        timer.start()
