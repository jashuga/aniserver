"""Settings and the people who use the site (from ~/.config/manga-request/config.json), service addresses, media folders."""
import json
import os
from pathlib import Path


CONF = json.loads((Path.home() / ".config/manga-request/config.json").read_text())
PROWLARR = "http://localhost:9696/api/v1"
QBIT = "http://localhost:8090/api/v2"
RSS_FOLDER = "Manga"
# the people (config.json "profiles": login name -> [display name, avatar color]); the first one is the owner
PROFILES = {u: tuple(v) for u, v in (CONF.get("profiles") or {"me": ["Me", "#ff7a1a"]}).items()}
OWNER = next(iter(PROFILES))
REMOTE_NAMES = tuple(PROFILES)   # anyone here can get an internet login at /security; must match their Jellyfin user name


# ---------- anime (AniList -> TVDB id + season -> Sonarr) ----------

SONARR = "http://localhost:8989/api/v3"
ANILIST = "https://graphql.anilist.co"
UA = "Mozilla/5.0 (X11; Linux x86_64) htpc-requests/1.0"
INTERNAL_ROOT = str(Path.home() / "Media/Anime")  # the Sonarr add hook moves shows to the SSD when this drive fills
RADARR = "http://localhost:7878/api/v3"
MOVIE_ROOT_INTERNAL = str(Path.home() / "Media/Anime Movies")
MOVIE_ROOT_SSD = "/mnt/media/Anime Movies"


# ---------- "My shows" hub: status, watch here (Jellyfin), play on TV (Kodi) ----------

KODI = "http://localhost:8085/jsonrpc"
JELLYFIN = "http://localhost:8096"
MEDIA_ROOTS = [os.path.realpath(p) for p in (INTERNAL_ROOT, "/mnt/media/Anime", MOVIE_ROOT_INTERNAL, MOVIE_ROOT_SSD)]
STATIC = Path(__file__).resolve().parent.parent / "static"


TV_PROFILE = CONF.get("tv_profile", OWNER)      # whose Jellyfin progress the living-room TV updates
