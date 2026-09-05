#!/bin/bash
# Prepare an Ubuntu/Debian host to feed the squeezelite container: PipeWire running
# under a lingering user session, bit-perfect clock config, and the socket the
# container bind-mounts at /run/user/<uid>/pipewire-0.
#
# Usage:  ./ubuntu-pipewire-install-on-host.sh [username]
#
# With no argument it sets up whoever owns uid 1000, which is what the container
# runs as and what the compose files bind-mount (/run/user/1000/pipewire-0). Pass
# a username to use a different account -- if it does not exist yet it is created,
# taking uid 1000 when that is still free.

set -euo pipefail

trap 'echo ">>> FAILED at line $LINENO. Nothing below this point ran." >&2' ERR

if [ "$(id -u)" -ne 0 ] && ! sudo -n true 2>/dev/null; then
    echo ">>> This script needs sudo. Run it as root or make sure sudo works." >&2
    exit 1
fi

TARGET_USER="${1:-${TARGET_USER:-}}"
if [ -n "$TARGET_USER" ]; then
    echo ">>> Starting Audiophile Environment Setup for user: $TARGET_USER"
else
    echo ">>> Starting Audiophile Environment Setup for the uid-1000 user"
fi

# --- 0. INSTALLATION ---
# Package names matter: apt aborts the WHOLE command on one unknown name, so a
# single wrong entry silently leaves the host with no audio stack at all.
#   rtkit          - real-time scheduling helper. NOT "rtkit-daemon"; no such package.
#   pipewire-audio - current name of the old "pipewire-audio-client-libraries".
#   pipewire-alsa  - lets plain ALSA apps on the host reach PipeWire.
echo ">>> Installing PipeWire and audiophile tools..."
sudo apt-get update
sudo apt-get install -y \
    pipewire pipewire-audio pipewire-pulse pipewire-alsa \
    wireplumber alsa-utils pulseaudio-utils rtkit

# --- 1. USER SETUP ---
if [ -n "$TARGET_USER" ]; then
    if id "$TARGET_USER" &>/dev/null; then
        echo ">>> User '$TARGET_USER' already exists. Updating groups..."
    else
        # Pin uid 1000 when it is free: the image runs as 1000, so matching it
        # means no 'user:' line is needed in compose.
        if getent passwd 1000 >/dev/null; then
            echo ">>> Creating audio user '$TARGET_USER' (uid 1000 is taken by $(getent passwd 1000 | cut -d: -f1))..."
            sudo useradd -m -s /bin/bash "$TARGET_USER"
        else
            echo ">>> Creating audio user '$TARGET_USER' with uid 1000..."
            sudo useradd -m -u 1000 -s /bin/bash "$TARGET_USER"
        fi
    fi
else
    # No argument: the uid-1000 account, whatever it is called on this box --
    # dietpi, ubuntu, pi, or your own login.
    # '|| true': getent exits non-zero when there is no uid 1000, and under
    # 'set -e' that would kill the script before the friendly message below.
    TARGET_USER="$(getent passwd 1000 | cut -d: -f1 || true)"
    if [ -z "$TARGET_USER" ]; then
        echo ">>> No user with uid 1000 on this host, and no username given." >&2
        echo ">>> Pass one to create it:  $0 <username>" >&2
        exit 1
    fi
    echo ">>> No username given; using uid 1000 -> '$TARGET_USER'"
fi

USER_UID="$(id -u "$TARGET_USER")"
USER_GID="$(id -g "$TARGET_USER")"
HOME_DIR="$(getent passwd "$TARGET_USER" | cut -d: -f6)"

# `usermod -aG a,b,c` is all-or-nothing: if any one group is missing it errors out
# and adds NONE of them. bluetooth/render/pulse-access/docker only exist once their
# package is installed, so add whichever are actually present.
echo ">>> Adding '$TARGET_USER' to the audio/hardware groups that exist..."
for g in audio video render bluetooth lp docker rtkit; do
    if getent group "$g" >/dev/null; then
        sudo usermod -aG "$g" "$TARGET_USER"
        echo "    + $g"
    else
        echo "    - $g (group does not exist, skipped)"
    fi
done

# Enable 'lingering' so PipeWire stays alive after logout. This is also what makes
# systemd-logind create and maintain /run/user/$USER_UID (a tmpfs it owns) -- do not
# mkdir that path by hand, logind manages it.
echo ">>> Enabling service lingering for '$TARGET_USER'..."
sudo loginctl enable-linger "$TARGET_USER"

# --- 2. ENVIRONMENT CONFIG ---
if [ -f "$HOME_DIR/.bashrc" ] && ! sudo grep -q "XDG_RUNTIME_DIR" "$HOME_DIR/.bashrc"; then
    echo 'export XDG_RUNTIME_DIR=/run/user/$(id -u)' | sudo tee -a "$HOME_DIR/.bashrc" >/dev/null
    echo ">>> XDG_RUNTIME_DIR added to $HOME_DIR/.bashrc"
fi

# --- 3. BIT-PERFECT CONFIGURATION ---
echo ">>> Applying bit-perfect PipeWire config..."
CONF_DIR="$HOME_DIR/.config/pipewire/pipewire.conf.d"
sudo -u "$TARGET_USER" mkdir -p "$CONF_DIR"

sudo -u "$TARGET_USER" tee "$CONF_DIR/bitperfect.conf" >/dev/null <<EOF
context.properties = {
    ## Rate used while nothing is playing; PipeWire switches on demand
    default.clock.rate          = 48000
    ## The rates the hardware is allowed to switch to -- trim to what your DAC supports
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
# `loginctl enable-linger` starts the user manager asynchronously; running
# `systemctl --user` too early fails with "Failed to connect to bus".
echo ">>> Waiting for the user session manager..."
for _ in $(seq 1 30); do
    [ -S "/run/user/$USER_UID/systemd/private" ] && break
    sleep 1
done
if [ ! -S "/run/user/$USER_UID/systemd/private" ]; then
    echo ">>> ERROR: /run/user/$USER_UID never appeared. Is systemd-logind running?" >&2
    exit 1
fi

run_as_user() {
    sudo -u "$TARGET_USER" env \
        XDG_RUNTIME_DIR="/run/user/$USER_UID" \
        DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$USER_UID/bus" \
        "$@"
}

echo ">>> Starting PipeWire services for $TARGET_USER..."
run_as_user systemctl --user daemon-reload
run_as_user systemctl --user enable --now \
    pipewire.socket pipewire.service pipewire-pulse.service wireplumber.service

# --- 5. VERIFY ---
# The socket below is exactly what the container bind-mounts; if it is missing,
# every other step was pointless.
SOCKET="/run/user/$USER_UID/pipewire-0"
echo ">>> Verifying..."
for _ in $(seq 1 15); do
    [ -S "$SOCKET" ] && break
    sleep 1
done
if [ ! -S "$SOCKET" ]; then
    echo ">>> ERROR: $SOCKET does not exist. PipeWire did not start correctly." >&2
    run_as_user systemctl --user --no-pager status pipewire wireplumber || true
    exit 1
fi
ls -l "$SOCKET"
run_as_user wpctl status || { echo ">>> ERROR: wpctl cannot reach PipeWire." >&2; exit 1; }

echo "--------------------------------------------------------"
echo "  SETUP COMPLETE - PipeWire is running and reachable.   "
echo "  Target User: $TARGET_USER (UID: $USER_UID)            "
echo "--------------------------------------------------------"

# --- 6. WHAT TO PUT IN DOCKER COMPOSE ---
cat <<EOF

--- COMPOSE SETTINGS FOR THIS HOST ---

  volumes:
    - $SOCKET:/tmp/pipewire-0
    - /dev/shm:/dev/shm
EOF

if [ "$USER_UID" != "1000" ] || [ "$USER_GID" != "1000" ]; then
cat <<EOF

  # The image runs as 1000:1000 by default, but '$TARGET_USER' is $USER_UID:$USER_GID,
  # so this line is REQUIRED or the container cannot open the socket above:
  user: "$USER_UID:$USER_GID"
EOF
else
cat <<EOF

  # '$TARGET_USER' is 1000:1000, which is what the image already runs as,
  # so no 'user:' line is needed.
EOF
fi

cat <<EOF

Pick PLAYER_NAME from the sink names in 'wpctl status' above, and PIPEWIRE_NODE from:
  sudo -u $TARGET_USER XDG_RUNTIME_DIR=/run/user/$USER_UID pw-cli ls Node | grep -E 'node.name|node.description'

SERVER_IP is your music server as <ip>:3483 -- Music Assistant (Squeezelite provider)
or Lyrion Music Server / LMS. MAC_ADDR must be unique per player: the server keys
each player's saved settings off it.

If you added groups to your own login user, log out and back in for them to apply.

--- AUDIO STATION TOOLKIT ---

Run these as $TARGET_USER with:
  sudo -u $TARGET_USER XDG_RUNTIME_DIR=/run/user/$USER_UID [command]

Check hardware level clock (the truth):
  cat /proc/asound/card*/pcm0p/sub0/hw_params

Monitor sample rate & bit-depth:
  pw-top

Check device status:
  wpctl status

EOF
