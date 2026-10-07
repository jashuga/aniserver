"""Talking to the other programs: Sonarr, Radarr, Prowlarr, qBittorrent, Jellyfin, Kodi, AniList, plus small shared caches."""
import base64
import json
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from aniserver.config import ANILIST, CONF, JELLYFIN, KODI, OWNER, PROWLARR, QBIT, RADARR, SONARR, UA

# ---------- backends ----------


def offline_check(what):
    """Tests set ANISERVER_NO_NETWORK, so a test that forgot to swap in a fake fails loudly instead of changing the real thing."""
    if os.environ.get("ANISERVER_NO_NETWORK"):
        raise RuntimeError(f"tests must not talk to the real {what}")

def prowlarr(method, path, body=None, timeout=90):
    offline_check("Prowlarr")
    req = urllib.request.Request(
        PROWLARR + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"X-Api-Key": CONF["prowlarr_key"], "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        return json.loads(raw) if raw else None


def qbit(path, data=None, timeout=30):
    offline_check("qBittorrent")
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    with urllib.request.urlopen(QBIT + path, data=body, timeout=timeout) as r:
        raw = r.read()
        try:
            return json.loads(raw) if raw else None
        except ValueError:
            return raw.decode()


def sonarr(method, path, body=None, timeout=60):
    offline_check("Sonarr")
    key = re.search(r"<ApiKey>(\w+)</ApiKey>", (Path.home() / ".config/Sonarr/config.xml").read_text()).group(1)
    req = urllib.request.Request(SONARR + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"X-Api-Key": key, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        return json.loads(raw) if raw else None


def graphql(query, variables=None):
    offline_check("AniList")
    req = urllib.request.Request(ANILIST, data=json.dumps({"query": query, "variables": variables or {}}).encode(),
                                 headers={"Content-Type": "application/json", "Accept": "application/json", "User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.load(r)["data"]
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise RuntimeError("AniList is rate-limiting searches; wait a minute and try again.")
        raise


def radarr(method, path, body=None, timeout=60):
    offline_check("Radarr")
    key = re.search(r"<ApiKey>(\w+)</ApiKey>", (Path.home() / ".config/Radarr/config.xml").read_text()).group(1)
    req = urllib.request.Request(RADARR + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"X-Api-Key": key, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        return json.loads(raw) if raw else None
_JF_CACHE = {"t": 0, "paths": {}, "users": {}}


def jellyfin(path, method="GET", body=None):
    offline_check("Jellyfin")
    headers = {"Authorization": f'MediaBrowser Token="{CONF["jellyfin_key"]}"'}
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(JELLYFIN + path, method=method, headers=headers,
                                 data=json.dumps(body).encode() if body is not None else None)
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read()
        return json.loads(raw) if raw else None


def jf_maps():
    """(file path -> Jellyfin item id, user name -> user id), cached for a minute."""
    if time.time() - _JF_CACHE["t"] > 60:
        items = jellyfin("/Items?Recursive=true&IncludeItemTypes=Episode,Movie&Fields=Path")["Items"]
        _JF_CACHE.update(t=time.time(), paths={os.path.realpath(i["Path"]): i["Id"] for i in items if i.get("Path")},
                         users={u["Name"]: u["Id"] for u in jellyfin("/Users")})
    return _JF_CACHE["paths"], _JF_CACHE["users"]


_QBIT_LIVE = {"t": 0, "v": []}
TORRENT_CATEGORIES = ("sonarr", "sonarr-ssd", "radarr", "radarr-ssd", "manga")


def qbit_live():
    """All torrents with live numbers (refreshed at most every 1.5 s)."""
    if time.time() - _QBIT_LIVE["t"] > 1.5:
        try:
            _QBIT_LIVE.update(t=time.time(), v=qbit("/torrents/info") or [])
        except Exception:
            pass
    return _QBIT_LIVE["v"]


def kodi_rpc(method, params=None):
    offline_check("Kodi")
    auth = base64.b64encode(f"kodi:{CONF['kodi_password']}".encode()).decode()
    req = urllib.request.Request(KODI, data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}).encode(),
                                 headers={"Content-Type": "application/json", "Authorization": "Basic " + auth})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)


# ---------- streaming UI: home / title / watch (Jellyfin library, played in the browser) ----------
# The library comes from Jellyfin (artwork, episodes, watch progress per person); playback is Jellyfin's HLS transcode
# (GPU, anime subtitles burned in with their styling) proxied through this site, so the Jellyfin key never reaches a browser.


POOL = ThreadPoolExecutor(12)
ITEM_FIELDS = "Overview,Genres,ProviderIds,Studios,Path,DateCreated,Chapters,MediaStreams,MediaSources,ChildCount,Trickplay"
_SONARR_TYPES = {"t": 0, "data": {}}



_SWR = {}
_SWR_LOCK = threading.Lock()


def swr(key, ttl, fn, max_stale=3600):
    """Cached result of fn(); once older than ttl it is refreshed in the background, so pages never wait on slow backends."""
    ent = _SWR.get(key)
    age = time.time() - ent["t"] if ent else None
    if ent and age < ttl:
        return ent["v"]
    if ent and age < max_stale:
        with _SWR_LOCK:
            if ent.get("busy"):
                return ent["v"]
            ent["busy"] = True

        def refresh():
            try:
                _SWR[key] = {"t": time.time(), "v": fn()}
            except Exception:
                ent["busy"] = False
        threading.Thread(target=refresh, daemon=True).start()
        return ent["v"]
    v = fn()
    _SWR[key] = {"t": time.time(), "v": v}
    return v


_HOME_IP = {"t": 0, "ip": None}


def home_public_ip():
    """This house's internet address (the DDNS name points at it). Devices at home that use the public address come back
    through the router with this address, so they still count as 'home' and get the original file."""
    if time.time() - _HOME_IP["t"] > 300:
        try:
            _HOME_IP["ip"] = socket.gethostbyname(CONF["public_host"])
        except (OSError, KeyError):
            pass
        _HOME_IP["t"] = time.time()
    return _HOME_IP["ip"]


def jf_uid(user):
    _, users = jf_maps()
    return users.get(user) or users.get(OWNER)


def jf_item(uid, iid):
    return jellyfin(f"/Items/{iid}?userId={uid}&Fields={ITEM_FIELDS}")


def series_types():
    """tvdb id -> Sonarr series type ('anime' / 'standard'), cached 5 minutes."""
    if time.time() - _SONARR_TYPES["t"] > 300:
        try:
            _SONARR_TYPES.update(t=time.time(), data={str(s["tvdbId"]): s["seriesType"] for s in sonarr("GET", "/series")})
        except Exception:
            pass
    return _SONARR_TYPES["data"]


def is_anime(series):
    tvdb = str((series.get("ProviderIds") or {}).get("Tvdb") or "")
    kind = series_types().get(tvdb)
    return kind == "anime" if kind else "Anime" in (series.get("Genres") or [])
