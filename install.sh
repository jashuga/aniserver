#!/bin/bash
# aniserver installer, run from the cloned repo as your normal user (no sudo):
#   links the helper scripts into ~/.local/bin, installs the website and safety-net services for this repo's location,
#   runs the setup wizard if there are no settings yet, and starts everything.
# Needs Python 3.12+ and Sonarr, Radarr, qBittorrent and Jellyfin already running (setup/ has scripts for those).
set -e
REPO=$(cd "$(dirname "$0")" && pwd)
SYSTEMCTL=${SYSTEMCTL:-systemctl}
python3 -c 'import sys; sys.exit(sys.version_info < (3, 12))' || { echo "aniserver needs Python 3.12 or newer."; exit 1; }

mkdir -p "$HOME/.local/bin" "$HOME/.config/systemd/user"
for f in "$REPO"/bin/*; do [ -f "$f" ] && [ -x "$f" ] && ln -sfn "$f" "$HOME/.local/bin/$(basename "$f")"; done
for unit in manga-request.service htpc-watchdog.service htpc-watchdog.timer; do
  sed "s|%h/aniserver|$REPO|g" "$REPO/systemd/user/$unit" > "$HOME/.config/systemd/user/$unit"
done
echo "✓ services installed for $REPO"

if [ ! -f "${ANISERVER_CONFIG:-$HOME/.config/manga-request/config.json}" ]; then
  python3 "$REPO/website/app.py" setup
fi

$SYSTEMCTL --user daemon-reload
$SYSTEMCTL --user enable --now manga-request.service htpc-watchdog.timer
echo "✓ aniserver is running: http://$(hostname).local:5050"
echo "  To keep it running while you're logged out: loginctl enable-linger $USER"
