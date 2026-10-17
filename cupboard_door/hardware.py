"""GPIO access for the A4988 driver and the two switches.

Everything that touches a pin lives here, so the rest of the program can run
against real pins (gpiozero + lgpio on the Pi 5) or mock pins (gpiozero's
MockFactory) without any changes.
"""

from __future__ import annotations

import logging
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


def make_real_factory(chip: Optional[int] = None) -> Any:
    """The lgpio pin factory, which works on the Pi 5 (RPi.GPIO does not)."""
    from gpiozero.pins.lgpio import LGPIOFactory

    return LGPIOFactory() if chip is None else LGPIOFactory(chip=chip)


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
