"""Terminal test mode for tuning: jog with keys, set the open position, change speed.

The book lever and temperature trigger are switched off here so the door only
moves when you press a key.
"""

from __future__ import annotations

import logging
import select
import sys
import termios
import time
import tty
from typing import Optional

from .config import SETTING_LIMITS, ConfigStore
from .hardware import HardwareError
from .sensors import TemperatureMonitor

log = logging.getLogger(__name__)

HELP = """
Cupboard door test mode
  d / a     jog 50 steps open / closed       D / A   jog 200 steps
  c / z     jog 10 steps open / closed
  h         close (drive to the closed switch, position becomes 0)
  o         open to the saved open position
  p         set the open position HERE (steps_to_open = current position)
  + / -     speed up / down by 50 steps per second
  ] / [     ramp steps up / down by 25
  f         flip the opening direction (open_dir)
  x         reset a fault
  space     stop the current move
  s         save settings to the config file
  ?         show this help
  q         quit
"""


def _clamp(name: str, value: float) -> float:
    kind, low, high = SETTING_LIMITS[name]
    return kind(max(low, min(high, value)))


def _read_key(timeout: float) -> Optional[str]:
    ready, _, _ = select.select([sys.stdin], [], [], timeout)
    return sys.stdin.read(1) if ready else None


def run_test_mode(store: ConfigStore, simulate: bool = False, sim_start_open: bool = False) -> int:
    from .main import build_system

    if not sys.stdin.isatty():
        print("Test mode needs an interactive terminal.")
        return 2
    try:
        system = build_system(store, simulate=simulate, sim_start_open=sim_start_open)
    except HardwareError as exc:
        print(exc)
        return 1

    door = system.door
    # A monitor with no trigger: shows the temperature but never moves the door.
    temps = TemperatureMonitor(system.temp_monitor.sensor, store)
    temps.start()
    # Log lines clear the status line first so the two do not run together.
    for handler in logging.getLogger().handlers:
        handler.setFormatter(logging.Formatter("\r\033[K%(levelname)s %(message)s"))
    unsaved = False
    old_term = termios.tcgetattr(sys.stdin)
    print(HELP)
    if door.state.value == "unknown":
        print("Door position is unknown. Press h to drive it to the closed switch first.\n")

    def note(text: str) -> None:
        sys.stdout.write("\r\033[K" + text + "\n")

    def change(name: str, value: float) -> None:
        nonlocal unsaved
        store.update({name: _clamp(name, value)}, save=False)
        unsaved = True
        note(f"{name} = {getattr(store.config, name)}")

    try:
        tty.setcbreak(sys.stdin.fileno())
        while True:
            cfg = store.config
            s = door.status()
            temp = temps.temperature
            line = (
                f"{s['state']:<8} pos {str(s['position']):>6}  open at {cfg.steps_to_open}  "
                f"speed {cfg.speed_pps}  ramp {cfg.ramp_steps}  dir {cfg.open_dir}  "
                f"closed sw {'PRESSED' if s['closed_switch'] else 'open   '}  "
                f"book {'in ' if s['book_switch'] else 'OUT'}  "
                f"temp {'--' if temp is None else f'{temp:.1f}C'}"
                f"{'  FAULT' if s['fault'] else ''}{'  *unsaved*' if unsaved else ''}"
            )
            sys.stdout.write("\r\033[K" + line)
            sys.stdout.flush()

            key = _read_key(0.25)
            if key is None:
                continue
            jogs = {"d": 50, "a": -50, "D": 200, "A": -200, "c": 10, "z": -10}
            result = None
            if key in jogs:
                result = door.request("jog", source="Test mode", steps=jogs[key])
            elif key == "h":
                result = door.request("close", source="Test mode")
            elif key == "o":
                result = door.request("open", source="Test mode")
            elif key == " ":
                door.abort_move()
                note("Stopping")
            elif key == "p":
                if not door.wait_idle(0.1):
                    note("Wait for the move to finish first")
                elif door.position is None or door.position <= 0:
                    note("Position unknown or closed. Press h to close, then jog open, then p.")
                else:
                    change("steps_to_open", door.position)
            elif key in "+=":
                change("speed_pps", cfg.speed_pps + 50)
            elif key in "-_":
                change("speed_pps", cfg.speed_pps - 50)
            elif key == "]":
                change("ramp_steps", cfg.ramp_steps + 25)
            elif key == "[":
                change("ramp_steps", cfg.ramp_steps - 25)
            elif key == "f":
                change("open_dir", 1 - cfg.open_dir)
            elif key == "x":
                result = door.reset_fault()
            elif key == "s":
                try:
                    store.save()
                    unsaved = False
                    note(f"Saved to {store.path}")
                except OSError as exc:
                    note(f"Could not save: {exc}")
            elif key == "?":
                note(HELP)
            elif key in "qQ":
                break
            if result is not None and not result.started:  # started moves are already logged
                note(result.message)
    except KeyboardInterrupt:
        pass
    finally:
        termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_term)
        temps.stop()
        system.door.shutdown()
        system.hw.close()
        print()
        if unsaved:
            print("Changes were NOT saved. Run test mode again and press s to keep them.")
        print("Test mode finished. Motor disabled.")
    time.sleep(0.05)
    return 0
