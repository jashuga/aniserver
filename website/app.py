#!/usr/bin/env python3
"""aniserver, the HTPC website. Run: python3 app.py (the manga-request user service); first time: python3 app.py setup.

The code lives in the aniserver/ package next to this file, one module per job:
  config     settings and people (~/.config/manga-request/config.json), service addresses, media folders
  services   Sonarr, Radarr, Prowlarr, qBittorrent, Jellyfin, Kodi and AniList clients, shared caches
  auth       sign-in: passwords, authenticator codes, signed cookies, password changes
  anime      adding anime/movies: AniList search, AniList -> TVDB/TMDB, seasons matched by air date
  manga      Nyaa manga search, downloads and follows
  library    status of everything in Sonarr/Radarr, live downloads, deleting, Jellyfin library queries
  streaming  playback: tracks, subtitles, HLS + adaptive ladder, fonts, bitrates, progress, the TV
  pages      HTML building blocks and the hashed CSS/JS in assets/
  artwork    anime posters and title logos in Japanese or English (Account page)
  wizard     first-time setup (python3 app.py setup)
  server     every route (Handler) and main()
Everything is also available as app.<name> (the helper scripts in bin/ and the tests use that)."""
import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
if __name__ == "__main__" and sys.argv[1:2] == ["setup"]:    # first-time setup: works before config.json exists
    from aniserver.wizard import main as setup
    raise SystemExit(setup())
for _name in ("config", "services", "auth", "anime", "manga", "library", "streaming", "pages", "artwork", "server"):
    globals().update({k: v for k, v in vars(importlib.import_module(f"aniserver.{_name}")).items() if not k.startswith("__")})

if __name__ == "__main__":
    main()  # noqa: F821 (from aniserver.server)
