"""End-to-end tests against the Flask app, driving the fake squeezelite.

Nothing here needs PipeWire, a DAC or root, so it runs in CI.
"""
import base64
import importlib
import json
import os
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAKE = os.path.join(ROOT, "tests", "fake_squeezelite.py")


def load_app(tmp_path, **env):
    """Import a fresh app whose config and squeezelite live in tmp_path."""
    for key in ("ADMIN_PASSWORD", "ADMIN_USER", "SERVER_IP", "SERVER_PORT"):
        os.environ.pop(key, None)
    os.environ["CONFIG_DIR"] = str(tmp_path)
    os.environ["SQUEEZELITE"] = FAKE
    os.environ.update(env)

    # players reads its defaults at import time, so it has to be reloaded too or
    # env-driven settings silently keep the previous values.
    for name in ("app", "players"):
        sys.modules.pop(name, None)
    players = importlib.import_module("players")
    players.sink_present = lambda node: True
    players.list_sinks = lambda: [
        {"id": 51, "node": "alsa_output.dx5", "description": "Topping DX5", "rate": 48000}
    ]
    players.set_sink_volume = lambda node, volume: None
    players.RETRY_START = 0.05
    players.NODE_WAIT_SECONDS = 0.5
    return importlib.import_module("app")


@pytest.fixture
def app_module(tmp_path):
    module = load_app(tmp_path)
    yield module
    module.supervisor.stop_all()


@pytest.fixture
def client(app_module):
    return app_module.app.test_client()


NEW = {"name": "Lounge", "node": "alsa_output.dx5", "server": "192.168.1.50"}


def wait_for(predicate, timeout=8.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


# ---- basics -----------------------------------------------------------------


def test_index_page_is_served(client):
    res = client.get("/")
    assert res.status_code == 200
    assert b"Squeezelite Players" in res.data


def test_sinks_come_from_pipewire(client):
    body = client.get("/api/sinks").get_json()
    assert body["sinks"][0]["node"] == "alsa_output.dx5"


def test_config_exposes_defaults_and_formats(client):
    body = client.get("/api/config").get_json()
    assert body["auth"] is False
    assert body["defaults"]["port"] == 3483
    assert "24" in body["formats"]


def test_the_page_never_hardcodes_absolute_api_paths():
    """Absolute /api/... breaks under Home Assistant Ingress, which serves the
    page from /api/hassio_ingress/<token>/ and strips that prefix."""
    page = open(os.path.join(ROOT, "panel", "static", "index.html")).read()
    assert 'document.baseURI' in page
    for bad in ('fetch("/api', "fetch('/api", 'href="/api', 'call("/api'):
        assert bad not in page, "%s escapes an Ingress prefix" % bad


# ---- CRUD -------------------------------------------------------------------


def test_create_list_and_delete(client):
    created = client.post("/api/players", json=dict(NEW, autostart=False))
    assert created.status_code == 201
    player = created.get_json()["player"]
    assert player["name"] == "Lounge"
    assert player["mac"], "a MAC must be generated"

    listed = client.get("/api/players").get_json()["players"]
    assert [p["name"] for p in listed] == ["Lounge"]
    assert listed[0]["node_present"] is True

    assert client.delete("/api/players/%s" % player["id"]).status_code == 200
    assert client.get("/api/players").get_json()["players"] == []


def test_invalid_input_is_rejected_with_a_message(client):
    res = client.post("/api/players", json={"name": "Bad", "port": 0})
    assert res.status_code == 400
    assert "port" in res.get_json()["error"]


def test_unknown_player_is_a_400_not_a_crash(client):
    assert client.post("/api/players/nope/start").status_code == 400


def test_unknown_action_is_a_404(client):
    created = client.post("/api/players", json=dict(NEW, autostart=False)).get_json()
    res = client.post("/api/players/%s/explode" % created["player"]["id"])
    assert res.status_code == 404


def test_start_stop_and_logs(client, app_module):
    created = client.post("/api/players", json=dict(NEW, autostart=False)).get_json()
    player_id = created["player"]["id"]

    client.post("/api/players/%s/start" % player_id)
    player = app_module.supervisor.get(player_id)
    assert wait_for(lambda: player.state == "running")

    logs = client.get("/api/players/%s/logs" % player_id).get_json()
    assert any("-n Lounge" in line for line in logs["lines"]), logs

    client.post("/api/players/%s/stop" % player_id)
    listed = client.get("/api/players").get_json()["players"]
    assert listed[0]["running"] is False
    assert listed[0]["uptime"] == 0


def test_editing_a_player_persists_and_survives_reload(client, tmp_path):
    created = client.post("/api/players", json=dict(NEW, autostart=False)).get_json()
    player_id = created["player"]["id"]
    client.patch("/api/players/%s" % player_id, json={"alsa_format": "32"})

    stored = json.loads((tmp_path / "players.json").read_text())["players"]
    assert stored[0]["alsa_format"] == "32"

    reloaded = load_app(tmp_path)
    try:
        listed = reloaded.app.test_client().get("/api/players").get_json()["players"]
        assert listed[0]["alsa_format"] == "32"
        assert listed[0]["id"] == player_id
    finally:
        reloaded.supervisor.stop_all()


def test_autostart_players_come_up_on_boot(tmp_path):
    first = load_app(tmp_path)
    try:
        first.app.test_client().post("/api/players", json=dict(NEW, autostart=True))
    finally:
        first.supervisor.stop_all()

    second = load_app(tmp_path)
    try:
        second.supervisor.autostart()
        player = second.supervisor.list()[0]
        target = second.supervisor.get(player["id"])
        assert wait_for(lambda: target.state == "running")
    finally:
        second.supervisor.stop_all()


# ---- auth -------------------------------------------------------------------


def test_no_password_means_no_auth(client):
    assert client.get("/api/players").status_code == 200


def test_admin_password_gates_every_route(tmp_path):
    module = load_app(tmp_path, ADMIN_PASSWORD="hunter2")
    try:
        client = module.app.test_client()
        assert client.get("/").status_code == 401
        assert client.get("/api/players").status_code == 401
        assert client.post("/api/players", json=NEW).status_code == 401

        token = base64.b64encode(b"admin:hunter2").decode()
        headers = {"Authorization": "Basic %s" % token}
        assert client.get("/api/players", headers=headers).status_code == 200

        wrong = base64.b64encode(b"admin:wrong").decode()
        assert client.get(
            "/api/players", headers={"Authorization": "Basic %s" % wrong}
        ).status_code == 401
    finally:
        module.supervisor.stop_all()


# ---- environment warnings ---------------------------------------------------


def test_missing_realtime_limits_are_surfaced(client, monkeypatch):
    import resource

    monkeypatch.setattr(
        resource, "getrlimit", lambda which: (0, 0)
    )
    warnings = client.get("/api/config").get_json()["warnings"]
    assert any("RLIMIT_RTPRIO" in w for w in warnings)
    assert any("SYS_NICE" in w for w in warnings)


def test_a_missing_pipewire_socket_is_surfaced(client, monkeypatch):
    monkeypatch.setenv("PIPEWIRE_RUNTIME_DIR", "/nonexistent")
    warnings = client.get("/api/config").get_json()["warnings"]
    assert any("PipeWire socket" in w for w in warnings)
