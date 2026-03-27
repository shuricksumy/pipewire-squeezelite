#!/bin/bash
# -e: exit on error | -x: print commands for debugging
set -ex

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
TARGET_SINK_NAME=$(wpctl status | grep -A 5 "Sinks:" | grep "${PLAYER_NAME}" | grep -oE '[0-9]+' | head -n 1)

if [ -n "$TARGET_SINK_NAME" ]; then
    log "INFO" "Detected output hardware: $TARGET_SINK_NAME"
    
    # Now find the numeric ID of that Sink in the Sinks section
    TARGET_ID=$(wpctl status | grep -A 20 "Sinks:" | grep "$TARGET_SINK_NAME" | grep -oE '^[[:space:]]*[0-9]{1,3}' | tr -d ' ' | head -n 1)
    
    if [ -n "$TARGET_ID" ]; then
        log "INFO" "Found Sink ID: $TARGET_ID. Setting volume to 1.0"
        wpctl set-volume "$TARGET_ID" 1.0
    fi
else
    log "WARN" "Could not trace stream $PLAYER_NAME to a hardware sink. Using default."
    wpctl set-volume @DEFAULT_AUDIO_SINK@ 1.0 || true
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
    -U Master \
    -d all=info \
    ${SQUEEZE_EXTRA}