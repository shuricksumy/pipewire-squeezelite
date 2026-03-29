#!/bin/bash
# =================================================================
# THE JBL CHARGE 5 "FINAL BOSS" FIX (UBUNTU 24.04 HEADLESS)
# -----------------------------------------------------------------
# VARIABLE VERSION: Change the MAC to fix any Bluetooth device.
# =================================================================

# --- 1. SET YOUR VARIABLES ---
TARGET_MAC="F8:5C:7D:75:D6:06"
TARGET_NAME="Sumy JBL Charge5"

# --- 2. KERNEL OVERRIDE (Persistent) ---
# Kill the eSCO (Voice) channel to stop "Address already in use"
echo "options bluetooth disable_esco=1" | sudo tee /etc/modprobe.d/jbl-audio-fix.conf

# --- 3. WIREPLUMBER OVERRIDE (User-Level) ---
mkdir -p ~/.config/wireplumber/wireplumber.conf.d/
cat <<EOF > ~/.config/wireplumber/wireplumber.conf.d/10-bluetooth-fix.conf
monitor.bluez.properties = {
  bluez5.roles = [ a2dp_sink ]
  bluez5.hfphsp-backend = "none"
}
EOF

# --- 4. RELOAD HARDWARE & SERVICES ---
sudo modprobe -r btusb && sudo modprobe btusb
sudo systemctl restart bluetooth
systemctl --user restart wireplumber

# --- 5. CLEAN HANDSHAKE ---
echo "--- Resetting connection for $TARGET_NAME ($TARGET_MAC) ---"
bluetoothctl remove "$TARGET_MAC"
sleep 2

echo "--- Put $TARGET_NAME in PAIRING MODE now ---"
sleep 5

# Automated Pairing Sequence
bluetoothctl scan on &
SCAN_PID=$!
sleep 10
kill $SCAN_PID

bluetoothctl pair "$TARGET_MAC"
bluetoothctl trust "$TARGET_MAC"
bluetoothctl connect "$TARGET_MAC"

# --- 6. FINAL VERIFICATION ---
echo "--- VERIFYING SINK STATUS ---"
wpctl status | grep -i "$TARGET_NAME"