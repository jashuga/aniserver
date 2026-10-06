# aniserver

A Dell Vostro 5490 laptop turned into a 24/7 anime HTPC on Ubuntu 25.10. It downloads new episodes automatically,
plays them on the living-room TV, streams to phones and PCs anywhere, and runs a small manga setup.
Its centerpiece is **aniserver**, a Netflix/Crunchyroll-style website with its own adaptive video player.

This is a personal setup published as a reference: the scripts assume one Ubuntu machine, one user account and the
services below. Only download what you have the right to.

## What runs where

| Piece | What it does |
|---|---|
| **Sonarr** (:8989) + **Radarr** (:7878) | Anime series and movies; TRaSH `[Anime] Remux-1080p` profiles synced by **Recyclarr** |
| **Prowlarr** (:9696) | Indexers: Nyaa.si, nekoBT, AnimeTosho, Nyaa.si (Manga) |
| **qBittorrent** (:8090) | Downloads; on-add hook gives manga top priority (`bin/qbit-added`) |
| **Jellyfin** (:8096) | Library, metadata, watch progress per person, GPU transcoding (VAAPI) |
| **Kodi + mpv** | The TV: Kodi menus, mpv (`mpv/mpv.conf`) plays everything with full-quality ASS subtitles |
| **Suwayomi** (:4567) + **Komga** (:25600) | Weekly manga chapters + volume library (read in Komelia) |
| **go-librespot** | Spotify Connect speaker |
| **Caddy** | HTTPS for two public hostnames (the website and Jellyfin); fail2ban; key-only SSH from the internet |
| **website/** (:5050) | The aniserver site (below) |

Two drives: the internal NVMe, plus a USB SSD at `/mnt/media`. `bin/sonarr-anime-defaults` sends a new show to whichever
drive has room, and sets anime vs standard profiles.

## The website (`website/app.py`)

Single-file Python (standard library only), run by the `manga-request` user service.

- **Home**: hero carousel (show logos, *Resume S4 E1 · 15m left*) and rows for Continue watching, New episodes, My shows,
  On the way (downloading or airing), and Trending (AniList).
- **Show pages**: backdrop, season tabs, episodes with stills and progress, watched toggles. Episodes that are still
  downloading or haven't aired yet appear greyed out.
- **Browse**: AniList search ("re zero", "jjk"…) → Fribb anime-lists → TVDB/TMDB → adds to Sonarr/Radarr, which works
  around Sonarr's weak anime search. Adding a season matches the AniList entry to TVDB episodes **by air date**, not by
  season number, because TVDB's season numbers often differ from the anime's (continuing seasons, split cours,
  Netflix-first ONAs, OVAs).
- **Downloads**: live progress from qBittorrent joined with Sonarr/Radarr (what each torrent is, importing, ready to watch,
  or couldn't be added).
- **Manga**: Nyaa manga search, downloads, and follows (qBittorrent RSS rules).
- **Player** (`/watch/<id>`). Picks the lightest path the browser and connection can handle:
  1. *Direct play* of the original file (home network; Range requests, instant seeking)
  2. *Original quality, repackaged* (Jellyfin remux to fMP4 HLS, 90 s buffer)
  3. *Adaptive* GPU-encoded ladder 1080p 10M / 1080p 6M / 720p / 480p / 360p (3 s aligned segments, hls.js ABR)
  4. *Burned-in subtitles* only for picture subtitles

  Away from home it plays the original only when the remembered bandwidth beats the file's 30-second peak bitrate
  (profiled with ffprobe). If playback stalls, it steps down to the next path. Subtitles are rendered in the browser
  by **JASSUB** (libass/WASM) using the release's embedded fonts. Also: resume and progress synced to Jellyfin,
  Skip Opening and Next episode (chapters), autoplay, audio/subtitle/quality/speed menus, Continue on TV (writes mpv's
  watch-later file, then Kodi `Player.Open`), PiP, MediaSession and keyboard shortcuts.
  Every minute and on any problem it writes to `~/.local/share/htpc-web/player.log`.
- **Security**: internet requests (via Caddy) need username + PBKDF2 password + TOTP (once per browser), with rate
  limiting and fail2ban. Video is fetched with HMAC stream tokens, and the Jellyfin API key never reaches the browser.
- **Profiles**: "Who's watching?" lists the people in `profiles` in `config.json` (the first one is the owner); each maps
  to the Jellyfin user with the same name. Internet logins are set up per person at `/security` (home network or
  Tailscale only).

### Getting started

1. Install Python 3.12+, Jellyfin, Sonarr, Radarr, Prowlarr and qBittorrent (`setup/` has the scripts used here).
2. Copy `website/` to `~/.local/opt/manga-request/`, and `website/config.example.json` to
   `~/.config/manga-request/config.json` (mode 600). Fill in the API keys, a long random `secret` and your profiles.
3. Install the user services from `systemd/user/` (`systemctl --user enable --now manga-request`) and open port 5050.
4. Tests: `python3 -m unittest discover -s website/tests` (they also `node --check` every embedded script).

## Other pieces

- `bin/htpc-watchdog` (`htpc-watchdog.timer`, every 15 min): safety net. It restarts services that stop answering,
  switches a show back on if nothing in it is set to download, re-searches aired episodes and released movies that are
  still missing, clears finished downloads Sonarr/Radarr can't import when the library already has them, and alerts the
  phone (ntfy) when something stays broken
- `bin/htpc-notify`: Sonarr/Radarr hook that sends one ntfy push per finished download, with *Watch* and *Play on TV* buttons
- `bin/ddns-update` (`ddns-update.timer`): keeps the public hostnames pointed at the home internet address (Namecheap
  dynamic DNS). It checks every 5 minutes and only contacts Namecheap when the address changed (or twice a day)
- `bin/manga-rescan`: Komga library rescan (`manga-rescan.timer`)
- `mpv/scripts/htpc-progress.lua`: reports where TV playback stopped, so it syncs to Jellyfin's Continue watching
- `setup/`: the one-time root scripts used to build the machine (`sudo bash setup/<script>`), including
  `streaming-tuning.sh` (BBR + fq for smooth streaming over lossy remote networks)
- `systemd/user/`: the user services and timers

## Secrets

None are in this repo. They live only on the machine:

| File | Contents |
|---|---|
| `~/.config/manga-request/config.json` | Website config; template in `website/config.example.json` |
| `~/.config/manga-request/remote-users.json` | Internet logins (password hashes, TOTP) |
| `~/.config/htpc-ntfy-topic`, `~/.config/ddns-update-urls`, `~/.config/htpc-jellyfin-admin` | ntfy topic, DDNS URLs, Jellyfin admin password |
| `~/.config/{Sonarr,Radarr,Prowlarr}/config.xml` | API keys |

## Updating this repo (automatic)

`autosync.sh` copies the live files in (`collect.sh`), runs `check-secrets.sh`, and commits + pushes only when
something changed. It runs after every Claude Code response (a `Stop` hook) and every 10 minutes
(`htpc-autosync.timer`). If the check fails, nothing is committed or pushed. Log: `~/.local/share/htpc-web/autosync.log`.

`check-secrets.sh` reads the real secret values from the files above and refuses if any of them appears in the repo.
It also refuses private words (people's names, the live domain) listed in `~/.config/aniserver-private-words`.

## Third-party code

- [hls.js](https://github.com/video-dev/hls.js) 1.6.15, Apache-2.0 (`website/static/hls-1.6.15.LICENSE.txt`)
- [JASSUB](https://github.com/ThaUnknown/jassub) 1.8.8, MIT (`website/static/jassub-1.8.8/LICENSE.txt`). Its
  WebAssembly worker is built from libass and its font libraries, and `default.woff2` is Liberation Sans (SIL Open Font
  License 1.1); see the JASSUB repository for those licenses.

The server serves `.gz` copies of the static files when present (`gzip -9 -k` the files in `website/static`).
