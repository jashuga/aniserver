#!/bin/bash
# HTPC performance tuning (run with: sudo bash ~/htpc-tune.sh). Safe to re-run.
set -e
U=${SUDO_USER:?run this with sudo from your normal account}

# 1. Background services never compete with video playback: lowest disk priority, low CPU priority.
for svc in qbittorrent-nox@$U sonarr prowlarr; do
  mkdir -p "/etc/systemd/system/$svc.service.d"
  cat > "/etc/systemd/system/$svc.service.d/htpc-priority.conf" <<'EOF'
[Service]
Nice=10
IOSchedulingClass=idle
CPUWeight=20
EOF
done
systemctl daemon-reload
systemctl restart qbittorrent-nox@$U sonarr prowlarr

# 2. Keep Kodi/mpv in RAM instead of swapping them out (15 GB RAM is plenty).
echo 'vm.swappiness=10' > /etc/sysctl.d/99-htpc.conf
sysctl -q -p /etc/sysctl.d/99-htpc.conf

# 3. Wi-Fi power saving off: lower latency for Sonarr/Komga/manga pages from other devices.
cat > /etc/NetworkManager/conf.d/99-wifi-powersave-off.conf <<'EOF'
[connection]
wifi.powersave = 2
EOF
iw dev wlp0s20f3 set power_save off || true   # apply now without dropping the connection

# 4. Snap updates only between 4 and 6 AM, never mid-episode.
snap set system refresh.timer=4:00-6:00

echo
echo "swappiness: $(cat /proc/sys/vm/swappiness)"
echo "wifi: $(iw dev wlp0s20f3 get power_save)"
echo "snap refresh: $(snap get system refresh.timer)"
for svc in qbittorrent-nox@$U sonarr prowlarr; do
  pid=$(systemctl show -p MainPID --value "$svc"); echo "$svc: $(systemctl is-active $svc), nice $(ps -o ni= -p $pid), io $(ionice -p $pid | cut -d: -f1)"
done
echo "Done. Tell Claude to continue."
