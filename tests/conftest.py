"""Shared fixtures. Everything runs on gpiozero's mock pins via --simulate code."""

from __future__ import annotations

import os
import sys
from typing import Iterator

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cupboard_door.config import ConfigStore  # noqa: E402
from cupboard_door.main import System, build_system  # noqa: E402
from cupboard_door.sensors import FakeSensor  # noqa: E402

# Small, fast moves so the tests finish quickly.
FAST = {
    "steps_to_open": 200,
    "speed_pps": 10000,
    "ramp_steps": 20,
    "start_pps": 4000,
    "close_margin_steps": 50,
}


@pytest.fixture
def store(tmp_path) -> ConfigStore:
    s = ConfigStore(str(tmp_path / "door_config.json"))
    s.update(FAST, save=False)
    return s


@pytest.fixture
def make_system(store) -> Iterator:
    made: list[System] = []

    def _make(start_open: bool = False, sensor: FakeSensor | None = None) -> System:
        system = build_system(store, simulate=True, sim_start_open=start_open, sensor=sensor or FakeSensor())
        made.append(system)
        return system

    yield _make
    for system in made:
        system.door.shutdown()
        system.hw.close()
