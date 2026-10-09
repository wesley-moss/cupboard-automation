import json
import os

import pytest

from cupboard_door.config import DEFAULT_PINS, ConfigError, ConfigStore, validate_settings


def test_missing_file_gives_defaults_and_writes_nothing(tmp_path):
    store = ConfigStore(str(tmp_path / "door_config.json"))
    cfg = store.load()
    assert cfg.steps_to_open == 1600 and cfg.pins == DEFAULT_PINS
    assert os.listdir(tmp_path) == []


def test_save_and_reload_round_trip(tmp_path):
    path = tmp_path / "door_config.json"
    store = ConfigStore(str(path))
    store.update({"steps_to_open": 1234, "temp_delta_c": 2.5})
    assert os.listdir(tmp_path) == ["door_config.json"]  # no temp files left behind
    again = ConfigStore(str(path)).load()
    assert again.steps_to_open == 1234 and again.temp_delta_c == 2.5


def test_corrupt_file_falls_back_to_defaults_and_is_kept(tmp_path):
    path = tmp_path / "door_config.json"
    path.write_text("{ not json")
    cfg = ConfigStore(str(path)).load()
    assert cfg.steps_to_open == 1600
    assert path.read_text() == "{ not json"


def test_bad_values_in_file_are_skipped(tmp_path):
    path = tmp_path / "door_config.json"
    path.write_text(json.dumps({"speed_pps": -5, "ramp_steps": 50, "pins": {"step": 12}}))
    cfg = ConfigStore(str(path)).load()
    assert cfg.speed_pps == 800  # bad value ignored
    assert cfg.ramp_steps == 50
    assert cfg.pins["step"] == 12 and cfg.pins["dir"] == 21


def test_duplicate_pins_rejected(tmp_path):
    path = tmp_path / "door_config.json"
    path.write_text(json.dumps({"pins": {"step": 21}}))
    assert ConfigStore(str(path)).load().pins == DEFAULT_PINS


@pytest.mark.parametrize(
    "changes",
    [{"speed_pps": 0}, {"open_dir": 2}, {"steps_to_open": 1.5}, {"temp_delta_c": "hot"}, {"nope": 1}, {"open_dir": True}],
)
def test_validation_rejects_bad_settings(changes):
    with pytest.raises(ConfigError):
        validate_settings(changes)


def test_update_with_bad_value_changes_nothing(tmp_path):
    store = ConfigStore(str(tmp_path / "door_config.json"))
    with pytest.raises(ConfigError):
        store.update({"steps_to_open": 900, "speed_pps": 0})
    assert store.config.steps_to_open == 1600
    assert not os.path.exists(store.path)
