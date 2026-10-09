import json
import time

import pytest

from cupboard_door.web import create_app


@pytest.fixture
def client(make_system):
    system = make_system()
    app = create_app(system)
    app.config["TESTING"] = True
    client = app.test_client()
    client.system = system
    return client


def wait_idle(client):
    assert client.system.door.wait_idle(5)


def test_index_page(client):
    r = client.get("/")
    assert r.status_code == 200 and b"Cupboard Door" in r.data


def test_status(client):
    data = client.get("/api/status").get_json()
    assert data["state"] == "closed"
    assert data["fault"] is False
    assert data["simulate"] is True
    assert "temperature_c" in data


def test_open_close_toggle(client):
    assert client.post("/api/open").status_code == 202
    wait_idle(client)
    assert client.get("/api/status").get_json()["state"] == "open"
    assert client.post("/api/open").status_code == 200  # already open
    assert client.post("/api/toggle").status_code == 202
    wait_idle(client)
    assert client.get("/api/status").get_json()["state"] == "closed"


def test_jog_validation(client):
    assert client.post("/api/jog", json={"steps": "abc"}).status_code == 400
    assert client.post("/api/jog", json={"steps": 0}).status_code == 400
    assert client.post("/api/jog", json={"steps": 50}).status_code == 202
    wait_idle(client)
    assert client.get("/api/status").get_json()["position"] == 50


def test_config_get_and_set(client):
    data = client.get("/api/config").get_json()
    assert data["settings"]["steps_to_open"] == 200
    r = client.post("/api/config", json={"speed_pps": 9000, "temp_window_s": 120})
    assert r.status_code == 200
    assert client.system.store.config.speed_pps == 9000  # applied live
    with open(client.system.store.path) as handle:
        assert json.load(handle)["temp_window_s"] == 120  # and saved


def test_config_rejects_bad_values(client):
    r = client.post("/api/config", json={"speed_pps": -1})
    assert r.status_code == 400
    assert "speed_pps" in r.get_json()["message"]


def test_fault_and_reset_via_api(client):
    client.post("/api/open")
    wait_idle(client)
    client.system.sim_hw.set_switch_broken(True)
    client.post("/api/close")
    wait_idle(client)
    status = client.get("/api/status").get_json()
    assert status["fault"] and status["state"] == "unknown"
    assert client.post("/api/open").status_code == 409
    client.system.sim_hw.set_switch_broken(False)
    assert client.post("/api/reset_fault").status_code == 200


def test_sim_book_pull(client):
    client.system.book_monitor.start()
    try:
        client.post("/api/sim/book_pull")
        deadline = time.time() + 5
        while client.get("/api/status").get_json()["state"] != "open" and time.time() < deadline:
            time.sleep(0.02)
        assert client.get("/api/status").get_json()["state"] == "open"
    finally:
        client.system.book_monitor.stop()


def test_sim_temperature(client):
    r = client.post("/api/sim/temperature", json={"delta": 2})
    assert r.get_json()["temperature_c"] == 23.0
