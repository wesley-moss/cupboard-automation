"""Settings, pin numbers and the door_config.json file."""

from __future__ import annotations

import copy
import json
import logging
import os
import tempfile
import threading
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Pin constants (BCM numbering). Every one of these can be overridden in
# door_config.json under "pins", for example {"pins": {"step": 12}}.
# Pin changes take effect after a restart.
# ---------------------------------------------------------------------------
DEFAULT_PINS: dict[str, int] = {
    "step": 20,         # A4988 STEP
    "dir": 21,          # A4988 DIR
    "en": 16,           # A4988 /ENABLE (active low: low = motor on, high = off)
    "door_closed": 23,  # door-closed switch, COM to GND, NO to pin
    "book_lever": 24,   # book lever switch, COM to GND, NO to pin
    "one_wire": 4,      # DS18B20 data (set by dtoverlay=w1-gpio, shown for reference)
}

# Allowed range for each setting: (type, minimum, maximum).
# These are also the settings the web page and the API may change.
SETTING_LIMITS: dict[str, tuple[type, float, float]] = {
    "steps_to_open": (int, 1, 200_000),
    "speed_pps": (int, 10, 10_000),
    "ramp_steps": (int, 0, 50_000),
    "start_pps": (int, 10, 5_000),
    "open_dir": (int, 0, 1),
    "close_margin_steps": (int, 0, 50_000),
    "temp_delta_c": (float, 0.1, 50.0),
    "temp_window_s": (int, 10, 86_400),
    "temp_poll_s": (float, 1.0, 600.0),
    "book_debounce_ms": (int, 1, 1_000),
    "book_lockout_s": (float, 0.0, 120.0),
}


class ConfigError(ValueError):
    """Raised when a setting is missing, the wrong type or out of range."""


@dataclass
class Config:
    # Door movement
    steps_to_open: int = 1600       # steps from fully closed to fully open
    speed_pps: int = 800            # cruising speed, steps per second
    ramp_steps: int = 200           # steps used to speed up, and again to slow down
    start_pps: int = 150            # speed at the very start and end of a move
    open_dir: int = 1               # DIR pin level that opens the door (0 or 1)
    close_margin_steps: int = 300   # extra steps allowed when closing before we call it a fault
    # Temperature trigger
    temp_delta_c: float = 3.0
    temp_window_s: int = 300
    temp_poll_s: float = 10.0
    # Book lever
    book_debounce_ms: int = 50
    book_lockout_s: float = 3.0
    # System (only read at start-up)
    web_port: int = 8080
    sensor_wait_s: int = 60
    w1_devices_dir: str = "/sys/bus/w1/devices"
    gpio_chip: Optional[int] = None  # None lets gpiozero pick the right chip
    pins: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_PINS))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def settings(self) -> dict[str, Any]:
        """Just the settings that can be changed while running."""
        return {name: getattr(self, name) for name in SETTING_LIMITS}


def validate_settings(changes: dict[str, Any]) -> dict[str, Any]:
    """Check a dict of setting changes. Returns cleaned values or raises ConfigError."""
    if not isinstance(changes, dict):
        raise ConfigError("Settings must be a JSON object")
    cleaned: dict[str, Any] = {}
    problems: list[str] = []
    for name, value in changes.items():
        if name not in SETTING_LIMITS:
            problems.append(f"{name}: unknown setting")
            continue
        kind, low, high = SETTING_LIMITS[name]
        try:
            if isinstance(value, bool):
                raise ValueError
            number = float(value)
            if kind is int:
                if number != int(number):
                    raise ValueError
                number = int(number)
        except (TypeError, ValueError):
            problems.append(f"{name}: must be a {'whole number' if kind is int else 'number'}")
            continue
        if not low <= number <= high:
            problems.append(f"{name}: must be between {low:g} and {high:g}")
            continue
        cleaned[name] = number
    if problems:
        raise ConfigError("; ".join(problems))
    return cleaned


def validate_pins(pins: Any) -> dict[str, int]:
    merged = dict(DEFAULT_PINS)
    if not isinstance(pins, dict):
        raise ConfigError("pins must be a JSON object")
    for name, value in pins.items():
        if name not in DEFAULT_PINS:
            raise ConfigError(f"pins.{name}: unknown pin name")
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 27:
            raise ConfigError(f"pins.{name}: must be a BCM GPIO number from 0 to 27")
        merged[name] = value
    used = [merged[n] for n in merged if n != "one_wire"]
    if len(set(used)) != len(used):
        raise ConfigError("pins: two functions share the same GPIO")
    return merged


def config_from_dict(data: dict[str, Any]) -> Config:
    """Build a Config from file contents. Bad values are skipped with a warning
    so that one typo never stops the door from starting."""
    cfg = Config()
    for name, value in data.items():
        try:
            if name in SETTING_LIMITS:
                setattr(cfg, name, validate_settings({name: value})[name])
            elif name == "pins":
                cfg.pins = validate_pins(value)
            elif name in ("web_port", "sensor_wait_s"):
                if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > 65535:
                    raise ConfigError(f"{name}: must be a whole number")
                setattr(cfg, name, value)
            elif name == "w1_devices_dir":
                if not isinstance(value, str):
                    raise ConfigError(f"{name}: must be text")
                cfg.w1_devices_dir = value
            elif name == "gpio_chip":
                if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
                    raise ConfigError(f"{name}: must be a number or null")
                cfg.gpio_chip = value
            elif name.startswith("_"):
                pass  # comments such as "_note" are allowed
            else:
                log.warning("Config: ignoring unknown key %r", name)
        except ConfigError as exc:
            log.warning("Config: ignoring bad value (%s), using the default", exc)
    return cfg


def atomic_write_json(path: str, data: dict[str, Any]) -> None:
    """Write JSON so that a power cut leaves either the old file or the new one,
    never a half-written file: write a temp file, fsync it, rename it over the
    old one, then fsync the folder so the rename itself is on the card."""
    folder = os.path.dirname(os.path.abspath(path))
    fd, tmp_path = tempfile.mkstemp(prefix=".door_config.", suffix=".tmp", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_path, 0o644)
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    try:
        dir_fd = os.open(folder, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        pass  # some file systems do not allow fsync on a folder


class ConfigStore:
    """Holds the live config, shared by every thread, and saves it to disk.

    Readers call ``store.config`` and get the current object. Updates build a
    new object and swap it in, so a reader never sees a half-applied change.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._config = Config()

    @property
    def config(self) -> Config:
        return self._config

    def load(self) -> Config:
        if not os.path.exists(self.path):
            log.info("No config file at %s, using defaults", self.path)
            self._config = Config()
            return self._config
        try:
            with open(self.path, encoding="utf-8") as handle:
                data = json.load(handle)
            if not isinstance(data, dict):
                raise ValueError("top level must be a JSON object")
            self._config = config_from_dict(data)
            log.info("Loaded config from %s", self.path)
        except (OSError, ValueError) as exc:
            # Keep running on defaults rather than crash-looping. The bad file is
            # left alone so it can be inspected and fixed.
            log.error("Could not read %s (%s). Using defaults.", self.path, exc)
            self._config = Config()
        return self._config

    def update(self, changes: dict[str, Any], save: bool = True) -> Config:
        """Validate, apply and (by default) save setting changes.

        Raises ConfigError for bad values (nothing is changed) and OSError if
        the file could not be written (the new values are still applied).
        """
        cleaned = validate_settings(changes)
        with self._lock:
            new = copy.deepcopy(self._config)
            for name, value in cleaned.items():
                setattr(new, name, value)
            self._config = new
            if save:
                self._save_locked()
        return new

    def save(self) -> None:
        with self._lock:
            self._save_locked()

    def _save_locked(self) -> None:
        atomic_write_json(self.path, self._config.to_dict())
        log.info("Saved config to %s", self.path)
