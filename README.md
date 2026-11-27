# aniserver

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE) ![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg) ![No dependencies](https://img.shields.io/badge/dependencies-stdlib%20only-brightgreen.svg)

A home anime server: new episodes download by themselves, play on the living-room TV, and stream anywhere through
**aniserver**, a Netflix-style website with its own adaptive player. Built on Sonarr, Radarr, qBittorrent, Jellyfin
and Kodi, running on one Ubuntu machine.

| Home | Show page |
|---|---|
| ![Home](docs/home.png) | ![Show page](docs/show.png) |
| **Movies & TV** | **My Shows** |
| ![Movies & TV](docs/movies.png) | ![My Shows](docs/myshows.png) |

## Features

- **Add anime by the name people use** ("jjk", "re zero"). Seasons are matched to TVDB by air date, so split cours,
  continuing seasons and Netflix-first releases get the right episodes.
- **A player that adapts to the connection**: direct play at home, the original repackaged, or GPU-encoded away from
  home (one quality per stream, stepping down only after real stalls), with full-quality anime subtitles (ASS, the
  release's own fonts) in the browser.
- **Movies and TV too**: a Movies & TV page searches TMDB/TVDB through Radarr and Sonarr, shows what's trending and
  popular, and adds them with their own 1080p quality rules and folders. Regular TV downloads whole seasons at a time,
  like anime batches.
- **Add · ready soon** (anime and movies): the first episode or the movie downloads first, from a well-seeded release, so
  it's ready to play within minutes; the rest follows. Search results you already have get a Play button.
- **My Shows**: everything you've added, searchable, with what's downloading right now at the top.
- **A Server page for the owner**: services with restart buttons, drive space (and a mount button if the USB drive
  comes loose), an empty-recycle-bin button, stuck downloads with one-click replacement, and what the watchdog did
  lately. Forgot your Jellyfin password? Reset it or approve a Quick Connect code from the Account page.
- **Resume anywhere**, skip openings, next episode, Play on TV and Continue on TV.
- **A safety net** that fixes or reports what usually breaks: shows left with nothing to download, stuck or misread
  imports, dead or fake downloads (replaced after a day), the TV missing episodes or keeping replaced ones, a media
  drive that came unplugged, services that stop answering.
- **Anime artwork in Japanese or English** (posters, title logos, romaji titles), each person's own pick.
- **Accounts**: one login per person (password + authenticator code once per browser, or a passkey such as
  Windows Hello), each with their own history; the TV keeps its own.
- **Manga**: Nyaa search, downloads and followed series for Komga.

## Getting started

You need a 64-bit Ubuntu/Debian machine with **Python 3.12+**. Kodi for the TV is optional (`sudo apt install kodi mpv`).

```bash
git clone https://github.com/jashuga/aniserver.git ~/aniserver
cd ~/aniserver && ./install.sh
```

`install.sh` downloads whatever is missing of Sonarr, Radarr, Prowlarr, qBittorrent and Jellyfin (into
`~/.local/opt`, as user services, no sudo; ones you already have are used as they are). Then the setup wizard connects
them to each other (media folders in `~/Media`, qBittorrent as the downloader, the Nyaa indexer, Jellyfin's libraries),
asks a few questions, and creates your login (password + authenticator app; the same password signs you into
Jellyfin). The site starts on port 5050. Run `python3 website/app.py setup` again any time to change the answers. Every setting is listed in
`website/config.example.json`; parts you skip (the TV, manga, phone alerts) stay hidden.

To reach it from outside the home, put it behind HTTPS (e.g. Caddy) and set `public_host`.

Tests (no real services needed): `python3 -m unittest discover -s website/tests`

## Layout

| Path | What's there |
|---|---|
| `website/` | The site: `app.py` (entry point and `setup`), `aniserver/` (one module per job), `assets/` (CSS/JS), `tests/` |
| `install.sh` | Installs everything and runs the setup wizard |
| `bin/` | Safety net (`htpc-watchdog`), phone notifications, dynamic DNS, Sonarr/qBittorrent hooks |
| `systemd/user/` | Services and timers |
| `kodi/`, `mpv/`, `recyclarr/` | TV player and quality-profile config |
| `setup/` | `prereqs.sh` (downloads the programs) and optional root setup scripts (examples from this machine) |

Only download what you have the right to.

## License

[MIT](LICENSE). Bundled: [hls.js](https://github.com/video-dev/hls.js) (Apache-2.0) and
[JASSUB](https://github.com/ThaUnknown/jassub) (MIT, includes libass and Liberation Sans, OFL); their license files are
in `website/static/`.
