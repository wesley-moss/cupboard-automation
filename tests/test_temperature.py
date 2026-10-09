import pytest

from cupboard_door.sensors import (
    DS18B20Sensor,
    FakeSensor,
    SensorError,
    TemperatureMonitor,
    TempWindow,
    parse_w1_slave,
)

GOOD = "72 01 4b 46 7f ff 0e 10 57 : crc=57 YES\n72 01 4b 46 7f ff 0e 10 57 t=23125\n"
BAD_CRC = "72 01 4b 46 7f ff 0e 10 57 : crc=00 NO\n72 01 4b 46 7f ff 0e 10 57 t=23125\n"


# ------------------------------------------------------------- window logic
def test_rise_within_window_triggers():
    w = TempWindow()
    assert w.add(0, 21.0, 3.0, 300) is None
    assert w.add(100, 22.5, 3.0, 300) is None
    assert w.add(200, 24.0, 3.0, 300) == pytest.approx(3.0)


def test_fall_within_window_triggers_negative():
    w = TempWindow()
    w.add(0, 25.0, 3.0, 300)
    assert w.add(60, 21.5, 3.0, 300) == pytest.approx(-3.5)


def test_small_change_does_not_trigger():
    w = TempWindow()
    w.add(0, 21.0, 3.0, 300)
    assert w.add(100, 23.9, 3.0, 300) is None


def test_slow_change_outside_window_does_not_trigger():
    w = TempWindow()
    for minute in range(0, 31):  # 0.1 C per minute: 3 C takes 30 minutes
        assert w.add(minute * 60, 21.0 + minute * 0.1, 3.0, 300) is None


def test_old_samples_are_dropped():
    w = TempWindow()
    w.add(0, 21.0, 3.0, 300)
    w.add(400, 22.0, 3.0, 300)
    assert len(w.samples) == 1


def test_clear_empties_history():
    w = TempWindow()
    w.add(0, 21.0, 3.0, 300)
    w.clear()
    assert w.add(10, 24.0, 3.0, 300) is None


# ---------------------------------------------------------------- parsing
def test_parse_good_reading():
    assert parse_w1_slave(GOOD) == pytest.approx(23.125)


def test_parse_negative_reading():
    text = GOOD.replace("t=23125", "t=-5250")
    assert parse_w1_slave(text) == pytest.approx(-5.25)


@pytest.mark.parametrize(
    "text",
    [BAD_CRC, "", "garbage", GOOD.replace("t=23125", "t=85000"), GOOD.replace("t=23125", "x")],
)
def test_parse_rejects_bad_data(text):
    with pytest.raises(SensorError):
        parse_w1_slave(text)


def test_ds18b20_missing_then_found(tmp_path):
    sensor = DS18B20Sensor(str(tmp_path))
    assert not sensor.available()
    with pytest.raises(SensorError):
        sensor.read()
    dev = tmp_path / "28-0123456789ab"
    dev.mkdir()
    (dev / "w1_slave").write_text(GOOD)
    assert sensor.available()
    assert sensor.name == "28-0123456789ab"
    assert sensor.read() == pytest.approx(23.125)
    (dev / "w1_slave").write_text(BAD_CRC)
    with pytest.raises(SensorError):
        sensor.read()


def test_ds18b20_disappearing_does_not_crash(tmp_path):
    dev = tmp_path / "28-0123456789ab"
    dev.mkdir()
    (dev / "w1_slave").write_text(GOOD)
    sensor = DS18B20Sensor(str(tmp_path))
    assert sensor.available()
    (dev / "w1_slave").unlink()
    with pytest.raises(SensorError):
        sensor.read()
    assert not sensor.available()


# ---------------------------------------------------------------- monitor
class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def test_monitor_triggers_once_and_clears_history(store):
    clock, fake, calls = Clock(), FakeSensor(21.0), []
    mon = TemperatureMonitor(fake, store, on_trigger=lambda c, t: calls.append(c), clock=clock)
    mon.poll_once()
    clock.t += 60
    fake.temperature = 24.2
    mon.poll_once()
    assert calls == [pytest.approx(3.2)]
    assert len(mon.window.samples) == 0
    clock.t += 10
    mon.poll_once()  # same temperature again: no second trigger
    assert len(calls) == 1


def test_monitor_survives_read_errors(store):
    clock, fake = Clock(), FakeSensor(21.0)
    mon = TemperatureMonitor(fake, store, clock=clock)
    mon.poll_once()
    assert mon.sensor_ok
    fake.fail_reads = 5
    for _ in range(3):
        mon.poll_once()
    assert not mon.sensor_ok
    assert mon.temperature is None
    assert "CRC" in mon.last_error
    fake.fail_reads = 0
    mon.poll_once()
    assert mon.sensor_ok and mon.temperature == 21.0


def test_monitor_waits_for_missing_sensor_then_carries_on(store):
    clock, fake = Clock(), FakeSensor(missing=True)
    mon = TemperatureMonitor(fake, store, clock=clock)
    assert mon.poll_once() == TemperatureMonitor.FIND_RETRY_S  # still in the 60 s wait
    clock.t += 61
    assert mon.poll_once() == TemperatureMonitor.MISSING_RETRY_S  # gave up waiting, keeps checking
    assert not mon.sensor_found and mon.temperature is None
    fake.missing = False  # plugged in later
    clock.t += 30
    mon.poll_once()
    assert mon.sensor_found and mon.temperature == 21.0
