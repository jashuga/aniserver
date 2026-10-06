#!/bin/bash
# Installs qBittorrent-nox, Sonarr, Prowlarr as services running as your user.
# Run with: sudo bash ~/htpc-arr-setup.sh
set -e
U=${SUDO_USER:?run this with sudo from your normal account}
H=/home/$U

apt-get update
apt-get install -y qbittorrent-nox sqlite3 mediainfo

for app in Sonarr Prowlarr; do
  lc=${app,,}
  rm -rf /opt/$app
  tar xzf $H/.cache/htpc/$lc.tar.gz -C /opt
  chown -R $U:$U /opt/$app
  cat > /etc/systemd/system/$lc.service <<UNIT
[Unit]
Description=$app
After=network-online.target
Wants=network-online.target

[Service]
User=$U
Group=$U
UMask=0077
Type=simple
ExecStart=/opt/$app/$app -nobrowser -data=$H/.config/$app
TimeoutStopSec=20
KillMode=process
Restart=on-failure

[Install]
WantedBy=multi-user.target
UNIT
done

systemctl daemon-reload
systemctl enable --now qbittorrent-nox@$U sonarr prowlarr
sleep 5
systemctl --no-pager --lines=0 status qbittorrent-nox@$U sonarr prowlarr | grep -E '●|Active:'
echo "Done. Tell Claude to continue."
