#!/bin/bash
# Installs whatever aniserver needs that isn't on this machine yet, for your user only (no sudo):
#   Sonarr, Radarr, Prowlarr, qBittorrent and Jellyfin (with its ffmpeg), into ~/.local/opt, as user services.
# Anything already answering on its port is left alone. install.sh runs this; run it alone to add missing pieces.
# Ports can be changed with SONARR_PORT, RADARR_PORT, PROWLARR_PORT, QBIT_PORT, JELLYFIN_PORT (before the first install).
set -e
OPT="$HOME/.local/opt"; UNITS="$HOME/.config/systemd/user"; REPO=$(cd "$(dirname "$0")/.." && pwd)
SYSTEMCTL=${SYSTEMCTL:-systemctl}
SONARR_PORT=${SONARR_PORT:-8989}; RADARR_PORT=${RADARR_PORT:-7878}; PROWLARR_PORT=${PROWLARR_PORT:-9696}
QBIT_PORT=${QBIT_PORT:-8090}; JELLYFIN_PORT=${JELLYFIN_PORT:-8096}
[ "$(uname -m)" = x86_64 ] || { echo "The automatic install only knows x86_64 (64-bit Intel/AMD) downloads. Install the programs yourself."; exit 1; }

answers() { curl -s -o /dev/null -m 3 "http://localhost:$1/" ; }
have() { answers "$2" || [ -x "$OPT/$1/$1" ] || $SYSTEMCTL list-unit-files 2>/dev/null | grep -qi "^$1"; }

missing=()
have Sonarr "$SONARR_PORT" || missing+=(Sonarr)
have Radarr "$RADARR_PORT" || missing+=(Radarr)
have Prowlarr "$PROWLARR_PORT" || missing+=(Prowlarr)
{ answers "$QBIT_PORT" || command -v qbittorrent-nox >/dev/null || [ -x "$OPT/qbittorrent/qbittorrent-nox" ]; } || missing+=(qBittorrent)
{ answers "$JELLYFIN_PORT" || [ -x "$OPT/jellyfin/jellyfin" ] || command -v jellyfin >/dev/null; } || missing+=(Jellyfin)
if [ ${#missing[@]} -eq 0 ]; then
  echo "✓ Sonarr, Radarr, Prowlarr, qBittorrent and Jellyfin are already installed"
  exit 0
fi
echo "Not installed yet: ${missing[*]}"
if [ -t 0 ]; then read -r -p "Download and install them for $USER (about 600 MB, no sudo)? [Y/n] " ok; [[ "$ok" =~ ^[Nn] ]] && exit 0; fi
ldconfig -p 2>/dev/null | grep -q libicuuc || echo "! Sonarr/Radarr/Prowlarr need ICU. If they don't start: sudo apt install libicu-dev"
mkdir -p "$OPT" "$UNITS"

fetch() {   # fetch <url> <dest dir>: download a .tar.gz/.tar.xz and unpack it into dest (replacing it)
  local tmp; tmp=$(mktemp -d)
  echo "  downloading $(basename "$2")…"
  curl -fsSL -o "$tmp/pkg" "$1"
  mkdir -p "$tmp/x"; tar -xf "$tmp/pkg" -C "$tmp/x"
  rm -rf "$2"; mv "$tmp/x/$(ls "$tmp/x" | head -1)" "$2"; rm -rf "$tmp"
}

arr() {   # arr <Name> <port> <download url>
  local name=$1 port=$2 cfg="$HOME/.config/$1"
  fetch "$3" "$OPT/$name"
  mkdir -p "$cfg"
  [ -f "$cfg/config.xml" ] || cat > "$cfg/config.xml" <<EOF
<Config>
  <Port>$port</Port>
  <BindAddress>*</BindAddress>
  <AuthenticationMethod>Forms</AuthenticationMethod>
  <AuthenticationRequired>DisabledForLocalAddresses</AuthenticationRequired>
</Config>
EOF
  sed "s|%h|$HOME|g" "$REPO/systemd/user/${name,,}.service" > "$UNITS/${name,,}.service"
  echo "✓ $name installed (port $port)"
}

for m in "${missing[@]}"; do
  case $m in
    Sonarr) arr Sonarr "$SONARR_PORT" "https://services.sonarr.tv/v1/download/main/latest?version=4&os=linux&arch=x64" ;;
    Radarr) arr Radarr "$RADARR_PORT" "https://radarr.servarr.com/v1/update/master/updatefile?os=linux&runtime=netcore&arch=x64" ;;
    Prowlarr) arr Prowlarr "$PROWLARR_PORT" "https://prowlarr.servarr.com/v1/update/master/updatefile?os=linux&runtime=netcore&arch=x64" ;;
    qBittorrent)
      mkdir -p "$OPT/qbittorrent" "$HOME/.config/qBittorrent" "$HOME/Media/downloads"
      echo "  downloading qbittorrent-nox…"
      url=$(curl -fsSL https://api.github.com/repos/userdocs/qbittorrent-nox-static/releases/latest | grep -o '"browser_download_url": *"[^"]*/x86_64-qbittorrent-nox"' | cut -d'"' -f4)
      curl -fsSL -o "$OPT/qbittorrent/qbittorrent-nox" "$url"; chmod +x "$OPT/qbittorrent/qbittorrent-nox"
      # aniserver talks to it from this machine without a password; downloads land in ~/Media/downloads
      [ -f "$HOME/.config/qBittorrent/qBittorrent.conf" ] || cat > "$HOME/.config/qBittorrent/qBittorrent.conf" <<EOF
[LegalNotice]
Accepted=true

[Preferences]
WebUI\\Port=$QBIT_PORT
WebUI\\LocalHostAuth=false
Downloads\\SavePath=$HOME/Media/downloads/

[BitTorrent]
Session\\DefaultSavePath=$HOME/Media/downloads
EOF
      sed "s|%h|$HOME|g; s|--webui-port=8090|--webui-port=$QBIT_PORT|" "$REPO/systemd/user/qbittorrent.service" > "$UNITS/qbittorrent.service"
      echo "✓ qBittorrent installed (port $QBIT_PORT)" ;;
    Jellyfin)
      base=https://repo.jellyfin.org/files
      jf=$(curl -fsSL "$base/server/linux/latest-stable/amd64/" | grep -o 'jellyfin_[^"<]*amd64\.tar\.gz' | head -1)
      ff=$(curl -fsSL "$base/ffmpeg/linux/latest-7.x/amd64/" | grep -o 'jellyfin-ffmpeg_[^"<]*portable_linux64-gpl\.tar\.xz' | head -1)
      fetch "$base/server/linux/latest-stable/amd64/$jf" "$OPT/jellyfin"
      tmp=$(mktemp -d); echo "  downloading jellyfin-ffmpeg…"
      curl -fsSL -o "$tmp/ff.tar.xz" "$base/ffmpeg/linux/latest-7.x/amd64/$ff"
      mkdir -p "$OPT/jellyfin-ffmpeg"; tar -xf "$tmp/ff.tar.xz" -C "$OPT/jellyfin-ffmpeg"; rm -rf "$tmp"
      mkdir -p "$HOME/.config/jellyfin"
      [ -f "$HOME/.config/jellyfin/network.xml" ] || [ "$JELLYFIN_PORT" = 8096 ] || printf '<?xml version="1.0" encoding="utf-8"?>\n<NetworkConfiguration>\n  <InternalHttpPort>%s</InternalHttpPort>\n</NetworkConfiguration>\n' "$JELLYFIN_PORT" > "$HOME/.config/jellyfin/network.xml"
      sed "s|%h|$HOME|g" "$REPO/systemd/user/jellyfin.service" > "$UNITS/jellyfin.service"
      echo "✓ Jellyfin installed (port $JELLYFIN_PORT)" ;;
  esac
done

$SYSTEMCTL --user daemon-reload
for m in "${missing[@]}"; do $SYSTEMCTL --user enable --now "${m,,}.service"; done
echo "  waiting for them to start…"
for port in $SONARR_PORT $RADARR_PORT $PROWLARR_PORT $QBIT_PORT $JELLYFIN_PORT; do
  for _ in $(seq 1 60); do answers "$port" && break; sleep 2; done
done
echo "✓ all running. Kodi for the TV is optional: sudo apt install kodi mpv"
