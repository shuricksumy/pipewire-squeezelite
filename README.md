# Squeezelite-PipeWire (Hi-Fi Edition)

This repository provides a high-performance Squeezelite Docker container optimized for PipeWire and bit-perfect audio delivery. It is specifically pre-configured for high-end DACs like the Topping DX5, supporting sample rates up to 384kHz and DSD.


## Features

- Bit-Perfect Audio: Configured to switch sample rates (44.1k - 384k) automatically to match source material.

- PipeWire Native: Uses the modern PipeWire audio engine for low-latency routing and superior volume management.

- Multi-Arch Support: Builds for both amd64 (PC) and arm64 (Raspberry Pi).

- Automated Builds: GitHub Actions handle automated Docker builds and submodule updates.


## 🛠️ Host Setup (Preparation)

Before running the container, your host system must be configured for PipeWire bit-perfect output.

### Install PipeWire & Tools

Run the following on your host machine to install the necessary audio stack:


```bash
sudo apt update && sudo apt install -y pipewire pipewire-audio-client-libraries \
    wireplumber pipewire-pulse alsa-utils rtkit-daemon
```

### Configure Bit-Perfect Output

To allow your DAC to switch sample rates without resampling, create a configuration override for PipeWire:

```bash
mkdir -p ~/.config/pipewire/pipewire.conf.d/
cat <<EOF > ~/.config/pipewire/pipewire.conf.d/bitperfect.conf
context.properties = {
    default.clock.rate          = 44100
    default.clock.allowed-rates = [ 44100 48000 88200 96000 176400 192000 352800 384000 ]
    default.clock.min-quantum   = 32
    default.clock.max-quantum   = 8192
}
EOF
```
Restart PipeWire with ```systemctl --user restart pipewire wireplumber```

### ENVIRONMENT CONFIG

Ensure the user session knows where its PipeWire runtime bus is located. This is critical for wpctl and PipeWire to communicate.

```Bash
echo ">>> Configuring environment for PipeWire..."

# Add to .bashrc if not already present
if ! grep -q "XDG_RUNTIME_DIR" ~/.bashrc; then
  echo 'export XDG_RUNTIME_DIR="/run/user/$(id -u)"' >> ~/.bashrc
  echo ">>> XDG_RUNTIME_DIR added to ~/.bashrc"
fi

# Apply to current session immediately
export XDG_RUNTIME_DIR="/run/user/$(id -u)"

# Verification: This should show your DAC (Topping DX5)
wpctl status
```

### Host Audio Stack Activation
On a server, PipeWire services usually only start when a user logs in physically. To ensure your Topping DX5 is always available to the Docker container, run the following:

```Bash
# 1. Enable 'Linger' for your user. 
# This ensures PipeWire/WirePlumber stay running even when you are logged out.
sudo loginctl enable-linger $(whoami)

# 2. Configure the Environment for the current session
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
export DBUS_SESSION_BUS_ADDRESS="unix:path=${XDG_RUNTIME_DIR}/bus"

# 3. Enable and Start the Audio Services for the user session
# The '--user' flag is mandatory here.
systemctl --user enable --now pipewire.service pipewire-pulse.service wireplumber.service

# 4. Verify Services are Running
systemctl --user status pipewire wireplumber --no-pager
```
## 🚀 Deployment (Docker Compose)

Use the following ```docker-compose.yml``` to deploy Squeezelite.

Note: Ensure /run/user/1000 matches your actual User ID (id -u).

```yaml
services:
  squeezelite-dx5:
    image: ghcr.io/shuricksumy/squeezelite-pipewire:latest
    container_name: squeezelite-dx5
    restart: unless-stopped
    network_mode: host
    cap_add:
      - SYS_NICE
      - IPC_LOCK
    ulimits:
      rtprio: 95
      memlock: -1
      msgqueue: 8192000
    group_add:
      - audio
      - video
    environment:
      - PLAYER_NAME=DX5 # part of name like in wpctl status Audio - to set volume
      - SERVER_IP=192.168.1.100       # IP of your LMS Server
      - MAC_ADDR=72:23:90:88:38:63    # Unique MAC for this player
      - PIPEWIRE_RUNTIME_DIR=/tmp
      - PIPEWIRE_REMOTE=pipewire-0
      - PIPEWIRE_NODE=alsa_output.usb-Topping_DX5-00.analog-stereo # like sync name in wpctl status
      - SQUEEZE_EXTRA=-a 16384:8:24:0 -b 8000:12000 -D 500
    volumes:
      - /run/user/1000/pipewire-0:/tmp/pipewire-0
      - /dev/shm:/dev/shm
      - /dev/snd:/dev/snd

```

## ⚙️ Configuration Variables

| Variable | Description | Default |
| :--      | :--         | :--     |  
| PLAYER_NAME |	The name that appears in Logitech Media Server (LMS). | TEST-DX5 |
| SERVER_IP | The IP address of your LMS server.|Required|
|MAC_ADDR|Unique MAC address to identify the player.|Required|
|PIPEWIRE_NODE|The specific PipeWire output name (find via wpctl status).|Required|
|SQUEEZE_EXTRA|Extra Squeezelite arguments (buffers, etc).|See Compose|

## 🔍 Diagnostics & Monitoring

Run these commands on the host (or inside the container) to verify audio health:

### Check if Topping DX5 is recognized:
```bash
wpctl status
```

### Monitor Sample Rate & Bit-Depth in Real-Time:
```bash
pw-top
```

### Check Hardware Clock (The Truth):
```bash
cat /proc/asound/card*/pcm0p/sub0/hw_params
```

## 🏗️ Build Information
This project uses a multi-stage Docker build.

- Stage 1 (Builder): Compiles Squeezelite from the included Git submodule with support for FLAC, DSD, SSL, and Resampling.

- Stage 2 (Runtime): A slim Debian Trixie image containing only the necessary libraries and PipeWire plugins.

To build locally:
```bash
git clone --recursive https://github.com/shuricksumy/pipewire-squeezelite.git
cd pipewire-squeezelite
docker build -t squeezelite-pipewire .
```

## 📜 License
This project is licensed under the MIT License. Squeezelite itself is licensed under its respective GPL license.