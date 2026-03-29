#!/bin/bash

# --- CONFIGURATION ---
TARGET_USER="dietpi"  # Set your desired user here
HOME_DIR="/home/$TARGET_USER"

echo ">>> Starting Audiophile Environment Setup for user: $TARGET_USER"

# --- 0. INSTALLATION ---
echo ">>> Installing PipeWire and Audiophile Tools..."
sudo apt update
sudo apt install -y pipewire pipewire-audio-client-libraries \
    wireplumber pipewire-pulse alsa-utils pulseaudio-utils rtkit-daemon

# --- 1. USER SETUP ---
# Check if user exists; if not, create.
if id "$TARGET_USER" &>/dev/null; then
    echo ">>> User '$TARGET_USER' already exists. Updating groups..."
else
    echo ">>> Creating dedicated audio user '$TARGET_USER'..."
    sudo useradd -m -s /bin/bash "$TARGET_USER"
fi

# Add to necessary groups for Real-Time and Hardware access
sudo usermod -aG audio,video,rtkit,bluetooth,lp,pulse-access,render,docker "$TARGET_USER"

# Enable 'lingering' so PipeWire stays alive after logout
echo ">>> Enabling service lingering for '$TARGET_USER'..."
sudo loginctl enable-linger "$TARGET_USER"

# Get the UID for pathing
USER_UID=$(id -u "$TARGET_USER")

# --- 2. ENVIRONMENT CONFIG ---
# Ensure runtime directory exists for headless systemd access
sudo mkdir -p "/run/user/$USER_UID"
sudo chown "$TARGET_USER:$TARGET_USER" "/run/user/$USER_UID"

# Add XDG_RUNTIME_DIR to .bashrc if not already there
if ! sudo -u "$TARGET_USER" grep -q "XDG_RUNTIME_DIR" "$HOME_DIR/.bashrc"; then
    echo "export XDG_RUNTIME_DIR=/run/user/\$(id -u)" | sudo -u "$TARGET_USER" tee -a "$HOME_DIR/.bashrc"
fi

# --- 3. BIT-PERFECT CONFIGURATION ---
echo ">>> Applying Audiophile Bit-Perfect Config..."
CONF_DIR="$HOME_DIR/.config/pipewire/pipewire.conf.d"
sudo -u "$TARGET_USER" mkdir -p "$CONF_DIR"

# Writing the config using your specific structure
sudo -u "$TARGET_USER" tee "$CONF_DIR/bitperfect.conf" <<EOF
context.properties = {
    ## Default when no audio is playing
    default.clock.rate          = 48000
    ## The rates the hardware is allowed to switch to
    default.clock.allowed-rates = [ 44100 48000 88200 96000 176400 192000 352800 384000 ]
    
    default.clock.min-quantum   = 32
    default.clock.max-quantum   = 8192
}

# Placed OUTSIDE context.properties for correct scope
stream.properties = {
    resample.quality      = 14
    channelmix.normalize  = false
    channelmix.mix-lfe    = false
}
EOF

# --- 4. START SERVICES ---
echo ">>> Starting PipeWire Services for $TARGET_USER..."
# Execute via sudo as the target user, pointing to the correct runtime bus
export RUN_CMD="sudo -u $TARGET_USER XDG_RUNTIME_DIR=/run/user/$USER_UID"

$RUN_CMD systemctl --user daemon-reload
$RUN_CMD systemctl --user enable --now pipewire pipewire-pulse wireplumber

echo "--------------------------------------------------------"
echo "  SETUP COMPLETE! DX3/DX5 is ready for Hi-Fi.          "
echo "  Target User: $TARGET_USER (UID: $USER_UID)           "
echo "--------------------------------------------------------"

# --- 5. USEFUL DIAGNOSTIC COMMANDS ---
cat <<EOF

--- AUDIO STATION TOOLKIT ---

To run these commands as $TARGET_USER, use: 
sudo -u $TARGET_USER XDG_RUNTIME_DIR=/run/user/$USER_UID [command]

Check Hardware Level Clock (The Truth):
  cat /proc/asound/card*/pcm0p/sub0/hw_params

Monitor Sample Rate & Bit-Depth:
  pw-top

Check Device Status:
  wpctl status

EOF