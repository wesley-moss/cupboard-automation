import pytest

from cupboard_door.motor import step_delays, step_speeds


def test_length_matches_steps():
    assert len(step_speeds(1000, 800, 200, 150)) == 1000


def test_zero_steps_is_empty():
    assert step_speeds(0, 800, 200, 150) == []


def test_starts_and_ends_slow_and_cruises_at_full_speed():
    speeds = step_speeds(1000, 800, 200, 150)
    assert speeds[0] == pytest.approx(150)
    assert speeds[-1] == pytest.approx(150)
    assert max(speeds) == pytest.approx(800)
    assert speeds.count(800.0) == 1000 - 2 * 200  # the flat top of the trapezoid


def test_symmetric_and_rising_then_falling():
    speeds = step_speeds(1000, 800, 200, 150)
    assert speeds == list(reversed(speeds))
    first_half = speeds[:500]
    assert all(b >= a for a, b in zip(first_half, first_half[1:]))
    assert all(150 <= v <= 800 for v in speeds)


def test_constant_acceleration_speed_squared_is_linear():
    speeds = step_speeds(1000, 800, 200, 150)
    squares = [v * v for v in speeds[:200]]
    gaps = [b - a for a, b in zip(squares, squares[1:])]
    assert max(gaps) == pytest.approx(min(gaps))


def test_short_move_is_a_triangle():
    speeds = step_speeds(100, 800, 200, 150)  # too short for two 200-step ramps
    assert len(speeds) == 100
    assert max(speeds) < 800
    assert speeds[0] == pytest.approx(150)
    assert speeds == list(reversed(speeds))


def test_no_ramp_means_full_speed_throughout():
    assert step_speeds(50, 800, 0, 150) == [800.0] * 50


def test_start_speed_never_above_cruise_speed():
    speeds = step_speeds(100, 100, 20, 500)
    assert all(v == pytest.approx(100) for v in speeds)


def test_single_step():
    assert step_speeds(1, 800, 200, 150) == [800.0]


def test_delays_are_reciprocal():
    assert step_delays([100.0, 200.0]) == [0.01, 0.005]


class FakeClock:
    """Moves forward 1 us per read, and by the full amount on sleep."""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        self.t += 1e-6
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


class FakeHw:
    def __init__(self, clock, late_step: int) -> None:
        self.clock, self.late_step, self.pulses, self.enabled = clock, late_step, [], False

    def set_direction(self, level): pass
    def enable_driver(self): self.enabled = True
    def disable_driver(self): self.enabled = False

    def step_pulse(self):
        self.pulses.append(self.clock.t)
        if len(self.pulses) == self.late_step:
            self.clock.t += 0.005  # Linux paused us for 5 ms


def test_late_step_is_not_followed_by_a_burst():
    from cupboard_door.motor import MIN_GAP_FRACTION, Motor

    clock = FakeClock()
    hw = FakeHw(clock, late_step=10)
    done = Motor(hw, clock=clock, sleep=clock.sleep).move(1, [1000.0] * 50, lambda: False)
    assert done == 50 and not hw.enabled  # driver switched off afterwards
    gaps = [b - a for a, b in zip(hw.pulses, hw.pulses[1:])]
    assert min(gaps) >= MIN_GAP_FRACTION * 0.001 - 1e-6
    assert max(gaps) > 0.005  # the late step itself


def test_move_stops_when_asked():
    from cupboard_door.motor import Motor

    clock = FakeClock()
    hw = FakeHw(clock, late_step=-1)
    done = Motor(hw, clock=clock, sleep=clock.sleep).move(1, [1000.0] * 50, lambda: len(hw.pulses) >= 7)
    assert done == 7 and not hw.enabled
