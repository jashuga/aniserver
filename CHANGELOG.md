# Changelog

## Unreleased

- **Movies & TV page** for everything that isn't anime: search (Radarr/Sonarr's TMDB/TVDB lookup, the better-known
  kind first), Trending / Popular / "because of what's in the library" rows from Radarr's discover lists (no TMDB key),
  and anime found there points to the Anime page. Movies: **Add** or **Add & watch now on TV** (picks a release with
  enough seeders to stay ahead of playback). Shows: all seasons, the latest season, or only new episodes.
- **Regular TV gets season packs first**: the batch-first search asks for "Title S02" in Prowlarr's TV categories.
- **Own folders, profiles and libraries**: `film_folders` / `tv_folders` (Movies, TV Shows), profiles `Movies 1080p`
  (TRaSH HD Bluray + WEB) and `TV 1080p` (TRaSH WEB-1080p plus Blu-ray 1080p, 720p as a last resort) in
  `recyclarr/recyclarr.yml`; the wizard creates the folders and Jellyfin libraries. The Sonarr add hook puts regular
  shows in TV Shows with TV 1080p.

## v1.1.0 — 2026-11-03

Easier to set up on your own machine, faster downloads, passkeys.

- **Passkeys**: sign in with Windows Hello, Face ID, Touch ID or a phone (Account page → Add this device). No password
  or code, and it keeps working when a browser forgets its cookies. Standard library only (CBOR, ES256, RS256).
- **Batch-first search**: adding a season asks Prowlarr for whole-season batches and lets Sonarr pick (seconds instead
  of Sonarr's episode-by-episode anime search, which took ~40 s per episode); falls back to Sonarr's own search.
- **Artwork and titles per person**: everyone picks Japanese or English artwork on their Account page; Japanese also
  shows romaji titles. The owner's pick is what the Jellyfin apps and the TV show.
- **Separate watch histories**: the TV (Kodi/mpv) keeps its own; the website keeps each person's. Play on TV resumes
  where the TV stopped; Continue on TV hands the website's spot over.
- **Surround on a Dolby Digital soundbar**: mpv re-encodes any 5.1 track to AC3 (`mpv/mpv.conf`);
  `setup/hdmi-passthrough.sh` switches Intel laptops from the SOF driver (stereo-only HDMI) to the legacy one.
- **Safety net**: imports batches Sonarr only knew by TVDB id, scans a missing movie's own folder, and deleted titles
  leave Jellyfin and the TV reliably (waits for the files and for Kodi's rescan first).
- **Tests on GitHub Actions** (Python 3.12 and 3.13).

- **`./install.sh`**: downloads whatever is missing of Sonarr, Radarr, Prowlarr, qBittorrent and Jellyfin
  (`setup/prereqs.sh`: into `~/.local/opt` as user services, no sudo), links the helper scripts, installs the services
  for wherever the repo was cloned, runs the setup wizard and starts everything.
- **Setup wizard** (`python3 website/app.py setup`): finds the programs and their API keys, then connects them: media
  folders, qBittorrent as the download client, Prowlarr feeding Sonarr/Radarr (with Nyaa if there's no indexer yet),
  Jellyfin's first-run setup (the owner becomes its admin, same password) and its Anime/Movies libraries. Creates the
  owner's login (password + authenticator app). Safe to re-run: anything already set up is left alone.
- **ffprobe** from Jellyfin's ffmpeg is used when there's no system ffmpeg.
- **Everything machine-specific is a setting** (`config.example.json` lists them): service addresses and API keys,
  media folders (one drive or two), home networks, the Sonarr profiles.
- **Optional parts switch off cleanly**: no Kodi hides the TV buttons, no Prowlarr/Nyaa hides manga, no ntfy topic
  means no phone pushes.

## v1.0.0 — 2026-10-25

First public release.

- **Website**: Netflix-style home, show pages, My Shows, live Downloads, AniList browse/add, manga search and follows,
  per-person accounts (password + TOTP once per browser) with their own watch history, password changes, deleting titles.
- **Player**: direct play / remux / adaptive GPU ladder / burned-in fallback, chosen per connection and remembered per
  network; JASSUB subtitles with the release's fonts; skip opening, next episode, resume, Continue on TV.
- **Adding anime**: AniList → Fribb → TVDB, seasons matched by air date (split cours, continuing seasons, Netflix-first
  dates, OVAs), never leaves a show with nothing monitored.
- **TV**: Kodi + mpv; watch while downloading; Kodi's library kept in step automatically.
- **Anime artwork** in Japanese or English (posters and title logos), set on the Account page.
- **Safety net** (`htpc-watchdog`): service restarts, re-searches, stuck/misread imports, Kodi sync, phone alerts.
- 52 tests that run without any of the real services.
