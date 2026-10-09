import time

from cupboard_door.door import DoorState


# --------------------------------------------------------- start-up state
def test_startup_switch_pressed_means_closed(make_system):
    system = make_system(start_open=False)
    assert system.door.state == DoorState.CLOSED
    assert system.door.position == 0
    assert not system.hw.driver_enabled


def test_startup_switch_not_pressed_means_unknown_and_no_movement(make_system):
    system = make_system(start_open=True)
    sim = system.sim_hw
    assert system.door.state == DoorState.UNKNOWN
    assert system.door.position is None
    assert not system.hw.driver_enabled
    time.sleep(0.1)
    assert sim.open_steps == 0 and sim.close_steps == 0  # nothing moved by itself


def test_unknown_toggle_closes_never_opens(make_system):
    system = make_system(start_open=True)
    result = system.door.request("toggle", source="Book pull", wait=True)
    assert result.started
    assert system.sim_hw.open_steps == 0
    assert system.door.state == DoorState.CLOSED
    assert system.sim_hw.position == 0


def test_unknown_open_button_closes_instead(make_system):
    system = make_system(start_open=True)
    result = system.door.request("open", wait=True)
    assert "unknown" in result.message.lower()
    assert system.sim_hw.open_steps == 0
    assert system.door.state == DoorState.CLOSED


def test_unknown_temperature_trigger_closes(make_system):
    system = make_system(start_open=True)
    system.door.request("auto_open", source="Temperature", wait=True)
    assert system.sim_hw.open_steps == 0
    assert system.door.state == DoorState.CLOSED


# ------------------------------------------------------------------ moves
def test_open_then_close_cycle(make_system, store):
    system = make_system()
    door, sim = system.door, system.sim_hw
    door.request("open", wait=True)
    assert door.state == DoorState.OPEN
    assert door.position == store.config.steps_to_open == sim.position
    assert not sim.door_closed_pressed()
    door.request("toggle", wait=True)
    assert door.state == DoorState.CLOSED and sim.position == 0
    assert not system.hw.driver_enabled  # driver off when idle
    assert sim.steps_while_disabled == 0  # never stepped with the driver off


def test_temperature_trigger_opens_closed_door_only(make_system):
    system = make_system()
    system.door.request("auto_open", wait=True)
    assert system.door.state == DoorState.OPEN
    steps = system.sim_hw.open_steps
    result = system.door.request("auto_open", wait=True)
    assert not result.started and system.sim_hw.open_steps == steps


def test_close_when_already_closed_does_nothing(make_system):
    system = make_system()
    result = system.door.request("close", wait=True)
    assert result.ok and not result.started
    assert system.sim_hw.close_steps == 0


def test_lock_blocks_second_move(make_system, store):
    store.update({"speed_pps": 200, "start_pps": 200}, save=False)  # about 1 s move
    system = make_system()
    first = system.door.request("open")
    assert first.started
    second = system.door.request("close", source="Book pull")
    assert not second.ok and "Busy" in second.message
    system.door.abort_move()
    assert system.door.wait_idle(5)


def test_jog_towards_closed_stops_at_switch(make_system):
    system = make_system()
    system.door.request("jog", steps=60, wait=True)
    assert system.door.position == 60 and system.door.state == DoorState.OPEN
    system.door.request("jog", steps=-200, wait=True)
    assert system.door.state == DoorState.CLOSED
    assert system.sim_hw.position == 0  # stopped at the switch, not 140 steps past it


def test_shutdown_stops_move_and_disables_driver(make_system, store):
    store.update({"speed_pps": 200, "start_pps": 200}, save=False)
    system = make_system()
    system.door.request("open")
    time.sleep(0.1)
    system.door.shutdown()
    assert not system.hw.driver_enabled
    assert system.sim_hw.position < store.config.steps_to_open
    assert not system.door.request("open").ok


# ------------------------------------------------------------------ fault
def test_switch_never_trips_enters_fault(make_system, store):
    system = make_system()
    door, sim = system.door, system.sim_hw
    door.request("open", wait=True)
    sim.set_switch_broken(True)
    door.request("close", wait=True)
    cfg = store.config
    assert door.fault
    assert sim.close_steps == cfg.steps_to_open + cfg.close_margin_steps  # stopped at the limit
    assert "did not trip" in door.fault_reason
    assert not system.hw.driver_enabled


def test_fault_blocks_moves_until_reset(make_system):
    system = make_system()
    door, sim = system.door, system.sim_hw
    door.request("open", wait=True)
    sim.set_switch_broken(True)
    door.request("close", wait=True)
    steps = sim.open_steps + sim.close_steps
    for action in ("open", "close", "toggle", "auto_open"):
        assert not door.request(action, wait=True).ok
    assert not door.request("jog", steps=50, wait=True).ok
    assert sim.open_steps + sim.close_steps == steps

    sim.set_switch_broken(False)  # the door really is closed; switch fixed
    assert door.reset_fault().ok
    assert not door.fault and door.state == DoorState.CLOSED


def test_reset_fault_with_door_not_closed_gives_unknown(make_system):
    system = make_system(start_open=True)
    system.sim_hw.set_switch_broken(True)
    system.door.request("close", wait=True)
    assert system.door.fault
    system.door.reset_fault()
    assert system.door.state == DoorState.UNKNOWN


def test_wrong_open_dir_ends_in_fault_not_damage(make_system, store):
    system = make_system()
    system.door.request("open", wait=True)
    store.update({"open_dir": 1 - store.config.open_dir}, save=False)  # wired backwards
    system.door.request("close", wait=True)
    assert system.door.fault  # drove the wrong way, switch never tripped


def test_stuck_closed_switch_after_opening_is_a_fault(make_system, store):
    system = make_system()
    system.sim_hw.set_switch_stuck(True)  # shorted wire: always reads pressed
    system.door.request("open", wait=True)
    assert system.door.fault
    assert "still pressed" in system.door.fault_reason
    opened = system.sim_hw.open_steps
    assert not system.door.request("toggle", wait=True).ok  # no second open past the stop
    assert system.sim_hw.open_steps == opened == store.config.steps_to_open
