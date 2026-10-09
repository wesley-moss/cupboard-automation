"""Door logic: state, position, the move lock, the closed switch and faults."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Optional

from .config import ConfigStore
from .hardware import Hardware, busy_wait
from .motor import Motor, step_speeds

log = logging.getLogger(__name__)


class DoorState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    UNKNOWN = "unknown"
    OPENING = "opening"
    CLOSING = "closing"
    JOGGING = "jogging"


MOVING_STATES = (DoorState.OPENING, DoorState.CLOSING, DoorState.JOGGING)


@dataclass
class Result:
    ok: bool
    message: str
    started: bool = False  # True when a move was started

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "message": self.message, "started": self.started}


class Door:
    """Owns the motor. Every move, whatever asked for it, goes through
    ``request`` and must take ``_move_lock`` first, so two moves can never
    run at the same time. A request that arrives mid-move is refused."""

    def __init__(self, hw: Hardware, store: ConfigStore, motor: Optional[Motor] = None) -> None:
        self.hw = hw
        self.store = store
        self.motor = motor or Motor(hw)
        self._move_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._abort = threading.Event()
        self._shutting_down = False
        self._move_thread: Optional[threading.Thread] = None
        self.state = DoorState.UNKNOWN
        self.position: Optional[int] = None  # steps from fully closed, None if unknown
        self.fault = False
        self.fault_reason = ""
        self.last_event = "Starting"
        self.last_event_time = time.time()

    # ------------------------------------------------------------------ state
    def _event(self, message: str, level: int = logging.INFO) -> None:
        with self._state_lock:
            self.last_event = message
            self.last_event_time = time.time()
        log.log(level, message)

    def _set(self, state: DoorState, position: Optional[int]) -> None:
        with self._state_lock:
            self.state = state
            self.position = position

    @property
    def is_moving(self) -> bool:
        return self.state in MOVING_STATES

    def initialise_state(self) -> DoorState:
        """Work out the door state at start-up. Power may have been cut in the
        middle of a move, so only the closed switch is trusted."""
        self.hw.disable_driver()
        if self.hw.door_closed_pressed():
            self._set(DoorState.CLOSED, 0)
            self._event("Start-up: door-closed switch is pressed, so the door is closed")
        else:
            self._set(DoorState.UNKNOWN, None)
            self._event(
                "Start-up: door-closed switch is NOT pressed, so the door position is unknown "
                "(power may have been cut mid-move). The motor stays off. The next book pull, "
                "temperature trigger or Close button will drive the door closed.",
                logging.WARNING,
            )
        return self.state

    def _closed_switch_confirmed(self) -> bool:
        # Read twice, 100 us apart, so a single spike of electrical noise from
        # the motor wires cannot stop the door early.
        if not self.hw.door_closed_pressed():
            return False
        busy_wait(100e-6)
        return self.hw.door_closed_pressed()

    def _sync_with_switch(self) -> None:
        """Before planning a move, make sure our idea of the door matches the switch."""
        pressed = self.hw.door_closed_pressed()
        if pressed and self.state != DoorState.CLOSED:
            self._set(DoorState.CLOSED, 0)
            self._event("Door-closed switch is pressed, door is closed")
        elif not pressed and self.state == DoorState.CLOSED:
            # Someone has pulled the door open by hand. We no longer know where it is.
            self._set(DoorState.UNKNOWN, None)
            self._event(
                "Door-closed switch released while the door should be closed. Position unknown.",
                logging.WARNING,
            )

    # --------------------------------------------------------------- requests
    def request(self, action: str, source: str = "web", steps: int = 0, wait: bool = False) -> Result:
        """Ask for a move. ``action`` is open, close, toggle, auto_open or jog.

        With wait=False the move runs on its own thread and this returns at
        once. With wait=True it runs on the caller's thread.
        """
        if self._shutting_down:
            return Result(False, "Shutting down")
        if not self._move_lock.acquire(blocking=False):
            log.info("%s: %s ignored, the door is already moving", source, action)
            return Result(False, "Busy: the door is already moving")
        try:
            move, result = self._plan(action, steps)
        except BaseException:
            self._move_lock.release()
            raise
        if move is None:
            self._move_lock.release()
            log.info("%s: %s -> %s", source, action, result.message)
            return result
        self._event(f"{source}: {result.message}")
        self._abort.clear()
        if wait:
            self._run_move(move)
        else:
            self._move_thread = threading.Thread(
                target=self._run_move, args=(move,), name="door-move", daemon=True
            )
            self._move_thread.start()
        return result

    def _plan(self, action: str, steps: int) -> tuple[Optional[Callable[[], None]], Result]:
        """Decide what a request means right now. Called with the move lock held."""
        if self.fault:
            return None, Result(False, f"Fault: {self.fault_reason} Reset the fault first.")
        self._sync_with_switch()
        cfg = self.store.config

        if action == "jog":
            steps = int(steps)
            if steps == 0:
                return None, Result(False, "Jog needs a step count other than 0")
            if steps < 0 and self.hw.door_closed_pressed():
                return None, Result(False, "Already at the closed switch")
            return (lambda: self._jog(steps)), Result(True, f"Jogging {steps:+d} steps", True)

        if action == "toggle":
            action = "open" if self.state == DoorState.CLOSED else "close"
        elif action == "auto_open":  # temperature trigger
            if self.state == DoorState.OPEN:
                return None, Result(True, "Door is already open")
            action = "open"

        if action == "open" and self.state == DoorState.UNKNOWN:
            # Unknown position (for example power was cut mid-move): never open.
            # An open move is a fixed number of steps counted from fully closed.
            # Started from an unknown point it could drive the door past its
            # fully open stop and strain the hinge, the mechanism or the motor.
            # A close move is safe because it has an end point we can sense: it
            # stops the moment the closed switch trips, and the step limit plus
            # fault check catches a switch that never trips. Once closed, the
            # position is known again (0) and normal operation carries on.
            action = "close"

        if action == "open":
            if self.position is not None and self.position >= cfg.steps_to_open:
                return None, Result(True, "Door is already open")
            return self._open, Result(True, "Opening", True)

        if action == "close":
            if self.hw.door_closed_pressed():
                return None, Result(True, "Door is already closed")
            if self.state == DoorState.UNKNOWN:
                msg = "Position unknown, driving the door closed to find the closed switch"
            else:
                msg = "Closing"
            return self._close, Result(True, msg, True)

        return None, Result(False, f"Unknown action: {action}")

    def _run_move(self, move: Callable[[], None]) -> None:
        try:
            move()
        except Exception as exc:  # never leave the lock held or the motor on
            self.hw.disable_driver()
            self._set(DoorState.UNKNOWN, None)
            self._event(f"Move failed: {exc}", logging.ERROR)
            log.exception("Move failed")
        finally:
            self._move_lock.release()

    # ------------------------------------------------------------------ moves
    def _speeds(self, steps: int) -> list[float]:
        cfg = self.store.config
        return step_speeds(steps, cfg.speed_pps, cfg.ramp_steps, cfg.start_pps)

    def _open(self) -> None:
        cfg = self.store.config
        start = self.position or 0
        steps = cfg.steps_to_open - start
        self._set(DoorState.OPENING, start)
        done = self.motor.move(cfg.open_dir, self._speeds(steps), self._abort.is_set)
        position = start + done
        self._set(DoorState.OPEN, position)
        if done == steps and self.hw.door_closed_pressed():
            # A switch that is stuck or shorted to GND would make the next
            # toggle think the door is closed and drive it past its open stop.
            self._enter_fault(
                "The door-closed switch is still pressed after opening. "
                "It may be stuck, mounted too far out, or its wire may be shorted to GND."
            )
        elif done == steps:
            self._event("Door opened")
        else:
            self._event(f"Opening stopped part way, {position} steps from closed", logging.WARNING)

    def _close(self) -> None:
        cfg = self.store.config
        limit = cfg.steps_to_open + cfg.close_margin_steps
        start = self.position
        expected = cfg.steps_to_open if start is None else start
        expected = max(0, min(expected, limit))
        # Normal ramped move for the distance we expect, then creep slowly for
        # the margin so the switch is met gently if the door is further out.
        creep = min(cfg.start_pps, cfg.speed_pps)
        speeds = self._speeds(expected) + [float(creep)] * (limit - expected)
        self._set(DoorState.CLOSING, start)

        def should_stop() -> bool:
            return self._abort.is_set() or self._closed_switch_confirmed()

        done = self.motor.move(1 - cfg.open_dir, speeds, should_stop)

        if self.hw.door_closed_pressed():
            self._set(DoorState.CLOSED, 0)
            self._event(f"Door closed (switch tripped after {done} steps)")
        elif self._abort.is_set():
            position = None if start is None else max(0, start - done)
            self._set(DoorState.UNKNOWN if position is None else DoorState.OPEN, position)
            self._event("Closing stopped part way", logging.WARNING)
        else:
            self._enter_fault(
                f"The door-closed switch did not trip after {done} steps "
                f"(steps_to_open {cfg.steps_to_open} + margin {cfg.close_margin_steps}). "
                "Check for an obstruction, a loose belt or coupling, the switch wiring, "
                "or a reversed open_dir."
            )

    def _jog(self, steps: int) -> None:
        cfg = self.store.config
        closing = steps < 0
        dir_level = 1 - cfg.open_dir if closing else cfg.open_dir
        start = self.position
        self._set(DoorState.JOGGING, start)

        def should_stop() -> bool:
            # A jog towards closed still stops at the closed switch.
            return self._abort.is_set() or (closing and self._closed_switch_confirmed())

        done = self.motor.move(dir_level, self._speeds(abs(steps)), should_stop)
        position = None if start is None else start + (-done if closing else done)
        if self.hw.door_closed_pressed():
            self._set(DoorState.CLOSED, 0)
        elif position is None or position <= 0:
            self._set(DoorState.UNKNOWN, None)
        else:
            self._set(DoorState.OPEN, position)
        self._event(f"Jogged {'-' if closing else '+'}{done} steps")

    # ----------------------------------------------------------------- faults
    def _enter_fault(self, reason: str) -> None:
        self.hw.disable_driver()
        with self._state_lock:
            self.fault = True
            self.fault_reason = reason
        self._set(DoorState.UNKNOWN, None)
        self._event(f"FAULT: {reason} Fix the problem, then press Reset fault.", logging.WARNING)

    def reset_fault(self) -> Result:
        if not self._move_lock.acquire(blocking=False):
            return Result(False, "Busy: the door is moving")
        try:
            if not self.fault:
                return Result(True, "No fault to reset")
            with self._state_lock:
                self.fault = False
                self.fault_reason = ""
            if self.hw.door_closed_pressed():
                self._set(DoorState.CLOSED, 0)
            else:
                self._set(DoorState.UNKNOWN, None)
            self._event(f"Fault reset, door is {self.state.value}")
            return Result(True, "Fault reset")
        finally:
            self._move_lock.release()

    # ----------------------------------------------------------- housekeeping
    def abort_move(self) -> None:
        """Stop the current move as soon as possible (used by test mode and shutdown)."""
        self._abort.set()

    def wait_idle(self, timeout: float = 30.0) -> bool:
        if self._move_lock.acquire(timeout=timeout):
            self._move_lock.release()
            return True
        return False

    def seconds_since_last_step(self) -> float:
        return time.perf_counter() - self.motor.last_step_time

    def status(self) -> dict[str, Any]:
        with self._state_lock:
            return {
                "state": self.state.value,
                "position": self.position,
                "moving": self.state in MOVING_STATES,
                "fault": self.fault,
                "fault_reason": self.fault_reason,
                "last_event": self.last_event,
                "last_event_time": time.strftime(
                    "%Y-%m-%d %H:%M:%S", time.localtime(self.last_event_time)
                ),
                "closed_switch": self.hw.door_closed_pressed(),
                "book_switch": self.hw.book_pressed(),
                "driver_enabled": self.hw.driver_enabled,
            }

    def shutdown(self, timeout: float = 5.0) -> None:
        """Stop any move, then make sure the driver is off."""
        self._shutting_down = True
        self._abort.set()
        self.wait_idle(timeout)
        self.hw.disable_driver()
