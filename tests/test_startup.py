import os
import socket
import time

from cupboard_door.door import DoorState
from cupboard_door.main import sd_notify, watchdog_interval
from cupboard_door.sensors import FakeSensor, TemperatureMonitor


def test_sensor_missing_does_not_stop_startup(make_system):
    system = make_system(sensor=FakeSensor(missing=True))
    assert system.door.state == DoorState.CLOSED
    mon = system.temp_monitor
    assert mon.poll_once() == TemperatureMonitor.FIND_RETRY_S
    assert mon.status()["temperature_c"] is None
    # The door still works without a sensor.
    system.door.request("open", wait=True)
    assert system.door.state == DoorState.OPEN


def test_threads_start_and_stop_cleanly(make_system):
    system = make_system(sensor=FakeSensor(missing=True))
    system.start(web=False)
    time.sleep(0.1)
    assert system.health_problems() == []
    system.stop()
    assert not system.hw.driver_enabled


def test_book_pull_toggles_through_threads(make_system):
    system = make_system()
    system.start(web=False)
    try:
        system.sim_hw.pull_book(hold_s=0.2)
        deadline = time.time() + 5
        while system.door.state != DoorState.OPEN and time.time() < deadline:
            time.sleep(0.02)
        assert system.door.state == DoorState.OPEN
    finally:
        system.stop()


def test_health_check_spots_dead_thread(make_system):
    system = make_system()
    problems = system.health_problems()  # threads never started
    assert any("book-monitor" in p for p in problems)


def test_sd_notify_sends_to_socket(tmp_path, monkeypatch):
    path = str(tmp_path / "notify.sock")
    server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    server.bind(path)
    try:
        monkeypatch.setenv("NOTIFY_SOCKET", path)
        assert sd_notify("WATCHDOG=1")
        assert server.recv(100) == b"WATCHDOG=1"
    finally:
        server.close()


def test_sd_notify_without_systemd_is_harmless(monkeypatch):
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    assert sd_notify("READY=1") is False


def test_watchdog_interval(monkeypatch):
    monkeypatch.delenv("WATCHDOG_USEC", raising=False)
    assert watchdog_interval() is None
    monkeypatch.setenv("WATCHDOG_USEC", "30000000")
    monkeypatch.setenv("WATCHDOG_PID", str(os.getpid()))
    assert watchdog_interval() == 15.0
    monkeypatch.setenv("WATCHDOG_PID", "1")
    assert watchdog_interval() is None
