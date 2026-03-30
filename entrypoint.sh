#!/bin/bash
# -e: exit on error | -x: print commands for debugging
set -ex

# --- 0. Configuration & Defaults ---
# If INIT_VOL is not set or empty, default to 1.0
VOLUME_SETTING="${INIT_VOL:-1.0}"

# Simple log helper to match your style
log() {
    echo "[$(date +'%Y-%m-%d %H:%M:%S')] [$1] $2"
}

log "INFO" "--- 🔊 Starting Squeezelite Environment ---"

# 1. Environment Validation
if [ ! -S "$PIPEWIRE_RUNTIME_DIR/$PIPEWIRE_REMOTE" ]; then
    log "WARN" "PipeWire socket not found at $PIPEWIRE_RUNTIME_DIR/$PIPEWIRE_REMOTE. Volume control may fail."
fi

# 2. Targeted Volume Initialization
# This regex finds the line under your PLAYER_NAME that points to the hardware
TARGET_ID=$(wpctl status | grep -A 20 "Sinks:" | grep "${PLAYER_NAME}" | grep -oE '[0-9]+' | head -n 1)

if [ -n "$TARGET_ID" ]; then
    log "INFO" "Found Sink ID: $TARGET_ID. Setting volume to $VOLUME_SETTING"
    wpctl set-mute "$TARGET_ID" 0
    wpctl set-volume "$TARGET_ID" "$VOLUME_SETTING"
else
    log "WARN" "Could not trace stream $PLAYER_NAME to a hardware sink. Using default."
    wpctl set-volume @DEFAULT_AUDIO_SINK@ "$VOLUME_SETTING" || true
fi


# 3. List available output devices for Squeezelite logs
log "INFO" "--- Squeezelite Device List ---"
/usr/local/bin/squeezelite -l | grep -A 50 "Output devices" || true

# 4. Launch Squeezelite (PID 1)
log "INFO" "--- Launching Squeezelite ---"
exec /usr/local/bin/squeezelite \
    -o pipewire \
    -n "${PLAYER_NAME}" \
    -s "${SERVER_IP}" \
    -m "${MAC_ADDR}" \
    -d all=info \
    ${SQUEEZE_EXTRA}