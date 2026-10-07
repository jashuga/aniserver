#!/bin/bash
# The website (website/) and helper scripts (bin/, linked from ~/.local/bin) run straight from this repo. This copies in
# the rest of the HTPC's config: systemd units, mpv, Kodi, Recyclarr (run it, review `git diff`, commit).
# Secrets never live in these files: API keys, passwords, TOTP secrets, ntfy topic and DDNS URLs stay in
# ~/.config/... and ~/htpc-credentials.txt, which are NOT copied. check-secrets.sh verifies that before every commit.
set -e
cd "$(dirname "$0")"
H=$HOME

# one-time setup scripts (run with sudo) live only in this repo: setup/
# systemd user services/timers (+ drop-ins)
mkdir -p systemd/user
for f in ddns-update htpc-autosync htpc-watchdog jellyfin kodi komga manga-request manga-rescan radarr recyclarr-sync spotify-connect suwayomi; do
  for ext in service timer; do [ -f "$H/.config/systemd/user/$f.$ext" ] && cp "$H/.config/systemd/user/$f.$ext" systemd/user/; done
  [ -d "$H/.config/systemd/user/$f.service.d" ] && rsync -a "$H/.config/systemd/user/$f.service.d" systemd/user/
done

# players: mpv (TV playback) and Kodi (TV menu, mpv as external player)
mkdir -p mpv/scripts kodi
cp "$H/.config/mpv/mpv.conf" mpv/
cp "$H/.config/mpv/scripts/htpc-progress.lua" mpv/scripts/
cp "$H/.kodi/userdata/playercorefactory.xml" kodi/
sed "s|$H/|/home/YOUR_USER/|g" "$H/.kodi/userdata/sources.xml" > kodi/sources.xml      # no real home path

# recyclarr (TRaSH anime quality profiles) with API keys blanked
mkdir -p recyclarr
sed -E 's/(api_key:).*/\1 YOUR_API_KEY/' "$H/.config/recyclarr/recyclarr.yml" > recyclarr/recyclarr.yml

./check-secrets.sh
echo "Collected. Review with: git status && git diff"
