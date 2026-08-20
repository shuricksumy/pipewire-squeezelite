#!/bin/bash
# Squeezelite launcher. One image, two roles:
#   ROLE=panel  - the web panel, which supervises several players itself.
#                 This is the default: with nothing configured, the useful thing
#                 to do is offer a UI rather than fail for want of a SERVER_IP.
#   ROLE=player - one supervised squeezelite, configured entirely by environment.
set -x

ROLE_GIVEN="${ROLE:-}"
ROLE="${ROLE:-panel}"

# Player settings with no ROLE is almost always a compose file written before
# the panel existed. It is not overridden -- an explicit default beats a clever
# one -- but it is worth saying out loud, because the symptom is silence.
if [ -z "${ROLE_GIVEN:-}" ] && [ "$ROLE" = "panel" ] \
   && [ -n "${PLAYER_NAME:-}${SERVER_IP:-}${MAC_ADDR:-}${SQUEEZE_EXTRA:-}" ]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] [WARN] This container has player settings (PLAYER_NAME/SERVER_IP/...)"
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] [WARN] but no ROLE, so it is starting the web panel and those are"
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] [WARN] ignored. Set ROLE=player for the single-player behaviour."
fi

if [ "$ROLE" = "panel" ]; then
    set +x
    # /config holds players.json. Without a writable one the panel still runs,
    # but every player vanishes on restart -- so say so loudly here as well as
    # in the UI, where it shows up as a warning banner.
    if ! mkdir -p /config 2>/dev/null || [ ! -w /config ]; then
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] [WARN] /config is not writable by uid $(id -u)."
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] [WARN] Players will not survive a restart. Fix with:"
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] [WARN]     sudo chown -R 1000:1000 ./panel_config"
    fi
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] [INFO] Starting the web panel on port ${PORT:-8080}"
    cd /opt/panel || exit 1
    exec python3 app.py
fi

if [ "$ROLE" != "player" ]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] [ERROR] Unknown ROLE '$ROLE' (expected 'player' or 'panel')."
    exit 1
fi

log() { echo "[$(date +'%Y-%m-%d %H:%M:%S')] [$1] $2"; }

log "INFO" "--- Starting Squeezelite Environment ---"


VOLUME_SETTING="${INIT_VOL:-1.0}"

# 1. PipeWire socket check
if [ ! -S "${PIPEWIRE_RUNTIME_DIR}/${PIPEWIRE_REMOTE}" ]; then
    log "WARN" "PipeWire socket not found at ${PIPEWIRE_RUNTIME_DIR}/${PIPEWIRE_REMOTE}. Volume control may fail."
fi

# 2. Volume init — once at container start
TARGET_ID=$(wpctl status | grep -A 20 "Sinks:" | grep "${PLAYER_NAME}" | grep -oE '[0-9]+' | head -n 1)
if [ -n "$TARGET_ID" ]; then
    log "INFO" "Found Sink ID: $TARGET_ID. Setting volume to $VOLUME_SETTING"
    wpctl set-mute "$TARGET_ID" 0
    wpctl set-volume "$TARGET_ID" "$VOLUME_SETTING"
else
    log "WARN" "Could not find sink '$PLAYER_NAME'. Using default sink."
    wpctl set-volume @DEFAULT_AUDIO_SINK@ "$VOLUME_SETTING" || true
fi

# 3. Device list — once for logs
log "INFO" "--- Squeezelite Device List ---"
/usr/local/bin/squeezelite -l | grep -A 50 "Output devices" || true

# 4. Reconnect loop with exponential backoff
RETRY_DELAY=5
MAX_DELAY=60

while true; do
    log "INFO" "--- Launching Squeezelite → ${SERVER_IP} ---"
    /usr/local/bin/squeezelite \
        -o pipewire \
        -n "${PLAYER_NAME}" \
        -s "${SERVER_IP}" \
        -m "${MAC_ADDR}" \
        -d all=info \
        ${SQUEEZE_EXTRA} || true

    log "WARN" "Squeezelite exited. Retrying in ${RETRY_DELAY}s..."
    sleep "$RETRY_DELAY"
    RETRY_DELAY=$(( RETRY_DELAY * 2 > MAX_DELAY ? MAX_DELAY : RETRY_DELAY * 2 ))
done
