"""Supervisor behaviour, driven by tests/fake_squeezelite.py."""
import json
import os
import time

import pytest

import players
from players import PlayerError


def wait_for(predicate, timeout=8.0, interval=0.02):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return None


def running(player):
    return wait_for(lambda: player.state == "running" and player._proc is not None)


BASE = {"name": "Lounge", "node": "alsa_output.usb-Topping_DX5", "server": "192.168.1.50"}


# ---- argv -------------------------------------------------------------------


def test_argv_carries_every_knob():
    argv = players.build_argv(
        players.validate(
            dict(
                BASE,
                mac="02:11:22:33:44:55",
                port=3483,
                mixer="Master",
                alsa_buffer=16384,
                alsa_period=8,
                alsa_format="24",
                alsa_mmap=0,
                stream_buffer=8000,
                output_buffer=12000,
                close_delay=5,
                dsd=True,
            )
        )
    )
    assert argv[0].endswith("squeezelite") or argv[0].endswith("fake_squeezelite.py")
    assert argv[1:5] == ["-o", "pipewire", "-n", "Lounge"]
    assert "-s" in argv and argv[argv.index("-s") + 1] == "192.168.1.50:3483"
    assert argv[argv.index("-m") + 1] == "02:11:22:33:44:55"
    assert argv[argv.index("-U") + 1] == "Master"
    assert argv[argv.index("-a") + 1] == "16384:8:24:0"
    assert argv[argv.index("-b") + 1] == "8000:12000"
    assert argv[argv.index("-C") + 1] == "5"
    assert "-D" in argv


def test_blank_server_means_discovery():
    argv = players.build_argv(players.validate({"name": "Lounge", "server": ""}))
    assert "-s" not in argv


def test_blank_mixer_leaves_volume_in_software():
    argv = players.build_argv(players.validate({"name": "Lounge", "mixer": ""}))
    assert "-U" not in argv


def test_names_are_one_argument_not_a_shell_string(supervisor):
    player = supervisor.create(dict(BASE, name="Kitchen; rm -rf /", autostart=False))
    argv = players.build_argv(player.config)
    assert "Kitchen; rm -rf /" in argv          # exactly one element
    assert not any(" rm " in a for a in argv if a != "Kitchen; rm -rf /")


def test_unicode_names_are_accepted(supervisor):
    player = supervisor.create(dict(BASE, name="Кухня · DX5", autostart=False))
    assert player.config["name"] == "Кухня · DX5"


@pytest.mark.parametrize("bad", ["with\nnewline", "with\ttab", "\x00null", ""])
def test_control_characters_in_names_are_rejected(bad):
    with pytest.raises(PlayerError):
        players.validate({"name": bad})


@pytest.mark.parametrize(
    "patch, message",
    [
        ({"port": 0}, "port"),
        ({"port": "abc"}, "port"),
        ({"volume": 5}, "volume"),
        ({"alsa_format": "17"}, "format"),
        ({"node": "bad node!"}, "node"),
        ({"server": "no spaces here"}, "server"),
        ({"mac": "not-a-mac"}, "MAC"),
        ({"pipewire_latency": "1024"}, "latency"),
        ({"extra": "unbalanced 'quote"}, "parseable"),
        ({"close_delay": 99999}, "idle close"),
    ],
)
def test_bad_definitions_are_rejected(patch, message):
    with pytest.raises(PlayerError) as caught:
        players.validate(dict(BASE, **patch))
    assert message.lower() in str(caught.value).lower()


def test_duplicate_names_are_rejected(supervisor):
    supervisor.create(dict(BASE, autostart=False))
    with pytest.raises(PlayerError):
        supervisor.create(dict(BASE, autostart=False))


# ---- MAC addresses ----------------------------------------------------------


def test_generated_macs_are_locally_administered_and_unicast():
    for _ in range(50):
        mac = players.random_mac()
        first = int(mac.split(":")[0], 16)
        assert first & 0x02, "%s is not locally administered" % mac
        assert not first & 0x01, "%s is multicast" % mac
        assert players.MAC_RE.match(mac)


def test_every_player_gets_its_own_mac(supervisor):
    made = [
        supervisor.create(dict(BASE, name="P%d" % n, autostart=False)) for n in range(6)
    ]
    macs = {p.config["mac"] for p in made}
    assert len(macs) == 6, "players must not share a MAC -- the server keys off it"


def test_an_explicit_mac_is_kept(supervisor):
    player = supervisor.create(dict(BASE, mac="02:aa:bb:cc:dd:ee", autostart=False))
    assert player.config["mac"] == "02:AA:BB:CC:DD:EE"


def test_a_duplicate_mac_is_rejected(supervisor):
    supervisor.create(dict(BASE, mac="02:11:22:33:44:55", autostart=False))
    with pytest.raises(PlayerError) as caught:
        supervisor.create(
            dict(BASE, name="Other", mac="02:11:22:33:44:55", autostart=False)
        )
    assert "MAC" in str(caught.value)


def test_macs_are_stable_across_a_reload(supervisor, tmp_path):
    player = supervisor.create(dict(BASE, autostart=False))
    mac = player.config["mac"]
    reloaded = players.Supervisor(config_path=str(tmp_path / "players.json"))
    assert [p["mac"] for p in reloaded.list()] == [mac]


# ---- lifecycle --------------------------------------------------------------


def test_start_launches_squeezelite_with_the_right_arguments(supervisor):
    player = supervisor.create(dict(BASE, autostart=False))
    player.start()
    assert running(player), player.logs
    assert wait_for(lambda: any("-n Lounge" in line for line in player.logs))


def test_multiple_players_run_concurrently_with_distinct_nodes(supervisor):
    made = []
    for n in range(3):
        made.append(
            supervisor.create(
                {
                    "name": "Room %d" % n,
                    "node": "alsa_output.dac%d" % n,
                    "server": "192.168.1.50",
                    "autostart": False,
                }
            )
        )
    for player in made:
        player.start()
    for n, player in enumerate(made):
        assert running(player), player.logs
        assert wait_for(
            lambda p=player, n=n: any(
                "PIPEWIRE_NODE=alsa_output.dac%d" % n in line for line in p.logs
            )
        ), list(player.logs)
    pids = {p._proc.pid for p in made}
    assert len(pids) == 3


def test_a_crashing_player_is_restarted(supervisor, monkeypatch):
    monkeypatch.setenv("FAKE_SQUEEZELITE_MODE", "crash")
    player = supervisor.create(dict(BASE, autostart=False))
    player.start()
    assert wait_for(lambda: player.restarts >= 2), list(player.logs)
    assert player.last_exit == 3


def test_stop_interrupts_the_backoff_promptly(supervisor, monkeypatch):
    monkeypatch.setenv("FAKE_SQUEEZELITE_MODE", "crash")
    monkeypatch.setattr(players, "RETRY_START", 30.0)
    monkeypatch.setattr(players, "RETRY_MAX", 30.0)
    player = supervisor.create(dict(BASE, autostart=False))
    player.start()
    assert wait_for(lambda: player.state == "backoff")
    began = time.time()
    player.stop()
    assert time.time() - began < 5.0, "stop waited out the backoff sleep"
    assert player.state == "stopped"


def test_stop_immediately_after_start_leaves_no_orphan(supervisor):
    """The window between 'decide to launch' and 'record the child'.

    A stop landing in there must still kill the process that is coming into
    existence, or a squeezelite is orphaned holding the sink forever.
    """
    for _ in range(15):
        player = supervisor.create(dict(BASE, name="Race", autostart=False))
        player.start()
        player.stop()
        assert player._proc is None or player._proc.poll() is not None
        assert not player.desired
        supervisor.delete(player.id)


def test_a_stopped_player_reports_a_clean_row(supervisor):
    player = supervisor.create(dict(BASE, autostart=False))
    player.start()
    assert running(player)
    player.stop()
    status = player.status()
    assert status["state"] == "stopped"
    assert status["running"] is False
    assert status["uptime"] == 0
    assert status["detail"] == ""


# ---- the sink watchdog ------------------------------------------------------


def test_player_restarts_when_its_sink_disappears(supervisor, fast):
    present = {"value": True}
    fast.setattr(players, "sink_present", lambda node: present["value"])
    player = supervisor.create(dict(BASE, autostart=False))
    player.start()
    assert running(player)
    first = player._proc.pid

    present["value"] = False
    assert wait_for(lambda: player._proc is None or player._proc.pid != first), list(
        player.logs
    )
    assert wait_for(lambda: player.state == "waiting"), list(player.logs)

    present["value"] = True
    assert wait_for(lambda: player.state == "running" and player._proc.pid != first)


def test_a_momentary_sink_blip_does_not_restart_the_player(supervisor, fast):
    present = {"value": True}
    fast.setattr(players, "sink_present", lambda node: present["value"])
    player = supervisor.create(dict(BASE, autostart=False))
    player.start()
    assert running(player)
    first = player._proc.pid

    present["value"] = False          # shorter than SINK_GRACE: a rate switch
    time.sleep(0.1)
    present["value"] = True
    time.sleep(0.6)
    assert player._proc.pid == first, "a blip must not count as a failure"
    assert player.state == "running"


def test_a_player_with_no_node_is_not_watchdogged(supervisor, fast):
    fast.setattr(players, "sink_present", lambda node: False)
    player = supervisor.create(dict(BASE, node="", autostart=False))
    player.start()
    assert running(player)
    time.sleep(0.6)
    assert player.state == "running"


# ---- persistence ------------------------------------------------------------


def test_every_configured_player_is_written_to_the_config(supervisor, tmp_path):
    for n in range(3):
        supervisor.create(dict(BASE, name="Room %d" % n, autostart=False))
    stored = json.loads((tmp_path / "players.json").read_text())
    assert [p["name"] for p in stored["players"]] == ["Room 0", "Room 1", "Room 2"]
    # Every field of every player, not just the identifying ones.
    for entry in stored["players"]:
        assert set(players.DEFAULTS) <= set(entry)
        assert entry["id"]


def test_players_survive_a_restart(supervisor, tmp_path):
    supervisor.create(
        dict(BASE, alsa_format="32", close_delay=9, dsd=True, autostart=False)
    )
    reloaded = players.Supervisor(config_path=str(tmp_path / "players.json"))
    restored = reloaded.list()
    assert len(restored) == 1
    assert restored[0]["name"] == "Lounge"
    assert restored[0]["alsa_format"] == "32"
    assert restored[0]["close_delay"] == 9
    assert restored[0]["dsd"] is True
    assert restored[0]["node"] == BASE["node"]


def test_edits_and_deletes_are_persisted(supervisor, tmp_path):
    player = supervisor.create(dict(BASE, autostart=False))
    supervisor.update(player.id, {"name": "Renamed", "alsa_buffer": 4096})
    stored = json.loads((tmp_path / "players.json").read_text())["players"]
    assert stored[0]["name"] == "Renamed" and stored[0]["alsa_buffer"] == 4096

    supervisor.delete(player.id)
    assert json.loads((tmp_path / "players.json").read_text())["players"] == []


def test_the_config_is_replaced_atomically(supervisor, tmp_path):
    supervisor.create(dict(BASE, autostart=False))
    assert not (tmp_path / "players.json.tmp").exists(), "temp file left behind"


def test_an_unwritable_config_is_reported_not_swallowed(tmp_path, fast):
    # A regular file where the config directory should be. Unlike chmod 0500
    # this also fails for root, which is how CI runs.
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory")
    sup = players.Supervisor(config_path=str(blocker / "players.json"))
    try:
        sup.create(dict(BASE, autostart=False))
        assert sup.save_error, "a failed save must be visible to the user"
        assert "players.json" in sup.save_error
    finally:
        sup.stop_all()


def test_a_corrupt_config_does_not_stop_the_panel_booting(tmp_path):
    path = tmp_path / "players.json"
    path.write_text("{not json at all")
    assert players.Supervisor(config_path=str(path)).list() == []


def test_a_single_bad_entry_does_not_lose_the_good_ones(tmp_path):
    path = tmp_path / "players.json"
    path.write_text(
        json.dumps({"players": [{"name": ""}, {"name": "Good", "id": "abc123"}]})
    )
    restored = players.Supervisor(config_path=str(path)).list()
    assert [p["name"] for p in restored] == ["Good"]


# ---- CRUD -------------------------------------------------------------------


def test_delete_stops_and_forgets(supervisor):
    player = supervisor.create(dict(BASE, autostart=False))
    player.start()
    assert running(player)
    proc = player._proc
    supervisor.delete(player.id)
    assert proc.poll() is not None, "delete must not leave the process running"
    assert supervisor.list() == []


def test_update_rebinds_a_running_player(supervisor):
    player = supervisor.create(dict(BASE, autostart=False))
    player.start()
    assert running(player)
    supervisor.update(player.id, {"node": "alsa_output.other"})
    assert running(player)
    assert wait_for(
        lambda: any("PIPEWIRE_NODE=alsa_output.other" in line for line in player.logs)
    ), list(player.logs)


def test_autostart_only_starts_the_flagged_ones(supervisor):
    auto = supervisor.create(dict(BASE, name="Auto", autostart=True))
    manual = supervisor.create(dict(BASE, name="Manual", autostart=False))
    supervisor.autostart()
    assert running(auto)
    assert manual.state == "stopped"
