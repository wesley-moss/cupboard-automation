"""Web page and JSON API (Flask), served from the same process as the GPIO code."""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, Any, Optional

from flask import Flask, jsonify, request

from .config import SETTING_LIMITS, ConfigError
from .door import Result

if TYPE_CHECKING:
    from .main import System

log = logging.getLogger(__name__)

MAX_JOG_STEPS = 10_000


def _reply(result: Result) -> Any:
    code = 202 if result.started else (200 if result.ok else 409)
    return jsonify(result.to_dict()), code


def _body() -> dict[str, Any]:
    data = request.get_json(silent=True)
    if isinstance(data, dict):
        return data
    return dict(request.form) if request.form else {}


def create_app(system: "System") -> Flask:
    app = Flask(__name__, static_folder="static")
    door = system.door

    @app.get("/")
    def index() -> Any:
        return app.send_static_file("index.html")

    @app.get("/api/status")
    def status() -> Any:
        data = door.status()
        data.update(system.temp_monitor.status())
        data["simulate"] = system.simulate
        return jsonify(data)

    @app.post("/api/open")
    def open_door() -> Any:
        return _reply(door.request("open", source="Web"))

    @app.post("/api/close")
    def close_door() -> Any:
        return _reply(door.request("close", source="Web"))

    @app.post("/api/toggle")
    def toggle_door() -> Any:
        return _reply(door.request("toggle", source="Web"))

    @app.post("/api/jog")
    def jog() -> Any:
        raw = _body().get("steps", request.args.get("steps"))
        try:
            steps = int(raw)
        except (TypeError, ValueError):
            return jsonify({"ok": False, "message": "steps must be a whole number"}), 400
        if steps == 0 or abs(steps) > MAX_JOG_STEPS:
            return jsonify({"ok": False, "message": f"steps must be 1 to {MAX_JOG_STEPS}, + or -"}), 400
        return _reply(door.request("jog", source="Web", steps=steps))

    @app.post("/api/reset_fault")
    def reset_fault() -> Any:
        return _reply(door.reset_fault())

    @app.get("/api/config")
    def get_config() -> Any:
        return jsonify(
            {
                "settings": system.store.config.settings(),
                "limits": {k: {"min": lo, "max": hi, "type": t.__name__} for k, (t, lo, hi) in SETTING_LIMITS.items()},
                "path": system.store.path,
            }
        )

    @app.route("/api/config", methods=["POST", "PUT"])
    def set_config() -> Any:
        try:
            cfg = system.store.update(_body())
        except ConfigError as exc:
            return jsonify({"ok": False, "message": str(exc)}), 400
        except OSError as exc:
            log.error("Settings applied but could not be saved: %s", exc)
            return jsonify(
                {"ok": False, "message": f"Applied, but could not save the file: {exc}",
                 "settings": system.store.config.settings()}
            ), 500
        log.info("Settings changed from the web page: %s", cfg.settings())
        return jsonify({"ok": True, "message": "Saved", "settings": cfg.settings()})

    # Simulator only: pretend to pull the book or change the temperature.
    if system.simulate and system.sim_hw is not None and system.fake_sensor is not None:
        sim_hw, fake = system.sim_hw, system.fake_sensor

        @app.post("/api/sim/book_pull")
        def sim_book_pull() -> Any:
            sim_hw.pull_book()
            return jsonify({"ok": True, "message": "Book pulled (simulated)"})

        @app.post("/api/sim/temperature")
        def sim_temperature() -> Any:
            body = _body()
            try:
                if "delta" in body:
                    fake.temperature = round(fake.temperature + float(body["delta"]), 2)
                else:
                    fake.temperature = float(body["temperature_c"])
            except (KeyError, TypeError, ValueError):
                return jsonify({"ok": False, "message": "send temperature_c or delta"}), 400
            return jsonify({"ok": True, "temperature_c": fake.temperature})

        @app.post("/api/sim/sensor")
        def sim_sensor() -> Any:
            fake.missing = bool(_body().get("missing", False))
            return jsonify({"ok": True, "missing": fake.missing})

        @app.post("/api/sim/switch_broken")
        def sim_switch_broken() -> Any:
            sim_hw.set_switch_broken(bool(_body().get("broken", False)))
            return jsonify({"ok": True, "broken": sim_hw.switch_broken})

    return app


class WebServer(threading.Thread):
    """Runs the Flask app on 0.0.0.0 (every network interface).

    Binding to 0.0.0.0 works even before the network is up. If the port is
    busy the server retries every 5 s instead of crashing.
    """

    def __init__(self, app: Flask, port: int, host: str = "0.0.0.0") -> None:
        super().__init__(name="web-server", daemon=True)
        self.app = app
        self.port = port
        self.host = host
        self._server: Optional[Any] = None
        self._stop_event = threading.Event()
        # Werkzeug logs every request at INFO; the page polls every second, which
        # would flood the journal and wear the SD card.
        logging.getLogger("werkzeug").setLevel(logging.WARNING)

    def run(self) -> None:
        from werkzeug.serving import make_server

        while not self._stop_event.is_set():
            try:
                self._server = make_server(self.host, self.port, self.app, threaded=True)
                break
            except OSError as exc:
                log.warning("Web server could not start on port %d (%s), retrying in 5 s", self.port, exc)
                self._stop_event.wait(5.0)
        if self._server is None or self._stop_event.is_set():
            return
        log.info("Web page at http://<this-pi>:%d/", self.port)
        self._server.serve_forever()

    def stop(self) -> None:
        self._stop_event.set()
        if self._server is not None:
            self._server.shutdown()
