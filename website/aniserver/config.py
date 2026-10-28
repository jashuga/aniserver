"""Settings (~/.config/manga-request/config.json, written by `python3 app.py setup`): the people who use the site,
where the other programs are, the media folders, and which optional parts (the TV, manga) are switched on.
Every key is optional except the secrets; the defaults match a standard single-machine install."""
import ipaddress
import json
import os
from pathlib import Path

HOME = Path.home()
# ANISERVER_CONFIG points somewhere else (the tests use config.example.json)
CONFIG_FILE = Path(os.environ.get("ANISERVER_CONFIG") or HOME / ".config/manga-request/config.json")
CONF = json.loads(CONFIG_FILE.read_text())


def _url(key, default):
    return (CONF.get(key, default) or "").rstrip("/")


def _folders(key, default):
    return [str(Path(p).expanduser()) for p in (CONF.get(key) or default)]


# the people (config.json "profiles": login name -> [display name, avatar color]); the first one is the owner
PROFILES = {u: tuple(v) for u, v in (CONF.get("profiles") or {"me": ["Me", "#ff7a1a"]}).items()}
OWNER = next(iter(PROFILES))
REMOTE_NAMES = tuple(PROFILES)   # anyone here can get a login at /security; must match their Jellyfin user name

# the other programs (base addresses; "kodi_url": "" switches the TV features off)
SONARR = _url("sonarr_url", "http://localhost:8989") + "/api/v3"
RADARR = _url("radarr_url", "http://localhost:7878") + "/api/v3"
PROWLARR = _url("prowlarr_url", "http://localhost:9696") + "/api/v1"
QBIT = _url("qbittorrent_url", "http://localhost:8090") + "/api/v2"
JELLYFIN = _url("jellyfin_url", "http://localhost:8096")
KODI_URL = _url("kodi_url", "http://localhost:8085")
KODI = KODI_URL + "/jsonrpc" if KODI_URL else ""
TV = bool(KODI_URL)              # Kodi + mpv on the TV: "Play on the TV" buttons, watch-while-downloading
MANGA = bool(CONF.get("manga", "prowlarr_key" in CONF))    # Nyaa manga search/follows (needs Prowlarr)
ANILIST = "https://graphql.anilist.co"
UA = "Mozilla/5.0 (X11; Linux x86_64) htpc-requests/1.0"
RSS_FOLDER = "Manga"

# media folders: the first of each list is the main drive; a second one is used when the main drive fills up
ANIME_FOLDERS = _folders("anime_folders", [HOME / "Media/Anime", "/mnt/media/Anime"])
MOVIE_FOLDERS = _folders("movie_folders", [HOME / "Media/Anime Movies", "/mnt/media/Anime Movies"])
INTERNAL_ROOT = ANIME_FOLDERS[0]
MOVIE_ROOT_INTERNAL = MOVIE_FOLDERS[0]
MOVIE_ROOT_SSD = MOVIE_FOLDERS[1] if len(MOVIE_FOLDERS) > 1 else ""
MEDIA_ROOTS = [os.path.realpath(p) for p in ANIME_FOLDERS + MOVIE_FOLDERS]
MANGA_FOLDER = Path(CONF.get("manga_folder") or HOME / "Media/Manga/Volumes").expanduser()
STATIC = Path(__file__).resolve().parent.parent / "static"

# requests from these networks count as "at home" (direct play of the original file); Tailscale counts as away
HOME_NETWORKS = [ipaddress.ip_network(n) for n in CONF.get("home_networks") or ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"]]
