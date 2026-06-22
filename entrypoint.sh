#!/bin/bash
set -x

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
