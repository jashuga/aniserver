#!/bin/bash
# HTPC system-level setup for Vostro 5490 (run with: sudo bash ~/htpc-root-setup.sh)
set -e
USER_NAME=${SUDO_USER:?run this with sudo from your normal account}

# 0. Remove the Kodi PPA: it has no Ubuntu 25.10 release and breaks apt update
#    (Kodi is installed from Ubuntu's own universe repo, so nothing depends on it)
rm -f /etc/apt/sources.list.d/team-xbmc-ubuntu-ppa-questing.sources

# 1. Intel VA-API hardware decoding (full HEVC/VP9 support)
apt-get update
apt-get install -y intel-media-va-driver-non-free vainfo

# 2. Keep running with the lid closed
mkdir -p /etc/systemd/logind.conf.d
cat > /etc/systemd/logind.conf.d/htpc.conf <<'EOF'
[Login]
HandleLidSwitch=ignore
HandleLidSwitchExternalPower=ignore
HandleLidSwitchDocked=ignore
EOF

# 3. Never suspend/hibernate (always plugged in, dedicated HTPC)
systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target

# 4. Auto-login to the desktop
cp -n /etc/gdm3/custom.conf /etc/gdm3/custom.conf.bak
sed -i '/^AutomaticLogin/d' /etc/gdm3/custom.conf
sed -i "/^\[daemon\]/a AutomaticLoginEnable=True\nAutomaticLogin=$USER_NAME" /etc/gdm3/custom.conf

# 5. Battery longevity: hold charge between 50-80% since it's always on AC
cat > /etc/systemd/system/battery-charge-limit.service <<'EOF'
[Unit]
Description=Limit battery charge for always-plugged-in use
After=multi-user.target

[Service]
Type=oneshot
ExecStart=/bin/sh -c 'echo Custom > /sys/class/power_supply/BAT0/charge_types; echo 50 > /sys/class/power_supply/BAT0/charge_control_start_threshold; echo 80 > /sys/class/power_supply/BAT0/charge_control_end_threshold'

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now battery-charge-limit.service

echo
echo "Done. Battery: $(cat /sys/class/power_supply/BAT0/charge_types) start=$(cat /sys/class/power_supply/BAT0/charge_control_start_threshold) end=$(cat /sys/class/power_supply/BAT0/charge_control_end_threshold)"
vainfo 2>/dev/null | grep -iE 'driver version|HEVCMain10' | head -3
echo "Reboot to apply lid/auto-login changes."
