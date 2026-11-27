# Changelog

## Unreleased

- "Add & watch" is now **Add · ready soon**: it never started playback, so the old name was misleading.

## v1.2.0 — 2026-11-25

Movies and regular TV, a Server page for fixing things without a terminal, a smoother player away from home.

### New

- **Movies & TV page** for everything that isn't anime: search (Radarr/Sonarr's TMDB/TVDB lookup, the better-known
  kind first), Trending / Popular / "because of what's in the library" rows from Radarr's discover lists (no TMDB key),
  and anime found there points to the Anime page. Shows: all seasons, the latest season, or only new episodes.
- **Own folders, profiles and libraries** for regular movies and TV: `film_folders` / `tv_folders` (Movies, TV Shows),
  profiles `Movies 1080p` (TRaSH HD Bluray + WEB) and `TV 1080p` (TRaSH WEB-1080p plus Blu-ray 1080p, 720p as a last
  resort) in `recyclarr/recyclarr.yml`; the wizard creates the folders and Jellyfin libraries. Regular TV gets season
  packs first ("Title S02" in Prowlarr's TV categories).
- **Add & watch** (anime and movies): adds it and downloads the first episode (a well-seeded single-episode release)
  or the movie first, in order and ahead of everything else, so it's ready to play within minutes; then the rest is
  searched as usual. Nothing starts playing by itself.
- **Server page** (owner only, an icon by the avatar): every service up/down with a **Restart** button; the media
  drives (connected, space left, a **Mount** button when one isn't); **Empty recycle bin** (also stops seeding
  torrents whose files only live on in the bin, so the space is really freed); stuck downloads with **Remove & find
  another**, or **Remove** for a finished one that can't be added; the watchdog's latest actions and alerts.
- **Forgot your Jellyfin password?** (owner, Account page): approve a Jellyfin app's Quick Connect code, or set a new
  password without the old one, since signing in to the site already proved who you are.
- **My Shows**: a search box, a **Downloading now** list at the top (progress, speed, time left, and downloads that
  can't be added), and a count of shows and movies. It replaces the Downloads tab; `/downloads` redirects there.
- **Play from search**: anime, movies and shows you already have get Play (and Episodes) in their search details.

### Better

- **Smoother adaptive player away from home**: it keeps one quality per stream and steps down only after real stalls.
  hls.js used to switch on its own every few seconds (it timed segments arriving at encode speed), and every switch
  started a new conversion on the server while the old ones kept running, so it buffered constantly. Skips now take
  about 2 s and the buffer fills about 2.5× faster than real time.
- **Cleaner look**: on phones the menu is a tab bar along the bottom; Manga and Followed manga are one tab; Account and
  Server lay their cards out in two columns on wide screens; Delete on My Shows is a small icon; one Jellyfin
  password card instead of two; the play icon on thumbnails is centred.
- **Watchdog**:
  - a phone alert when a media drive in fstab isn't mounted;
  - downloads that haven't received anything for a day are removed, blocklisted and searched again;
  - a finished download Sonarr/Radarr leaves "pending" with a warning (e.g. a repack that isn't better than the
    episode you have) is removed and blocklisted after 20 minutes;
  - TV entries for files Sonarr/Radarr replaced with a better copy are removed after their watched state is copied;
  - LimeTorrents (setting `regular_tv_only_indexers`) is only used for regular TV, through a `regular-tv` tag.
- **Repacks**: Sonarr and Radarr no longer prefer repacks over the quality rules (`propers_and_repacks:
  do_not_prefer` in the Recyclarr config), so they stop grabbing repacks worse than what's already there.

### Removed

- The old Downloads page and "Watch now on TV" while downloading (`/api/downloads`, `/api/watchnow`).

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
