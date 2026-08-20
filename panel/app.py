#!/usr/bin/env python3
"""squeezelite-web -- a small panel for running squeezelite players.

Replaces the "edit docker-compose, redeploy, SSH in to change a buffer size"
loop: create a player against a PipeWire sink from a browser, start and stop it,
watch its log, and let a supervisor keep it alive.

Deliberately small: no database, no build step, no websockets. The browser polls
/api/players and every action is a POST that returns the refreshed list.
"""

import atexit
import hmac
import logging
import os
import resource
import shutil
import sys

from flask import Flask, jsonify, request, send_from_directory

import players as players_mod
from players import PlayerError, Supervisor

log = logging.getLogger("squeezelite-web")

app = Flask(__name__, static_folder="static", static_url_path="")

ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")

PORT = int(os.environ.get("PORT", "8080"))

supervisor = Supervisor()


@app.before_request
def require_auth():
    """Gate everything -- API and the page itself -- when ADMIN_PASSWORD is set.

    Unset (the default) means no auth at all, which is why the README is
    explicit that this belongs on a trusted LAN and not on a port-forward.
    """
    if not ADMIN_PASSWORD:
        return None
    auth = request.authorization
    if (
        auth
        and auth.type == "basic"
        and hmac.compare_digest(auth.username or "", ADMIN_USER)
        and hmac.compare_digest(auth.password or "", ADMIN_PASSWORD)
    ):
        return None
    return (
        jsonify(error="authentication required"),
        401,
        {"WWW-Authenticate": 'Basic realm="squeezelite-web"'},
    )


# ---- error mapping ----------------------------------------------------------


@app.errorhandler(PlayerError)
def handle_player_error(exc):
    return jsonify(ok=False, error=str(exc)), getattr(exc, "status", 400)


# ---- environment warnings ---------------------------------------------------


def environment_warnings():
    """Things about this container that will quietly ruin the audio.

    Real-time scheduling is a container-level property: players are children of
    this process, so they inherit the panel container's limits. Without them a
    192 kHz stream drops out under load and nothing in squeezelite's own log
    says why -- so say it here instead.
    """
    warnings = []

    try:
        rtprio_soft, _ = resource.getrlimit(resource.RLIMIT_RTPRIO)
    except (AttributeError, OSError, ValueError):
        rtprio_soft = 0
    if rtprio_soft < 1:
        warnings.append(
            "Real-time scheduling is unavailable (RLIMIT_RTPRIO=%s). Players will "
            "run at ordinary priority and may drop out at high sample rates. Add "
            "cap_add: [SYS_NICE, IPC_LOCK] and ulimits: {rtprio: 95, memlock: -1} "
            "to this container in compose." % rtprio_soft
        )

    try:
        memlock_soft, _ = resource.getrlimit(resource.RLIMIT_MEMLOCK)
    except (AttributeError, OSError, ValueError):
        memlock_soft = 0
    if memlock_soft != resource.RLIM_INFINITY and memlock_soft < 64 * 1024 * 1024:
        warnings.append(
            "Locked-memory limit is low (RLIMIT_MEMLOCK=%s). Set ulimits: "
            "{memlock: -1} so PipeWire can lock its buffers." % memlock_soft
        )

    socket = os.path.join(
        os.environ.get("PIPEWIRE_RUNTIME_DIR", "/tmp"),
        os.environ.get("PIPEWIRE_REMOTE", "pipewire-0"),
    )
    if not os.path.exists(socket):
        warnings.append(
            "No PipeWire socket at %s. Bind-mount the host's, e.g. "
            "'/run/user/1000/pipewire-0:/tmp/pipewire-0'." % socket
        )
    elif not os.access(socket, os.W_OK):
        warnings.append(
            "The PipeWire socket %s is not writable by uid %s. It is owned by the "
            "host desktop user -- match it with 'user:' in compose."
            % (socket, os.getuid())
        )

    if supervisor.save_error:
        warnings.append(
            "Players cannot be saved (%s). They will be lost when this container "
            "restarts. Mount a writable volume at /config, e.g. "
            "'./panel_config:/config', and chown it to the uid this runs as."
            % supervisor.save_error
        )

    config_dir = os.path.dirname(supervisor.config_path) or "."
    if not os.path.exists(supervisor.config_path) and not os.access(config_dir, os.W_OK):
        warnings.append(
            "%s is not writable by uid %s, so players will not survive a restart. "
            "Mount a writable volume at /config and chown it to this uid."
            % (config_dir, os.getuid())
        )

    devices = players_mod.list_alsa_devices()
    if devices and not any(d["hardware"] for d in devices):
        warnings.append(
            "No ALSA hardware devices are visible, only conversion plugins. The "
            "ALSA output mode needs the sound devices passed through as devices, "
            "not as a volume: 'devices: [/dev/snd:/dev/snd]' in compose. "
            "(-v /dev/snd mounts the nodes but the device cgroup still blocks "
            "opening them.) PipeWire output is unaffected."
        )

    if not shutil.which(players_mod.SQUEEZELITE):
        warnings.append(
            "squeezelite was not found on PATH (%s)." % players_mod.SQUEEZELITE
        )

    return warnings


# ---- routes -----------------------------------------------------------------


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/api/config")
def api_config():
    return jsonify(
        auth=bool(ADMIN_PASSWORD),
        warnings=environment_warnings(),
        defaults=supervisor.new_player_defaults(),
        formats=list(players_mod.ALSA_FORMATS),
    )


@app.get("/api/players")
def api_players():
    return jsonify(players=supervisor.list())


@app.get("/api/sinks")
def api_sinks():
    """PipeWire sinks available right now -- what a player can be bound to."""
    return jsonify(sinks=players_mod.list_sinks())


@app.get("/api/alsa-devices")
def api_alsa_devices():
    """ALSA outputs squeezelite can address directly, for hosts without a
    working PipeWire session."""
    return jsonify(devices=players_mod.list_alsa_devices())


@app.post("/api/players")
def api_create_player():
    player = supervisor.create(request.get_json(silent=True) or {})
    return jsonify(ok=True, player=player.status(), players=supervisor.list()), 201


@app.patch("/api/players/<player_id>")
def api_update_player(player_id):
    player = supervisor.update(player_id, request.get_json(silent=True) or {})
    return jsonify(ok=True, player=player.status(), players=supervisor.list())


@app.delete("/api/players/<player_id>")
def api_delete_player(player_id):
    supervisor.delete(player_id)
    return jsonify(ok=True, players=supervisor.list())


@app.post("/api/players/<player_id>/<action>")
def api_player_action(player_id, action):
    if action not in ("start", "stop", "restart"):
        return jsonify(ok=False, error="unknown action"), 404
    player = supervisor.get(player_id)
    if action in ("stop", "restart"):
        player.stop()
    if action in ("start", "restart"):
        player.start()
    return jsonify(ok=True, players=supervisor.list())


@app.get("/api/players/<player_id>/logs")
def api_player_logs(player_id):
    player = supervisor.get(player_id)
    return jsonify(
        id=player.id, name=player.config["name"], lines=list(player.logs)
    )


def main():
    logging.basicConfig(
        level=logging.INFO, format="[%(asctime)s] [%(levelname)s] %(message)s"
    )
    if not ADMIN_PASSWORD:
        log.warning(
            "ADMIN_PASSWORD is not set -- every route is open to anyone who can "
            "reach this port. Keep it on a trusted LAN."
        )
    for warning in environment_warnings():
        log.warning(warning)

    atexit.register(supervisor.stop_all)
    supervisor.autostart()
    log.info("panel listening on 0.0.0.0:%s", PORT)
    app.run(host="0.0.0.0", port=PORT, threaded=True)


if __name__ == "__main__":
    sys.exit(main())
