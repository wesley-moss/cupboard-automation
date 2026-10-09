"""Step timing: the speed ramp and the loop that sends STEP pulses."""

from __future__ import annotations

import math
import time
from typing import Callable, Iterable, Sequence

from .hardware import Hardware

ENABLE_SETTLE_S = 0.005  # give the A4988 a moment after /ENABLE goes low
MIN_GAP_FRACTION = 0.75  # after a late step, the next gap is never shorter than 75% of planned


def step_speeds(total_steps: int, speed_pps: float, ramp_steps: int, start_pps: float) -> list[float]:
    """Speed (steps per second) for each step of a trapezoidal move.

    The move starts at ``start_pps``, speeds up over ``ramp_steps`` steps to
    ``speed_pps``, cruises, then slows down over the last ``ramp_steps`` steps.
    Short moves that cannot fit two full ramps get a triangle: half the steps
    speeding up, half slowing down, never reaching full speed.

    The speed for step k into a ramp is sqrt(v0^2 + (v1^2 - v0^2) * k / ramp),
    which is constant acceleration (a straight line on a speed vs time graph).
    """
    if total_steps <= 0:
        return []
    speed = float(speed_pps)
    start = min(float(start_pps), speed)
    ramp = max(0, min(int(ramp_steps), total_steps // 2))
    speeds: list[float] = []
    for i in range(total_steps):
        k = min(i, total_steps - 1 - i)  # steps from the nearest end of the move
        if k < ramp:
            v = math.sqrt(start * start + (speed * speed - start * start) * k / ramp)
        else:
            v = speed
        speeds.append(v)
    return speeds


def step_delays(speeds: Iterable[float]) -> list[float]:
    """Time between steps, in seconds, for a list of speeds."""
    return [1.0 / v for v in speeds]


class Motor:
    """Sends a sequence of STEP pulses with accurate spacing.

    Timing uses a running deadline on a monotonic clock, so small delays from
    Linux do not add up over a move. If a step is late (Linux is not a
    real-time system), the time is made up gradually: the next gap is never
    shorter than 75% of its planned length, so there is no burst of fast steps
    that could stall the motor. The driver is enabled only for the move and is
    always disabled again afterwards, even if something goes wrong.
    """

    def __init__(
        self,
        hw: Hardware,
        clock: Callable[[], float] = time.perf_counter,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.hw = hw
        self._clock = clock
        self._sleep = sleep
        self.last_step_time = clock()

    def move(
        self,
        dir_level: int,
        speeds: Sequence[float] | Iterable[float],
        should_stop: Callable[[], bool],
    ) -> int:
        """Step once per entry in ``speeds``. Returns how many steps were made.

        ``should_stop`` is checked before every step; the move ends as soon as
        it returns True (for example when the door-closed switch trips).
        """
        self.hw.set_direction(dir_level)
        self.hw.enable_driver()
        done = 0
        try:
            self._sleep(ENABLE_SETTLE_S)
            deadline = self._clock()
            for speed in speeds:
                if should_stop():
                    break
                self.hw.step_pulse()
                done += 1
                now = self._clock()
                self.last_step_time = now
                gap = 1.0 / speed
                deadline = max(deadline + gap, now + MIN_GAP_FRACTION * gap)
                self._wait_until(deadline)
        finally:
            self.hw.disable_driver()
        return done

    def _wait_until(self, deadline: float) -> None:
        # Sleep for most of the gap (frees the CPU for the web server), then
        # spin for the last fraction of a millisecond for accuracy.
        remaining = deadline - self._clock()
        if remaining > 0.0006:
            self._sleep(remaining - 0.0004)
        while self._clock() < deadline:
            pass
