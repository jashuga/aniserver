#!/usr/bin/env python3
"""HTPC requests: a small LAN page for
- Anime: searching AniList (understands "re zero", "jjk", romaji/English/synonyms), mapping each result to its
  TVDB id + season (Fribb/anime-lists, AniList relations for brand-new seasons) and adding it to Sonarr, because
  Sonarr's own TVDB search can't find many anime by the names people actually use.
- Manga: searching Nyaa's English digital manga, sending volumes to qBittorrent (category "manga" -> Komga
  "Volumes" library) and following series so new volumes download automatically via qBittorrent RSS rules.
"""
import base64
import calendar
import gzip
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import shutil
import socket
import struct
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

CONF = json.loads((Path.home() / ".config/manga-request/config.json").read_text())
PROWLARR = "http://localhost:9696/api/v1"
QBIT = "http://localhost:8090/api/v2"
RSS_FOLDER = "Manga"


# ---------- remote (internet) logins: username + strong password + TOTP code ----------
# Requests that arrive through the Caddy HTTPS proxy (peer 127.0.0.1 with X-Forwarded-For) are "public".
# Everything else reached port 5050 directly, i.e. from the home network or Tailscale (5050 is not forwarded).


REMOTE_USERS_FILE = Path.home() / ".config/manga-request/remote-users.json"
AUTH_LOG = Path.home() / ".local/share/htpc-web/auth.log"      # read by fail2ban
REMOTE_COOKIE = "htpc_remote"
REMOTE_SESSION_DAYS = 365   # sign in with password + code once per device; stays signed in for a year
# the people (config.json "profiles": login name -> [display name, avatar color]); the first one is the owner
PROFILES = {u: tuple(v) for u, v in (CONF.get("profiles") or {"me": ["Me", "#ff7a1a"]}).items()}
OWNER = next(iter(PROFILES))
REMOTE_NAMES = tuple(PROFILES)   # anyone here can get an internet login at /security; must match their Jellyfin user name
FAILS = {}            # ip -> [timestamps of failed logins]
PENDING = {}          # user -> (password hash, totp secret) during setup


def remote_users():
    try:
        return json.loads(REMOTE_USERS_FILE.read_text())
    except (OSError, ValueError):
        return {}


def save_remote_users(users):
    tmp = REMOTE_USERS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(users, indent=2))
    tmp.chmod(0o600)
    tmp.replace(REMOTE_USERS_FILE)


def hash_password(pw, salt=None, iterations=400_000):
    salt = salt or secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, iterations)
    return f"pbkdf2${iterations}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def check_password(pw, stored):
    try:
        _, it, salt, dk = stored.split("$")
        return hmac.compare_digest(hash_password(pw, base64.b64decode(salt), int(it)).split("$")[3], dk)
    except (ValueError, AttributeError):
        return False


def hotp(secret, counter):
    key = base64.b32decode(secret + "=" * (-len(secret) % 8))
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    o = digest[-1] & 0x0F
    return f"{(struct.unpack('>I', digest[o:o + 4])[0] & 0x7FFFFFFF) % 1_000_000:06d}"


def totp_ok(secret, code, last_counter=0):
    """RFC 6238 (30 s, 6 digits), ±1 step of clock drift; returns the matched counter (codes can't be reused)."""
    code = (code or "").strip().replace(" ", "")
    now = int(time.time() // 30)
    for c in (now - 1, now, now + 1):
        if c > last_counter and hmac.compare_digest(hotp(secret, c), code):
            return c
    return None


TRUST_COOKIE = "htpc_trusted"
TRUST_DAYS = 730      # a device that passed the 2FA code once only needs the password after that


def remote_token(user, exp, purpose="remote"):
    # "gen" goes up when the person changes their password, which invalidates every sign-in made before that
    gen = (remote_users().get(user) or {}).get("gen", 0)
    sig = hmac.new(CONF["secret"].encode(), (f"{user}:{exp}:{purpose}" + (f":{gen}" if gen else "")).encode(), hashlib.sha256).hexdigest()
    return f"{user}:{exp}:{sig}"


def remote_token_user(token, purpose="remote"):
    try:
        user, exp, sig = token.split(":")
        if int(exp) < time.time() or user not in remote_users():
            return None
        return user if hmac.compare_digest(remote_token(user, exp, purpose), token) else None
    except ValueError:
        return None


def cookie_flags(secure):
    # Secure only over HTTPS: at home the site also opens as plain http://<laptop>:5050, where browsers drop Secure cookies
    return "Path=/; HttpOnly; SameSite=Lax" + ("; Secure" if secure else "")


def session_cookies(user, secure=True):
    """Signed-in for a year (sliding), and this browser remembered as trusted for two."""
    exp, texp = int(time.time()) + REMOTE_SESSION_DAYS * 86400, int(time.time()) + TRUST_DAYS * 86400
    return [f"{REMOTE_COOKIE}={remote_token(user, exp)}; Max-Age={REMOTE_SESSION_DAYS * 86400}; {cookie_flags(secure)}",
            f"{TRUST_COOKIE}={remote_token(user, texp, 'trusted')}; Max-Age={TRUST_DAYS * 86400}; {cookie_flags(secure)}"]


def password_problem(new, repeat, current=""):
    if len(new) < 12:
        return "Use at least 12 characters."
    if new != repeat:
        return "The two new passwords don't match."
    if new == current:
        return "That's the same as the current password."
    if new.lower() in [p.lower() for p in CONF.get("reserved_passwords", [])]:
        return "Pick a password you don't use for anything else."
    return None


def change_website_password(user, current, new, repeat):
    """Internet sign-in password (the authenticator code stays the same). Returns an error message or None."""
    users = remote_users()
    rec = users.get(user)
    if not rec:
        return "This profile doesn't have an internet sign-in yet."
    if not check_password(current, rec["pw"]):
        return "wrong-current"
    problem = password_problem(new, repeat, current)
    if problem:
        return problem
    rec["pw"] = hash_password(new)
    rec["gen"] = rec.get("gen", 0) + 1
    save_remote_users(users)
    note_password_changed("Website", user)
    return None


def change_jellyfin_password(user, current, new, repeat):
    """Jellyfin password (apps and the public Jellyfin address), changed as that user after checking the current one."""
    problem = password_problem(new, repeat, current)
    if problem:
        return problem
    client = 'MediaBrowser Client="aniserver", Device="aniserver account page", DeviceId="aniserver-account", Version="1.0"'
    try:
        req = urllib.request.Request(JELLYFIN + "/Users/AuthenticateByName", data=json.dumps({"Username": user, "Pw": current}).encode(),
                                     headers={"Content-Type": "application/json", "Authorization": client})
        with urllib.request.urlopen(req, timeout=20) as r:
            auth = json.load(r)
    except urllib.error.HTTPError:
        return "wrong-current"
    as_user = {"Content-Type": "application/json", "Authorization": f'{client}, Token="{auth["AccessToken"]}"'}
    req = urllib.request.Request(f"{JELLYFIN}/Users/{auth['User']['Id']}/Password", method="POST", headers=as_user,
                                 data=json.dumps({"CurrentPw": current, "NewPw": new}).encode())
    urllib.request.urlopen(req, timeout=20).read()
    try:
        urllib.request.urlopen(urllib.request.Request(f"{JELLYFIN}/Sessions/Logout", method="POST", headers=as_user, data=b""), timeout=10)
    except Exception:
        pass
    note_password_changed("Jellyfin", user)
    return None


CREDENTIALS_FILE = Path.home() / "htpc-credentials.txt"


def note_password_changed(kind, user):
    """Don't leave a stale password in ~/htpc-credentials.txt (the new one isn't written anywhere)."""
    f = CREDENTIALS_FILE
    try:
        text = f.read_text()
    except OSError:
        return
    stamp = f"password: (changed on the website {time.strftime('%Y-%m-%d')})"
    lines = [re.sub(r"password: \S+(\s*\(ask \w+ to change it\))?", stamp, line)
             if kind in line and re.search(rf"\buser: {re.escape(user)}\b", line) else line for line in text.splitlines()]
    f.write_text("\n".join(lines) + "\n")


LOGIN_LOG = Path.home() / ".local/share/htpc-web/logins.log"


def note_login(user, ip, remembered, agent):
    """Successful internet sign-ins, so "why did it ask for the code again?" can be answered (which browser, which way)."""
    LOGIN_LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOGIN_LOG.open("a") as f:
        f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} user={user} ip={ip} {'remembered browser' if remembered else 'code entered'} agent={agent[:200]!r}\n")


def too_many_fails(ip):
    now = time.time()
    FAILS[ip] = [t for t in FAILS.get(ip, []) if now - t < 900]
    return len(FAILS[ip]) >= 5


def record_fail(ip, user):
    FAILS.setdefault(ip, []).append(time.time())
    AUTH_LOG.parent.mkdir(parents=True, exist_ok=True)
    with AUTH_LOG.open("a") as f:
        f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} HTPC AUTH FAIL ip={ip} user={user!r}\n")


# ---------- backends ----------

def prowlarr(method, path, body=None, timeout=90):
    req = urllib.request.Request(
        PROWLARR + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"X-Api-Key": CONF["prowlarr_key"], "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        return json.loads(raw) if raw else None


def qbit(path, data=None, timeout=30):
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    with urllib.request.urlopen(QBIT + path, data=body, timeout=timeout) as r:
        raw = r.read()
        try:
            return json.loads(raw) if raw else None
        except ValueError:
            return raw.decode()


# ---------- manga (Nyaa via Prowlarr -> qBittorrent -> Komga) ----------

VOLUMES = Path.home() / "Media/Manga/Volumes"
RESULTS = {}  # guid -> release from recent searches (keeps Prowlarr download links server-side)


def search(query, digital_only):
    q = f"{query} Digital" if digital_only and "digital" not in query.lower() else query
    params = urllib.parse.urlencode({"query": q, "type": "search", "limit": 100,
                                     "indexerIds": CONF["indexer_id"]})
    results = prowlarr("GET", "/search?" + params)
    results.sort(key=lambda r: -(r.get("seeders") or 0))
    for r in results:
        RESULTS[r["guid"]] = r
    return results


def series_dir(series):
    """Folder for a series' volumes, safe on Windows too (Komelia downloads keep these names)."""
    name = re.sub(r'[<>:"/\\|?*]', "", series).strip().rstrip(".") or "Unknown"
    return VOLUMES / name


def grab(guid):
    r = RESULTS.get(guid)
    if not r:
        raise ValueError("That search result expired. Search again and retry.")
    series, _ = parse_release(r["title"])
    link = r.get("magnetUrl") or r.get("downloadUrl")
    # Straight into Volumes/<Series>/ without the torrent's long folder name, so Komga shows a clean series.
    qbit("/torrents/add", {"urls": link, "category": "manga", "autoTMM": "false",
                           "savepath": str(series_dir(series)), "contentLayout": "NoSubfolder"})


def downloads():
    items = qbit("/torrents/info?sort=added_on&reverse=true") or []
    return [t for t in items if t.get("category") in ("manga", "sonarr", "sonarr-ssd", "radarr", "radarr-ssd")]


STUCK_IMPORTS = ("importBlocked", "importFailed")   # finished, but Sonarr/Radarr can't file it (htpc-watchdog clears or reports it)
TORRENT_STATES = {   # qBittorrent state -> what the Downloads page says
    "downloading": "downloading", "forcedDL": "downloading", "stalledDL": "stalled", "metaDL": "finding", "forcedMetaDL": "finding",
    "queuedDL": "queued", "pausedDL": "paused", "stoppedDL": "paused", "checkingDL": "checking", "checkingResumeData": "checking",
    "allocating": "checking", "moving": "checking", "error": "error", "missingFiles": "error"}


def _download_names():
    """downloadId -> what it is (show + episodes / movie), from Sonarr/Radarr queues and recent history; also which are
    still being imported ("importing") or can't be ("blocked"), and when finished ones were imported."""
    names, pending, imported = {}, {}, {}
    library = {provider_key(i): i["Id"] for i in swr("libitems", 60, lambda: library_items(), 3600) if provider_key(i)}

    def show(rec, series, ep):
        d = names.setdefault(rec["downloadId"].upper(), {"kind": "show", "title": series.get("title") or "", "eps": {},
                                                         "poster": poster_of(series.get("images")),
                                                         "link": library.get(f"s{series.get('tvdbId')}")})
        if ep:
            d["eps"][(ep["seasonNumber"], ep["episodeNumber"])] = ep.get("title") or ""
    for r in sonarr("GET", "/queue?pageSize=1000&includeSeries=true&includeEpisode=true")["records"]:
        if r.get("downloadId"):
            show(r, r.get("series") or {}, r.get("episode"))
            if r.get("trackedDownloadState") in ("importPending", "importing") + STUCK_IMPORTS:
                pending[r["downloadId"].upper()] = "blocked" if r["trackedDownloadState"] in STUCK_IMPORTS else "importing"
    for r in sonarr("GET", "/history?pageSize=250&sortKey=date&sortDirection=descending&includeSeries=true&includeEpisode=true")["records"]:
        if r.get("downloadId") and r.get("eventType") in ("grabbed", "downloadFolderImported"):
            show(r, r.get("series") or {}, r.get("episode"))
            if r["eventType"] == "downloadFolderImported":
                imported.setdefault(r["downloadId"].upper(), r["date"])

    def movie(rec, m):
        names.setdefault(rec["downloadId"].upper(), {"kind": "movie", "title": f"{m.get('title', '')} ({m.get('year', '')})".replace(" ()", ""),
                                                     "eps": {}, "poster": poster_of(m.get("images")),
                                                     "link": library.get(f"m{m.get('tmdbId')}")})
    for r in radarr("GET", "/queue?pageSize=1000&includeMovie=true")["records"]:
        if r.get("downloadId"):
            movie(r, r.get("movie") or {})
            if r.get("trackedDownloadState") in ("importPending", "importing") + STUCK_IMPORTS:
                pending[r["downloadId"].upper()] = "blocked" if r["trackedDownloadState"] in STUCK_IMPORTS else "importing"
    for r in radarr("GET", "/history?pageSize=100&sortKey=date&sortDirection=descending&includeMovie=true")["records"]:
        if r.get("downloadId") and r.get("eventType") in ("grabbed", "downloadFolderImported"):
            movie(r, r.get("movie") or {})
            if r["eventType"] == "downloadFolderImported":
                imported.setdefault(r["downloadId"].upper(), r["date"])
    return names, pending, imported


def library_items():
    return jellyfin("/Items?Recursive=true&IncludeItemTypes=Series,Movie&Fields=ProviderIds")["Items"]


def live_downloads():
    """Everything downloading or finished in the last 3 days: live numbers from qBittorrent, names from Sonarr/Radarr."""
    names, pending, imported = swr("dlnames", 10, _download_names, 600)
    now = time.time()
    items = []
    for t in qbit_live():
        if t.get("category") not in TORRENT_CATEGORIES:
            continue
        h = t["hash"].upper()
        done = (t.get("progress") or 0) >= 1
        if done and now - (t.get("completion_on") or now) > 3 * 86400:
            continue
        n = names.get(h) or {}
        kind = n.get("kind") or ("manga" if t["category"] == "manga" else "movie" if t["category"].startswith("radarr") else "show")
        if kind == "manga":
            title, sub = parse_release(t["name"])[0] or t["name"], t["name"]
        elif n.get("eps"):
            eps = sorted(n["eps"].items())
            (s1, e1), name1 = eps[0]
            sub = (f"S{s1:02d}E{e1:02d}" + (f" · {name1}" if name1 else "") if len(eps) == 1 else
                   f"{len(eps)} episodes · S{s1:02d}E{e1:02d} to S{eps[-1][0][0]:02d}E{eps[-1][0][1]:02d}")
            title = n["title"]
        else:
            title, sub = n.get("title") or t["name"], t["name"] if n.get("title") else ""
        if done:
            state = pending.get(h, "ready")
        else:
            state = TORRENT_STATES.get(t.get("state"), "downloading")
            if state == "downloading" and not t.get("dlspeed"):
                state = "stalled"
        items.append({"id": h, "kind": kind, "title": title, "sub": sub, "poster": n.get("poster") or "",
                      "link": f"/title/{n['link']}" if n.get("link") else "", "progress": round(t.get("progress") or 0, 4),
                      "size": t.get("size") or 0, "done": (t.get("size") or 0) - (t.get("amount_left") or 0),
                      "speed": t.get("dlspeed") or 0, "eta": t["eta"] if 0 < (t.get("eta") or 0) < 8640000 and t.get("dlspeed") else None,
                      "seeds": t.get("num_seeds") or 0, "swarm": max(t.get("num_complete") or 0, 0), "peers": t.get("num_leechs") or 0,
                      "state": state, "added": t.get("added_on") or 0, "finished": t.get("completion_on") if done else None,
                      "imported": imported.get(h), "release": t["name"]})
    order = {"downloading": 0, "stalled": 1, "finding": 1, "checking": 1, "queued": 2, "paused": 3, "error": 0, "importing": 4, "blocked": 4, "ready": 5}
    items.sort(key=lambda i: (order[i["state"]], -(i["finished"] or i["added"])))
    return {"items": items, "speed": sum(i["speed"] for i in items), "now": now}


def parse_release(title):
    """'Gachiakuta v01-12 (2024-2026) (Digital) (Rillant)' -> ('Gachiakuta', 'Rillant')"""
    series = re.split(r"\s+(?:v\d|\d{3}\b|\()", title, maxsplit=1)[0].strip()
    m = re.search(r"\(([^()]+)\)\s*$", title)
    uploader = m.group(1) if m and "digital" not in m.group(1).lower() else ""
    return series, uploader


def feed_url(series):
    # Nyaa RSS: Literature - English-translated (3_1), no remakes
    return "https://nyaa.si/?page=rss&c=3_1&f=1&q=" + urllib.parse.quote_plus(f"{series} Digital")


def follows():
    rules = qbit("/rss/rules") or {}
    out = []
    for name, rule in rules.items():
        if name.startswith("Manga: "):
            out.append({"name": name, "series": name[len("Manga: "):],
                        "uploader": rule.get("mangaUploader", ""), "pattern": rule.get("mustContain", "")})
    return sorted(out, key=lambda r: r["series"].lower())


def follow(series, uploader):
    series = series.strip()
    if not series:
        raise ValueError("Series name is empty.")
    url = feed_url(series)
    item_path = f"{RSS_FOLDER}\\{series}"
    items = qbit("/rss/items") or {}
    if RSS_FOLDER not in items:
        qbit("/rss/addFolder", {"path": RSS_FOLDER})
    if series not in (items.get(RSS_FOLDER) or {}):
        qbit("/rss/addFeed", {"url": url, "path": item_path})
    # Load the feed and mark everything already on Nyaa as read, so only NEW uploads match.
    qbit("/rss/refreshItem", {"itemPath": item_path})
    for _ in range(30):
        feed = ((qbit("/rss/items?withData=true") or {}).get(RSS_FOLDER) or {}).get(series) or {}
        if feed and not feed.get("isLoading") and feed.get("articles") is not None:
            break
        time.sleep(1)
    qbit("/rss/markAsRead", {"itemPath": item_path})
    # Single new volumes only ("Series v13 (2026) (Digital) (Uploader)"), never batch re-uploads (v01-13).
    pattern = r"(?i)^" + re.escape(series) + r"\s+v\d+(\.\d+)?\s+\(.*\(Digital\)"
    if uploader:
        pattern += r".*\(" + re.escape(uploader) + r"\)\s*$"
    folder = str(series_dir(series))
    rule = {"enabled": True, "useRegex": True, "mustContain": pattern, "mustNotContain": "",
            "affectedFeeds": [url], "assignedCategory": "manga", "savePath": folder,
            "torrentParams": {"category": "manga", "save_path": folder, "use_auto_tmm": False,
                              "content_layout": "NoSubfolder"},
            "addPaused": False, "smartFilter": False, "ignoreDays": 0, "mangaUploader": uploader}
    qbit("/rss/setRule", {"ruleName": f"Manga: {series}", "ruleDef": json.dumps(rule)})


def unfollow(series):
    qbit("/rss/removeRule", {"ruleName": f"Manga: {series}"})
    try:
        qbit("/rss/removeItem", {"path": f"{RSS_FOLDER}\\{series}"})
    except urllib.error.HTTPError:
        pass


# ---------- anime (AniList -> TVDB id + season -> Sonarr) ----------

SONARR = "http://localhost:8989/api/v3"
ANILIST = "https://graphql.anilist.co"
UA = "Mozilla/5.0 (X11; Linux x86_64) htpc-requests/1.0"
MAPPING_URL = "https://raw.githubusercontent.com/Fribb/anime-lists/master/anime-list-full.json"
MAPPING_FILE = Path.home() / ".cache/htpc/anime-list-full.json"
INTERNAL_ROOT = str(Path.home() / "Media/Anime")  # the Sonarr add hook moves shows to the SSD when this drive fills
ANIME_PROFILE = "[Anime] Remux-1080p"
_MAPPING = {"mtime": 0, "data": {}, "tmdb": {}}
_TRENDING = {"t": 0, "data": []}
RADARR = "http://localhost:7878/api/v3"
MOVIE_ROOT_INTERNAL = str(Path.home() / "Media/Anime Movies")
MOVIE_ROOT_SSD = "/mnt/media/Anime Movies"
INTERNAL_RESERVE, SSD_RESERVE = 100e9, 20e9   # same drive rule as the Sonarr add hook

MEDIA_FIELDS = """id format status episodes duration season seasonYear startDate{year month day} endDate{year month day}
  title{romaji english} synonyms coverImage{large extraLarge color} bannerImage description(asHtml:false)
  genres averageScore studios(isMain:true){nodes{name}} nextAiringEpisode{episode airingAt} siteUrl"""
SEARCH_QUERY = "query($s:String){Page(perPage:30){media(search:$s,type:ANIME,sort:SEARCH_MATCH,isAdult:false){" + MEDIA_FIELDS + "}}}"
TRENDING_QUERY = ("query{Page(perPage:24){media(type:ANIME,status:RELEASING,sort:TRENDING_DESC,isAdult:false,"
                  "format_in:[TV,TV_SHORT,ONA]){" + MEDIA_FIELDS + "}}}")


def sonarr(method, path, body=None, timeout=60):
    key = re.search(r"<ApiKey>(\w+)</ApiKey>", (Path.home() / ".config/Sonarr/config.xml").read_text()).group(1)
    req = urllib.request.Request(SONARR + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"X-Api-Key": key, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        return json.loads(raw) if raw else None


def graphql(query, variables=None):
    req = urllib.request.Request(ANILIST, data=json.dumps({"query": query, "variables": variables or {}}).encode(),
                                 headers={"Content-Type": "application/json", "Accept": "application/json", "User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.load(r)["data"]
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise RuntimeError("AniList is rate-limiting searches; wait a minute and try again.")
        raise


def anilist_to_tvdb():
    """{anilist_id: (tvdb_id, tvdb_season)} from Fribb/anime-lists, refreshed weekly (stale copy kept on failure)."""
    try:
        if not MAPPING_FILE.exists() or time.time() - MAPPING_FILE.stat().st_mtime > 7 * 86400:
            MAPPING_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = MAPPING_FILE.with_suffix(".tmp")
            with urllib.request.urlopen(urllib.request.Request(MAPPING_URL, headers={"User-Agent": UA}), timeout=60) as r:
                tmp.write_bytes(r.read())
            json.loads(tmp.read_text())  # only replace the cache with valid JSON
            tmp.replace(MAPPING_FILE)
    except Exception:
        pass
    if MAPPING_FILE.exists() and MAPPING_FILE.stat().st_mtime != _MAPPING["mtime"]:
        data, tmdb = {}, {}
        for e in json.loads(MAPPING_FILE.read_text()):
            if e.get("anilist_id") and e.get("tvdb_id"):
                data[e["anilist_id"]] = (e["tvdb_id"], (e.get("season") or {}).get("tvdb"))
            # themoviedb_id is {"movie": [ids]} for films and {"tv": id} for shows; Radarr needs the movie id
            movie_ids = (e.get("themoviedb_id") or {}).get("movie") if isinstance(e.get("themoviedb_id"), dict) else None
            if e.get("anilist_id") and movie_ids:
                tmdb[e["anilist_id"]] = movie_ids[0] if isinstance(movie_ids, list) else movie_ids
        _MAPPING.update(mtime=MAPPING_FILE.stat().st_mtime, data=data, tmdb=tmdb)
    return _MAPPING["data"]


def anilist_to_tmdb():
    anilist_to_tvdb()
    return _MAPPING["tmdb"]


def radarr(method, path, body=None, timeout=60):
    key = re.search(r"<ApiKey>(\w+)</ApiKey>", (Path.home() / ".config/Radarr/config.xml").read_text()).group(1)
    req = urllib.request.Request(RADARR + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"X-Api-Key": key, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        return json.loads(raw) if raw else None


def effective_free(root):
    """Free space on root's drive minus what qBittorrent still has to download onto it."""
    dev = os.stat(root).st_dev
    pending = 0
    for t in qbit("/torrents/info") or []:
        try:
            if t["amount_left"] and os.stat(t["save_path"]).st_dev == dev:
                pending += t["amount_left"]
        except OSError:
            pass
    return shutil.disk_usage(root).free - pending


SPLIT_COUR = re.compile(r"\bpart\s*\d|\b(2nd|second|latter)\s+(part|half|cour)\b|\bcour\s*\d", re.I)


def resolve_via_relations(media, mapping, rounds=3):
    """New seasons often aren't in the mapping yet: walk AniList's prequel/parent links (batched, one request per
    round) to the nearest mapped entry. Returns {anilist_id: (tvdb_id, season_guess_or_None)}."""
    titles = {m["id"]: " ".join(filter(None, [m["title"]["english"], m["title"]["romaji"]])) for m in media}
    found = {}
    state = {m["id"]: (m["id"], 0, True) for m in media}  # node -> (start id, prequel hops, prequel-only chain)
    frontier = list(state)
    seen = set(frontier)
    for _ in range(rounds):
        if not frontier:
            break
        fields = " ".join(f"m{n}: Media(id:{aid}){{id relations{{edges{{relationType node{{id type}}}}}}}}"
                          for n, aid in enumerate(frontier))
        nxt = []
        for node in graphql("query{" + fields + "}").values():
            if not node:
                continue
            start, hops, prequel_only = state[node["id"]]
            if start in found:
                continue
            for e in node["relations"]["edges"]:
                rel, n = e["relationType"], e["node"]
                if rel not in ("PREQUEL", "PARENT") or n["type"] != "ANIME":
                    continue
                chain = prequel_only and rel == "PREQUEL"
                if n["id"] in mapping:
                    tv, s = mapping[n["id"]]
                    guess = None
                    if chain and s:  # sequel of a mapped season: same season if it's a "Part 2", else the next one
                        guess = s + hops + (0 if SPLIT_COUR.search(titles[start]) else 1)
                    found[start] = (tv, guess)
                    break
                if n["id"] not in seen:
                    seen.add(n["id"])
                    state[n["id"]] = (start, hops + 1, chain)
                    nxt.append(n["id"])
        frontier = [i for i in nxt if state[i][0] not in found]
    return found


def anilist_date(d):
    """AniList {year, month, day} -> epoch seconds (UTC midnight), or None when the day isn't known."""
    if not d or not (d.get("year") and d.get("month") and d.get("day")):
        return None
    return calendar.timegm((d["year"], d["month"], d["day"], 0, 0, 0))


def air_time(e):
    return calendar.timegm(time.strptime(e["airDateUtc"][:19], "%Y-%m-%dT%H:%M:%S")) if e.get("airDateUtc") else None


def entry_episodes(episodes, media, slack_days=2, horizon_days=120):
    """The TVDB episodes (Sonarr episode records) that make up one AniList entry, matched by AIR DATE rather than by
    season number - TVDB often numbers seasons differently from the anime (a continuing season, split cours, ONAs:
    e.g. JoJo: Steel Ball Run's 2nd stage is TVDB season 6 episodes 2-12).
    - OVAs/specials look at TVDB's specials first; TV entries never take specials.
    - A finished entry never gets fewer episodes than it has (Netflix-first releases: TVDB often has the later TV dates),
      and a match much bigger than the entry is rejected (an OVA released during a TV season).
    - An airing entry stops at its episode count, so it doesn't run on into the next entry.
    Returns [] when it can't tell (the caller then falls back to the season number)."""
    start = anilist_date(media.get("startDate"))
    if start is None:
        return []
    finished, count = media.get("status") == "FINISHED", media.get("episodes")
    end = anilist_date(media.get("endDate")) if finished else None
    lo, hi = start - slack_days * 86400, (end + slack_days * 86400) if end is not None else None
    by_air = lambda e: (air_time(e), e["seasonNumber"], e["episodeNumber"])
    regular = sorted((e for e in episodes if e["seasonNumber"] > 0 and air_time(e) is not None), key=by_air)
    specials = sorted((e for e in episodes if e["seasonNumber"] == 0 and air_time(e) is not None), key=by_air)
    in_window = lambda pool: [e for e in pool if air_time(e) >= lo and (hi is None or air_time(e) <= hi)]
    found = []
    for pool in ((specials, regular) if media.get("format") in ("OVA", "SPECIAL", "MOVIE") else (regular,)):
        found = in_window(pool)
        if not found and finished and count and pool is regular:
            # dated differently on TVDB (e.g. on Netflix before TV): the entry is the run of episodes that follows
            found = [e for e in pool if lo <= air_time(e) <= lo + horizon_days * 86400][:count]
        if found:
            break
    if not found or not count:
        return found
    if not finished:
        return found[:count]
    if len(found) > count * 1.5 + 2:
        return []
    if len(found) < count and found[0]["seasonNumber"] > 0:
        in_order = sorted(regular, key=lambda e: (e["seasonNumber"], e["episodeNumber"]))
        first = in_order.index(found[0])
        found = in_order[first:first + count]
    return found


def describe_episodes(eps):
    """[S6E2..S6E12] -> 'season 6, episodes 2-12'."""
    if not eps:
        return ""
    seasons = sorted({e["seasonNumber"] for e in eps})
    if len(seasons) == 1:
        nums = [e["episodeNumber"] for e in eps]
        label = "specials" if seasons[0] == 0 else f"season {seasons[0]}"
        return f"{label}, episode {nums[0]}" if len(nums) == 1 else f"{label}, episodes {min(nums)}-{max(nums)}"
    a, b = eps[0], eps[-1]
    return f"S{a['seasonNumber']}E{a['episodeNumber']} to S{b['seasonNumber']}E{b['episodeNumber']}"


_TVDB_SEASONS = {}


def tvdb_seasons(tvdb_id, library=None):
    """Season numbers TVDB lists for a show (from Sonarr; cached 30 minutes)."""
    if library and tvdb_id in library:
        return {x["seasonNumber"] for x in library[tvdb_id]["seasons"]}
    hit = _TVDB_SEASONS.get(tvdb_id)
    if not hit or time.time() - hit[0] > 1800:
        try:
            found = sonarr("GET", f"/series/lookup?term=tvdb:{tvdb_id}")
            hit = (time.time(), {x["seasonNumber"] for x in (found[0]["seasons"] if found else [])})
        except Exception:
            hit = (time.time(), set())
        _TVDB_SEASONS[tvdb_id] = hit
    return hit[1]


def settle_season(tvdb_id, guess, library=None):
    """A guessed 'next season' that TVDB doesn't have is usually TVDB continuing the previous season
    (e.g. JoJo: Steel Ball Run's 2nd stage is TVDB season 6 episodes 2+, not a season 7) -> use TVDB's latest season."""
    if not guess:
        return guess
    seasons = {s for s in tvdb_seasons(tvdb_id, library) if s > 0}
    if not seasons or guess in seasons:
        return guess
    lower = [s for s in seasons if s < guess]
    return max(lower) if lower else guess


def clean_description(d):
    if not d:
        return ""
    d = re.sub(r"<br\s*/?>", "\n", d, flags=re.I)
    d = html.unescape(re.sub(r"<[^>]+>", "", d))
    d = re.sub(r"\(Source:[^)]*\)\s*$", "", d.strip()).strip()
    return re.sub(r"\n{3,}", "\n\n", d)


def title_of(m):
    return m["title"]["english"] or m["title"]["romaji"]


def build_cards(media):
    """One card per AniList entry (season, OVA, side story...), annotated with its TVDB series/season and
    whether that is already in Sonarr."""
    mapping, tmdbmap = anilist_to_tvdb(), anilist_to_tmdb()
    media = [m for m in media if m["format"] != "MUSIC"]
    unmapped = [m for m in media if m["id"] not in mapping and m["format"] != "MOVIE"]
    via = resolve_via_relations(unmapped, mapping) if unmapped else {}
    library = {s["tvdbId"]: s for s in sonarr("GET", "/series")}
    movies = {x["tmdbId"]: x for x in radarr("GET", "/movie")} if any(m["format"] == "MOVIE" for m in media) else {}
    episodes = {}

    def entry_eps(series, m):
        if series["id"] not in episodes:
            try:
                episodes[series["id"]] = sonarr("GET", f"/episode?seriesId={series['id']}")
            except Exception:
                episodes[series["id"]] = []
        return entry_episodes(episodes[series["id"]], m)
    cards = []
    for m in media:
        if m["format"] == "MOVIE":
            tmdb = tmdbmap.get(m["id"])
            movie = movies.get(tmdb) if tmdb else None
            cards.append({"m": m, "tvdb": None, "tmdb": tmdb, "season": None, "guessed": False, "series": None,
                          "season_info": None, "movie": movie,
                          "state": ("movie_done" if movie["hasFile"] else "movie_added") if movie else "movie_new"})
            continue
        tv, season, guessed = None, None, False
        if m["id"] in mapping:
            tv, season = mapping[m["id"]]
        elif m["id"] in via:
            (tv, season), guessed = via[m["id"]], True
            season = settle_season(tv, season, library)
        series = library.get(tv) if tv else None
        matched = entry_eps(series, m) if series else []
        if matched:   # the show is in Sonarr: its TVDB air dates say exactly which season/episodes this entry is
            season, guessed = max({e["seasonNumber"] for e in matched}, key=[e["seasonNumber"] for e in matched].count), False
        season_info = None
        if series and season is not None:
            season_info = next((x for x in series["seasons"] if x["seasonNumber"] == season), None)
        if series and matched:
            state = "have" if all(e["monitored"] or e.get("hasFile") for e in matched) else "season"
        elif series and (season is None or (season_info and season_info["monitored"])):
            state = "have"
        elif series:
            state = "season"      # show is in Sonarr, this season isn't monitored yet
        else:
            state = "new"
        cards.append({"m": m, "tvdb": tv, "tmdb": None, "season": season, "guessed": guessed, "series": series,
                      "season_info": season_info, "movie": None, "state": state})
    # keep each show's entries together (at the position of its best match), in season order; specials and
    # side stories after the numbered seasons
    first = {}
    for i, c in enumerate(cards):
        first.setdefault(c["tvdb"] or ("al", c["m"]["id"]), i)

    def order(c):
        s = c["season"]
        year = c["m"].get("seasonYear") or (c["m"].get("startDate") or {}).get("year") or 9999
        return (first[c["tvdb"] or ("al", c["m"]["id"])], c["m"]["format"] == "MOVIE", s is None or s == 0, s or 0, year)
    return sorted(cards, key=order)


def anime_search(query):
    return build_cards(graphql(SEARCH_QUERY, {"s": query})["Page"]["media"])


def trending():
    if time.time() - _TRENDING["t"] > 1800:
        _TRENDING.update(t=time.time(), data=graphql(TRENDING_QUERY)["Page"]["media"])
    return build_cards(_TRENDING["data"])


def match_by_title(anilist_id):
    """Last resort for anime missing from the mapping: Sonarr's lookup with each AniList title, same start year."""
    m = graphql("query($id:Int){Media(id:$id){id startDate{year} title{romaji english} synonyms}}", {"id": anilist_id})["Media"]
    year = (m.get("startDate") or {}).get("year")
    for t in [m["title"]["english"], m["title"]["romaji"], *(m.get("synonyms") or [])]:
        if not t:
            continue
        for s in sonarr("GET", "/series/lookup?term=" + urllib.parse.quote(t)) or []:
            if year and s.get("year") and abs(s["year"] - year) <= 1:
                return s["tvdbId"]
    raise ValueError("Couldn't match this one to Sonarr's catalog. Try Sonarr's own search with its English title.")


def match_movie_by_title(anilist_id):
    """For anime movies missing from the mapping: Radarr's lookup with each AniList title, same release year."""
    m = graphql("query($id:Int){Media(id:$id){id startDate{year} title{romaji english} synonyms}}", {"id": anilist_id})["Media"]
    year = (m.get("startDate") or {}).get("year")
    for t in [m["title"]["english"], m["title"]["romaji"], *(m.get("synonyms") or [])]:
        if not t:
            continue
        for x in radarr("GET", "/movie/lookup?term=" + urllib.parse.quote(t)) or []:
            if year and x.get("year") and abs(x["year"] - year) <= 1:
                return x["tmdbId"]
    raise ValueError("Couldn't match this movie to Radarr's catalog. Try Radarr's own search with its English title.")


def movie_root():
    """Internal drive while it has room (after pending downloads), else the USB SSD (tagged 'ssd' so the
    download goes to the SSD too and hardlinks keep working)."""
    if effective_free(MOVIE_ROOT_INTERNAL) >= INTERNAL_RESERVE or not Path(MOVIE_ROOT_SSD).is_dir():
        return MOVIE_ROOT_INTERNAL, []
    if effective_free(MOVIE_ROOT_SSD) > SSD_RESERVE:
        return MOVIE_ROOT_SSD, [next(t["id"] for t in radarr("GET", "/tag") if t["label"] == "ssd")]
    return MOVIE_ROOT_INTERNAL, []


def movie_add(tmdb_id, anilist_id):
    if not tmdb_id:
        tmdb_id = match_movie_by_title(anilist_id)
    existing = next((x for x in radarr("GET", "/movie") if x["tmdbId"] == tmdb_id), None)
    if existing:
        return existing["title"], "It's already in Radarr."
    mv = radarr("GET", f"/movie/lookup/tmdb?tmdbId={tmdb_id}")
    profile = next(p["id"] for p in radarr("GET", "/qualityprofile") if p["name"] == ANIME_PROFILE)
    root, tags = movie_root()
    mv.update(qualityProfileId=profile, rootFolderPath=root, monitored=True, minimumAvailability="released",
              tags=tags, addOptions={"searchForMovie": True, "monitor": "movieOnly"})
    added = radarr("POST", "/movie", mv)
    where = " on the USB SSD" if root == MOVIE_ROOT_SSD else ""
    return added["title"], f"Added{where}. Radarr is searching for it now; it appears in Kodi's Movies when it's done."


def anilist_entry(anilist_id):
    return graphql("query($id:Int){Media(id:$id){id format status episodes startDate{year month day} endDate{year month day}}}",
                   {"id": anilist_id})["Media"]


def monitor_entry(series_id, media, season_hint=None):
    """Monitor exactly the episodes of one AniList entry (matched by air date; the season number is only a fallback)
    and search the ones that have aired. Returns (description, aired count), or None if TVDB lists nothing for it yet."""
    for _ in range(20):  # a just-added show needs a few seconds for Sonarr to load its episode list
        eps = sonarr("GET", f"/episode?seriesId={series_id}")
        if eps:
            break
        time.sleep(1.5)
    picked = entry_episodes(eps, media)
    upcoming = media.get("status") == "NOT_YET_RELEASED" or (anilist_date(media.get("startDate")) or 0) > time.time()
    if not picked and season_hint is not None and not upcoming:    # an upcoming entry must not grab the old season
        picked = sorted((e for e in eps if e["seasonNumber"] == season_hint), key=lambda e: e["episodeNumber"])
    if not picked:
        return None
    ids = {e["id"] for e in picked}
    was = {e["id"] for e in eps if e["monitored"]}
    last = {}
    for e in eps:
        if e["seasonNumber"] not in last or e["episodeNumber"] > last[e["seasonNumber"]]["episodeNumber"]:
            last[e["seasonNumber"]] = e
    # a season this entry runs to the end of stays monitored, so episodes TVDB lists later download too
    open_seasons = {n for n, e in last.items() if e["id"] in ids}
    s = sonarr("GET", f"/series/{series_id}")
    s["monitored"] = True
    if media.get("status") in ("RELEASING", "NOT_YET_RELEASED"):
        s["monitorNewItems"] = "all"
    for x in s["seasons"]:
        if x["seasonNumber"] in open_seasons:
            x["monitored"] = True
    sonarr("PUT", f"/series/{series_id}?moveFiles=false", s)
    # monitoring a season can switch on its earlier episodes too; keep those as they were
    extra = [e["id"] for e in eps if e["seasonNumber"] in open_seasons and e["id"] not in ids and e["id"] not in was]
    if extra:
        sonarr("PUT", "/episode/monitor", {"episodeIds": extra, "monitored": False})
    sonarr("PUT", "/episode/monitor", {"episodeIds": sorted(ids), "monitored": True})
    now = time.time()
    aired = [e["id"] for e in picked if not e.get("hasFile") and air_time(e) is not None and air_time(e) <= now]
    if aired:
        sonarr("POST", "/command", {"name": "EpisodeSearch", "episodeIds": aired})
    return describe_episodes(picked), len(aired)


def follow_new_episodes(series_id):
    """Keep the show monitored with every not-yet-aired episode (and any new season TVDB adds) monitored."""
    s = sonarr("GET", f"/series/{series_id}")
    s["monitored"] = True
    s["monitorNewItems"] = "all"
    sonarr("PUT", f"/series/{series_id}?moveFiles=false", s)
    future = [e["id"] for e in sonarr("GET", f"/episode?seriesId={series_id}")
              if not e.get("hasFile") and (not e.get("airDateUtc") or e["airDateUtc"] > time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))]
    if future:
        sonarr("PUT", "/episode/monitor", {"episodeIds": future, "monitored": True})


def entry_result(series_id, media, season):
    """Monitor one entry; if TVDB has nothing for it yet, keep the show following new episodes (never leave it off)."""
    res = monitor_entry(series_id, media, season)
    if res is None:
        follow_new_episodes(series_id)
        return "- it isn't listed on TVDB yet, so new episodes will download as soon as they're listed."
    what, aired = res
    return (f"{what}; Sonarr is searching for the {aired} that ha{'s' if aired == 1 else 've'} aired now." if aired else
            f"{what}; episodes download as they air.")


def anime_add(tvdb_id, anilist_id, season, mode):
    """mode: 'season' (just that season), 'all' (every season), 'future' (only episodes that haven't aired)."""
    if not tvdb_id:
        hit = resolve_via_relations([graphql("query($id:Int){Media(id:$id){id title{romaji english}}}",
                                             {"id": anilist_id})["Media"]], anilist_to_tvdb()).get(anilist_id)
        tvdb_id, season = hit if hit else (match_by_title(anilist_id), None)
    season = settle_season(tvdb_id, season)
    media = anilist_entry(anilist_id) if mode == "season" and anilist_id else {}
    if mode == "season" and season is None and anilist_date(media.get("startDate")) is None:
        mode = "all"
    existing = next((s for s in sonarr("GET", "/series") if s["tvdbId"] == tvdb_id), None)
    if existing:
        if mode == "season":
            return existing["title"], entry_result(existing["id"], media, season)
        return existing["title"], "It's already in Sonarr."
    s = sonarr("GET", f"/series/lookup?term=tvdb:{tvdb_id}")[0]
    profile = next(p["id"] for p in sonarr("GET", "/qualityprofile") if p["name"] == ANIME_PROFILE)
    for x in s["seasons"]:
        x["monitored"] = mode != "season" and x["seasonNumber"] > 0
    monitor = {"season": "none", "all": "all", "future": "future"}[mode]
    s.update(qualityProfileId=profile, seriesType="anime", rootFolderPath=INTERNAL_ROOT, monitored=True,
             seasonFolder=True, addOptions={"monitor": monitor, "searchForMissingEpisodes": mode == "all"})
    added = sonarr("POST", "/series", s)
    if mode == "season":
        return added["title"], "Added " + entry_result(added["id"], media, season)
    if mode == "future":
        return added["title"], "Added. New episodes will download as they air."
    return added["title"], "Added. Sonarr is searching for every episode now; downloads start within a few minutes."


# ---------- "My shows" hub: status, watch here (Jellyfin), play on TV (Kodi) ----------

KODI = "http://localhost:8085/jsonrpc"
JELLYFIN = "http://localhost:8096"
MEDIA_ROOTS = [os.path.realpath(p) for p in (INTERNAL_ROOT, "/mnt/media/Anime", MOVIE_ROOT_INTERNAL, MOVIE_ROOT_SSD)]
_JF_CACHE = {"t": 0, "paths": {}, "users": {}}
SEARCH_COMMANDS = ("SeriesSearch", "SeasonSearch", "EpisodeSearch", "MissingEpisodeSearch", "MoviesSearch")


def jellyfin(path, method="GET", body=None):
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


def jellyfin_base(host, public):
    return f"https://{CONF['watch_host']}" if public else f"http://{host}:8096"


def media_path(path):
    """Only files inside the anime/movie libraries can be opened or played."""
    real = os.path.realpath(path or "")
    if not any(real.startswith(root + os.sep) for root in MEDIA_ROOTS) or not os.path.isfile(real):
        raise ValueError("That file isn't in the library (any more).")
    return real


def link_sig(path, exp):
    return hmac.new(CONF["secret"].encode(), f"{path}|{exp}|link".encode(), hashlib.sha256).hexdigest()[:32]


def signed_qs(path, days=21):
    exp = int(time.time()) + days * 86400
    return urllib.parse.urlencode({"p": path, "exp": exp, "sig": link_sig(path, exp)})


def signature_ok(qs):
    try:
        p, exp, sig = qs["p"][0], int(qs["exp"][0]), qs["sig"][0]
        return exp > time.time() and hmac.compare_digest(link_sig(p, exp), sig)
    except (KeyError, ValueError, IndexError):
        return False


def kodi_play(path):
    """Start a file on the TV: Kodi hands it to mpv (playercorefactory), like picking it in Kodi."""
    path = media_path(path)
    auth = base64.b64encode(f"kodi:{CONF['kodi_password']}".encode()).decode()
    req = urllib.request.Request(KODI, data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "Player.Open",
                                                        "params": {"item": {"file": path}}}).encode(),
                                 headers={"Content-Type": "application/json", "Authorization": "Basic " + auth})
    with urllib.request.urlopen(req, timeout=15) as r:
        res = json.load(r)
    if "error" in res:
        raise RuntimeError(f"Kodi said: {res['error'].get('message')}")


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


def queue_numbers(r, live):
    """Size / left / eta / speed for a Sonarr/Radarr queue record: qBittorrent's live numbers when it has the torrent
    (Sonarr/Radarr only refresh theirs about once a minute)."""
    t = live.get((r.get("downloadId") or "").upper())
    if t:
        return {"size": t.get("size") or 0, "left": t.get("amount_left") or 0, "speed": t.get("dlspeed") or 0,
                "eta": t["eta"] if 0 < (t.get("eta") or 0) < 8640000 and t.get("dlspeed") else 0}
    return {"size": r.get("size") or 0, "left": r.get("sizeleft") or 0, "speed": 0, "eta": parse_timeleft(r.get("timeleft"))}


def kodi_rpc(method, params=None):
    auth = base64.b64encode(f"kodi:{CONF['kodi_password']}".encode()).decode()
    req = urllib.request.Request(KODI, data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}).encode(),
                                 headers={"Content-Type": "application/json", "Authorization": "Basic " + auth})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)


DELETE_LOG = Path.home() / ".local/share/htpc-web/deleted.log"


def delete_title(key, who):
    """Remove a show ('s<tvdb>') or movie ('m<tmdb>') completely: cancel its downloads, delete its files (library copies
    and the qBittorrent copies, so the space is really freed) and update Kodi/Jellyfin. Returns (title, bytes freed)."""
    hashes = set()
    if key.startswith("s"):
        s = next(x for x in sonarr("GET", "/series") if f"s{x['tvdbId']}" == key)
        hashes |= {r["downloadId"].upper() for r in sonarr("GET", f"/history/series?seriesId={s['id']}") if r.get("downloadId")}
        for r in sonarr("GET", "/queue?pageSize=1000")["records"]:
            if r.get("seriesId") == s["id"]:
                hashes.add((r.get("downloadId") or "").upper())
                sonarr("DELETE", f"/queue/{r['id']}?removeFromClient=true&blocklist=false")
        title, size = s["title"], (s.get("statistics") or {}).get("sizeOnDisk") or 0
        sonarr("DELETE", f"/series/{s['id']}?deleteFiles=true&addImportListExclusion=false")
    else:
        m = next(x for x in radarr("GET", "/movie") if f"m{x['tmdbId']}" == key)
        hashes |= {r["downloadId"].upper() for r in radarr("GET", f"/history/movie?movieId={m['id']}") if r.get("downloadId")}
        for r in radarr("GET", "/queue?pageSize=1000")["records"]:
            if r.get("movieId") == m["id"]:
                hashes.add((r.get("downloadId") or "").upper())
                radarr("DELETE", f"/queue/{r['id']}?removeFromClient=true&blocklist=false")
        title, size = f"{m['title']} ({m.get('year')})", m.get("sizeOnDisk") or 0
        radarr("DELETE", f"/movie/{m['id']}?deleteFiles=true&addImportExclusion=false")
    present = {t["hash"].upper() for t in qbit("/torrents/info") or []}
    gone = [h.lower() for h in hashes if h and h in present]
    if gone:
        qbit("/torrents/delete", {"hashes": "|".join(gone), "deleteFiles": "true"})
    for refresh in (lambda: jellyfin("/Library/Refresh", "POST"), lambda: kodi_rpc("VideoLibrary.Clean", {"showdialogs": False})):
        try:
            refresh()
        except Exception:
            pass
    for k in ("dlcards", "dlnames", "libitems"):
        _SWR.pop(k, None)
    _JF_CACHE["t"] = 0
    DELETE_LOG.parent.mkdir(parents=True, exist_ok=True)
    with DELETE_LOG.open("a") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {who} deleted {title} ({size / 1e9:.1f} GB, {len(gone)} torrents)\n")
    return title, size


def human_eta(seconds):
    seconds = int(seconds or 0)
    if seconds <= 0:
        return ""
    if seconds < 90:
        return "under 2 min left"
    if seconds < 3600:
        return f"{seconds // 60} min left"
    return f"{seconds // 3600} h {seconds % 3600 // 60:02d} min left"


def parse_timeleft(t):
    """Sonarr/Radarr '01:02:03' or '1.02:03:04' -> seconds."""
    try:
        days = 0
        if "." in t.split(":")[0]:
            d, t = t.split(".", 1)
            days = int(d)
        h, m, s = (int(float(x)) for x in t.split(":"))
        return days * 86400 + h * 3600 + m * 60 + s
    except (ValueError, AttributeError):
        return 0


def poster_of(images):
    return next((i.get("remoteUrl") for i in images or [] if i.get("coverType") == "poster" and i.get("remoteUrl")), "")


def hub_cards(with_files=True):
    """Everything added to Sonarr/Radarr, with live status (and, for My Shows, the downloaded episode files)."""
    series = sonarr("GET", "/series")
    queue = sonarr("GET", "/queue?pageSize=1000&includeEpisode=true")["records"]
    commands = [c for c in sonarr("GET", "/command") if c["status"] in ("queued", "started") and c["name"] in SEARCH_COMMANDS]
    movies = radarr("GET", "/movie")
    mqueue = radarr("GET", "/queue?pageSize=1000")["records"]
    mcommands = [c for c in radarr("GET", "/command") if c["status"] in ("queued", "started") and c["name"] in SEARCH_COMMANDS]
    live = {t["hash"].upper(): t for t in qbit_live()}
    cards = []
    for s in series:
        st = s.get("statistics") or {}
        q = [r for r in queue if r.get("seriesId") == s["id"]]
        downloads = {}
        for r in q:
            if r.get("trackedDownloadState") in STUCK_IMPORTS:
                continue      # finished but Sonarr can't import it (e.g. an extra Blu-ray copy): not "downloading"
            d = downloads.setdefault(r.get("downloadId") or r["title"], {**queue_numbers(r, live), "eps": 0,
                                                                         "state": r.get("trackedDownloadState") or r.get("status")})
            d["eps"] += 1
        searching = any((c.get("body") or {}).get("seriesId") == s["id"] for c in commands)
        eps = []
        if with_files and st.get("episodeFileCount"):
            files = {f["id"]: f["path"] for f in sonarr("GET", f"/episodefile?seriesId={s['id']}")}
            for e in sonarr("GET", f"/episode?seriesId={s['id']}"):
                if e.get("hasFile") and e.get("episodeFileId") in files:
                    eps.append({"label": f"S{e['seasonNumber']:02d}E{e['episodeNumber']:02d}", "title": e.get("title") or "",
                                "path": files[e["episodeFileId"]], "key": (e["seasonNumber"], e["episodeNumber"])})
            eps.sort(key=lambda x: x["key"])
        missing = max(0, (st.get("episodeCount") or 0) - (st.get("episodeFileCount") or 0))
        cards.append({"kind": "show", "id": f"s{s['tvdbId']}", "title": s["title"], "year": s.get("year"),
                      "poster": poster_of(s.get("images")), "downloads": list(downloads.values()), "searching": searching,
                      "files": eps, "missing": missing, "next": s.get("nextAiring"), "added": s.get("added") or "",
                      "status": s.get("status"), "size": st.get("sizeOnDisk") or 0, "count": st.get("episodeFileCount") or 0})
    for m in movies:
        q = [r for r in mqueue if r.get("movieId") == m["id"] and r.get("trackedDownloadState") not in STUCK_IMPORTS]
        downloads = [{**queue_numbers(r, live), "eps": 1, "state": r.get("trackedDownloadState") or r.get("status")} for r in q]
        searching = any(m["id"] in ((c.get("body") or {}).get("movieIds") or []) for c in mcommands)
        files = [{"label": "Movie", "title": "", "path": m["movieFile"]["path"], "key": (0, 0)}] if m.get("hasFile") and m.get("movieFile") else []
        cards.append({"kind": "movie", "id": f"m{m['tmdbId']}", "title": m["title"], "year": m.get("year"),
                      "poster": poster_of(m.get("images")), "downloads": downloads, "searching": searching, "files": files,
                      "missing": 0 if files else 1, "next": None, "added": m.get("added") or "", "status": m.get("status"),
                      "size": m.get("sizeOnDisk") or 0, "count": 1 if m.get("hasFile") else 0})
    active = lambda c: bool(c["downloads"] or c["searching"])
    cards.sort(key=lambda c: (not active(c), -(time.mktime(time.strptime(c["added"][:19], "%Y-%m-%dT%H:%M:%S")) if c["added"] else 0)))
    return cards


def continue_watching(user):
    """Jellyfin's resume list for this person, newest first."""
    _, users = jf_maps()
    uid = users.get(user) or users.get(OWNER)
    if not uid:
        return []
    items = jellyfin(f"/UserItems/Resume?userId={uid}&limit=12&Fields=Path&MediaTypes=Video&enableImages=true")["Items"]
    out = []
    for i in items:
        try:
            path = media_path(i.get("Path"))
        except ValueError:
            continue
        label = (f"S{i.get('ParentIndexNumber', 0):02d}E{i.get('IndexNumber', 0):02d} · {i['Name']}" if i.get("Type") == "Episode" else "Movie")
        out.append({"title": i.get("SeriesName") or i["Name"], "label": label, "path": path, "id": i["Id"],
                    "image_id": i.get("SeriesId") or i["Id"], "pct": (i.get("UserData") or {}).get("PlayedPercentage") or 0})
    return out


def next_up(user):
    """Jellyfin's 'next episode to watch' per show for this person: {series name: episode}."""
    _, users = jf_maps()
    uid = users.get(user) or users.get(OWNER)
    if not uid:
        return {}
    items = jellyfin(f"/Shows/NextUp?userId={uid}&limit=100&Fields=Path&enableResumable=false")["Items"]
    return {os.path.realpath(i["Path"]): i for i in items if i.get("Path")}


# ---------- streaming UI: home / title / watch (Jellyfin library, played in the browser) ----------
# The library comes from Jellyfin (artwork, episodes, watch progress per person); playback is Jellyfin's HLS transcode
# (GPU, anime subtitles burned in with their styling) proxied through this site, so the Jellyfin key never reaches a browser.


POOL = ThreadPoolExecutor(12)
STATIC = Path(__file__).resolve().parent / "static"
IMG_WIDTHS = (160, 240, 360, 480, 640, 960, 1280, 1920)
ITEM_FIELDS = "Overview,Genres,ProviderIds,Studios,Path,DateCreated,Chapters,MediaStreams,MediaSources,ChildCount,Trickplay"
QUALITIES = {   # key: (label, video bitrate, max width) for converted streams; "auto" plays the original whenever possible
    "max": ("Best · 1080p", 20_000_000, 1920),
    "high": ("1080p · 10 Mbps", 10_000_000, 1920),
    "1080": ("1080p", 8_000_000, 1920),
    "720": ("720p", 4_000_000, 1280),
    "480": ("480p · data saver", 1_500_000, 854),
}
STREAM_PATH = re.compile(r"/stream/([0-9a-f]{32})/(master\.m3u8|main\.m3u8|hls1/main/-?\d+\.(?:ts|mp4)|file|sub/\d+\.ass|att/\d+|trick/\d+/\d+\.jpg)")
FONT_CACHE = Path.home() / ".cache/htpc/font-names.json"
_FONT_NAMES = {}
PLAYER_LOG = Path.home() / ".local/share/htpc-web/player.log"
_FILE_PATHS = {}
LADDER = [(1920, 10_000_000), (1920, 6_000_000), (1280, 3_500_000), (854, 1_800_000), (640, 900_000)]   # adaptive levels (width, video bps)
LADDER_HEVC = [(1920, 6_000_000), (1920, 3_500_000), (1280, 2_200_000), (1280, 1_300_000), (854, 650_000)]   # same picture, ~35% less data
RATES_CACHE = Path.home() / ".cache/htpc/bitrates.json"
_RATES = {}
REMOTE_MAX_BITRATE = 45_000_000   # home upload measured 100-200 Mbps; above this, away-from-home viewers get a converted stream
HEX32 = re.compile(r"[0-9a-f]{32}")
LANGS = {"eng": "English", "jpn": "Japanese", "enm": "English", "spa": "Spanish", "fra": "French", "fre": "French",
         "deu": "German", "ger": "German", "ita": "Italian", "por": "Portuguese", "rus": "Russian", "ara": "Arabic",
         "zho": "Chinese", "chi": "Chinese", "kor": "Korean", "pol": "Polish", "nld": "Dutch", "dut": "Dutch",
         "swe": "Swedish", "nor": "Norwegian", "nob": "Norwegian", "dan": "Danish", "fin": "Finnish", "tur": "Turkish",
         "hin": "Hindi", "ind": "Indonesian", "tha": "Thai", "vie": "Vietnamese", "msa": "Malay", "may": "Malay",
         "heb": "Hebrew", "ell": "Greek", "gre": "Greek", "hun": "Hungarian", "ces": "Czech", "cze": "Czech",
         "ron": "Romanian", "rum": "Romanian", "ukr": "Ukrainian", "hrv": "Croatian", "und": "Unknown"}
OPENING = re.compile(r"^(op|opening|op\s*\d+|opening (song|credits|theme))$", re.I)
ENDING = re.compile(r"^(ed|ending|ed\s*\d+|ending (song|credits|theme)|outro|credits|end credits)$", re.I)
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


_IMG_CACHE = OrderedDict()
_IMG_LOCK = threading.Lock()
IMG_CACHE_BYTES = 96 * 1024 * 1024


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


def img_url(item_id, kind, w, tag=None):
    return f"/img/{item_id}/{kind}?w={w}" + (f"&t={tag[:10]}" if tag else "")


def thumb_of(i, w=480):
    """16:9 picture for an episode/movie card: the episode still, else the show's landscape art."""
    tags = i.get("ImageTags") or {}
    if i.get("Type") == "Episode" and tags.get("Primary"):
        return img_url(i["Id"], "Primary", w, tags["Primary"])
    if i.get("ParentThumbItemId"):
        return img_url(i["ParentThumbItemId"], "Thumb", w, i.get("ParentThumbImageTag"))
    if i.get("ParentBackdropItemId"):
        return img_url(i["ParentBackdropItemId"], "Backdrop", w, (i.get("ParentBackdropImageTags") or [None])[0])
    if tags.get("Thumb"):
        return img_url(i["Id"], "Thumb", w, tags["Thumb"])
    if i.get("BackdropImageTags"):
        return img_url(i["Id"], "Backdrop", w, i["BackdropImageTags"][0])
    return img_url(i["Id"], "Primary", w, tags.get("Primary"))


def backdrop_of(i, w=1920):
    tags = i.get("ImageTags") or {}
    if i.get("BackdropImageTags"):
        return img_url(i["Id"], "Backdrop", w, i["BackdropImageTags"][0])
    if tags.get("Thumb"):
        return img_url(i["Id"], "Thumb", w, tags["Thumb"])
    return img_url(i["Id"], "Primary", w, tags.get("Primary"))


def logo_of(i, w=640):
    tag = (i.get("ImageTags") or {}).get("Logo")
    return img_url(i["Id"], "Logo", w, tag) if tag else None


def poster_of_item(i, w=360):
    return img_url(i["Id"], "Primary", w, (i.get("ImageTags") or {}).get("Primary"))


def fmt_runtime(ticks):
    mins = round((ticks or 0) / 600_000_000)
    return f"{mins // 60}h {mins % 60}m" if mins >= 60 else (f"{mins}m" if mins else "")


def time_left(i):
    ud, total = i.get("UserData") or {}, i.get("RunTimeTicks") or 0
    pos = ud.get("PlaybackPositionTicks") or 0
    return f"{fmt_runtime(total - pos)} left" if pos and total - pos > 600_000_000 else ("Almost done" if pos else "")


def progress_pct(i):
    ud = i.get("UserData") or {}
    if ud.get("PlayedPercentage"):
        return ud["PlayedPercentage"]
    total = i.get("RunTimeTicks") or 0
    return 100 * (ud.get("PlaybackPositionTicks") or 0) / total if total else 0


def ep_code(e):
    if e.get("Type") != "Episode":
        return ""
    s, n = e.get("ParentIndexNumber"), e.get("IndexNumber")
    if s == 0:
        return f"Special {n or ''}".strip()
    return f"S{s} E{n}" if s is not None and n is not None else (f"E{n}" if n is not None else "")


def year_span(i):
    y = i.get("ProductionYear")
    if not y or i.get("Type") != "Series":
        return str(y or "")
    end = (i.get("EndDate") or "")[:4]
    return f"{y}–" if i.get("Status") == "Continuing" else (f"{y}–{end}" if end and end != str(y) else str(y))


def meta_bits(i, seasons=None):
    bits = [year_span(i), i.get("OfficialRating") or ""]
    if i.get("CommunityRating"):
        bits.append(f"★ {i['CommunityRating']:.1f}")
    if i.get("Type") == "Movie" and i.get("RunTimeTicks"):
        bits.append(fmt_runtime(i["RunTimeTicks"]))
    if seasons:
        bits.append(f"{seasons} season{'s' if seasons != 1 else ''}")
    return [b for b in bits if b]


def nice_genres(i, n=3):
    return [g for g in (i.get("Genres") or []) if g not in ("Animation", "Anime")][:n]


# --- library queries (all per person, see PROFILES) ---

def library(uid):
    return jellyfin(f"/Items?userId={uid}&Recursive=true&IncludeItemTypes=Series,Movie&Fields=Overview,Genres,ProviderIds,Studios,DateCreated,ChildCount"
                    "&SortBy=DateLastContentAdded,DateCreated&SortOrder=Descending,Descending")["Items"]


def resume_items(uid, limit=20):
    items = jellyfin(f"/UserItems/Resume?userId={uid}&Limit={limit}&MediaTypes=Video&Fields=Overview")["Items"]
    return [i for i in items if i.get("Type") in ("Episode", "Movie")]


def nextup_items(uid, series_id=None):
    q = f"/Shows/NextUp?userId={uid}&Limit=40&Fields=Overview&EnableResumable=false&EnableRewatching=false"
    return jellyfin(q + (f"&SeriesId={series_id}" if series_id else ""))["Items"]


def latest_episodes(uid):
    return jellyfin(f"/Items/Latest?userId={uid}&IncludeItemTypes=Episode&Limit=30&GroupItems=false&Fields=DateCreated,Overview")


def series_episodes(uid, series_id):
    items = jellyfin(f"/Shows/{series_id}/Episodes?userId={uid}&Fields=Overview,Path,DateCreated,PremiereDate")["Items"]
    return [e for e in items if e.get("LocationType") != "Virtual"]


def play_target(uid, series_id, episodes=None):
    """Which episode a show's big Play button starts: the one in progress, else next up, else the first unwatched."""
    eps = episodes if episodes is not None else series_episodes(uid, series_id)
    if not eps:
        return None, ""
    started = [e for e in eps if (e.get("UserData") or {}).get("PlaybackPositionTicks")]
    if started:
        e = max(started, key=lambda e: (e.get("UserData") or {}).get("LastPlayedDate") or "")
        return e, "Resume"
    played = [k for k, e in enumerate(eps) if (e.get("UserData") or {}).get("Played")]
    if played:
        after = [e for e in eps[played[-1] + 1:] if not (e.get("UserData") or {}).get("Played")]
        if after:
            return after[0], "Play"
        return eps[0], "Watch again"
    regular = [e for e in eps if e.get("ParentIndexNumber") != 0]
    return (regular or eps)[0], "Play"


def _downloads_by_key():
    try:
        return {c["id"]: c for c in hub_cards(with_files=False)}
    except Exception:
        return {}


def downloads_by_key():
    """Sonarr/Radarr cards keyed 's<tvdb>' / 'm<tmdb>' (download progress, searching, next airing); refreshed every 15 s."""
    return swr("dlcards", 5, _downloads_by_key, 900)


def trending_cached():
    return swr("trending", 900, trending, 86400)


def provider_key(i):
    ids = i.get("ProviderIds") or {}
    if i.get("Type") == "Series" and ids.get("Tvdb"):
        return f"s{ids['Tvdb']}"
    if i.get("Type") == "Movie" and ids.get("Tmdb"):
        return f"m{ids['Tmdb']}"
    return None


def download_progress(card):
    """(percent, eta text, episode count) of a card's active downloads, or None."""
    if not card or not card["downloads"]:
        return None
    size = sum(d["size"] for d in card["downloads"]) or 1
    return (100 * (size - sum(d["left"] for d in card["downloads"])) / size, human_eta(max(d["eta"] for d in card["downloads"])),
            sum(d["eps"] for d in card["downloads"]))


def upcoming_episodes(tvdb, seasons):
    """Monitored episodes Sonarr doesn't have yet, for the given seasons: downloading / airing soon / searching."""
    try:
        s = next(x for x in sonarr("GET", "/series") if str(x["tvdbId"]) == str(tvdb))
        eps = sonarr("GET", f"/episode?seriesId={s['id']}")
        queue = sonarr("GET", f"/queue?pageSize=1000&seriesIds={s['id']}&includeEpisode=true")["records"]
    except Exception:
        return {}
    q = {}
    for r in queue:
        if r.get("episodeId"):
            size = r.get("size") or 0
            q[r["episodeId"]] = (100 * (size - (r.get("sizeleft") or 0)) / size if size else 0, human_eta(parse_timeleft(r.get("timeleft"))))
    out = {}
    now = time.time()
    for e in eps:
        if e.get("hasFile") or not e.get("monitored") or e["seasonNumber"] not in seasons:
            continue
        air = e.get("airDateUtc")
        ts = calendar.timegm(time.strptime(air[:19], "%Y-%m-%dT%H:%M:%S")) if air else None
        if e["id"] in q:
            pct, eta = q[e["id"]]
            status, kind = f"Downloading · {pct:.0f}%" + (f" · {eta}" if eta else ""), "dl"
        elif ts and ts > now:
            lt = time.localtime(ts)
            status, kind = "Airs " + time.strftime("%a, %b %-d · %-I:%M %p", lt), "air"
        elif ts:
            status, kind = "Aired · waiting for a release", "wait"
        else:
            status, kind = "Not announced yet", "tba"
        out.setdefault(e["seasonNumber"], []).append({"n": e["episodeNumber"], "title": e.get("title") or "TBA",
                                                     "status": status, "kind": kind, "overview": e.get("overview") or ""})
    return out


# --- playback ---

def lang_name(code):
    return LANGS.get((code or "und").lower(), (code or "Unknown").upper())


def track_detail(s):
    """'Full Subtitles / English / ASS / FLE' -> 'Full Subtitles'; drops parts that only repeat the language/codec."""
    title = (s.get("Title") or "").split(" / ")[0].strip()
    lang = lang_name(s.get("Language"))
    if not title or title.lower() in (lang.lower(), "audio media handler", "subtitle", "sound handler"):
        return ""
    return title


def sub_kind(s):
    t = (s.get("Title") or "").lower()
    if "sign" in t or "song" in t or "forced" in t:
        return "signs"
    if (s.get("Language") or "").lower() == "enm" or "honorific" in t:
        return "honorifics"
    if "sdh" in t or "cc" == t:
        return "sdh"
    return "full"


def tracks(item):
    streams = item.get("MediaStreams") or []
    audios = [{"i": s["Index"], "lang": (s.get("Language") or "und").lower(), "label": lang_name(s.get("Language")),
               "detail": track_detail(s), "ch": s.get("Channels"), "codec": (s.get("Codec") or "").lower()}
              for s in streams if s["Type"] == "Audio"]
    subs = [{"i": s["Index"], "lang": "eng" if (s.get("Language") or "").lower() == "enm" else (s.get("Language") or "und").lower(),
             "label": lang_name(s.get("Language")), "detail": track_detail(s), "kind": sub_kind(s), "def": bool(s.get("IsDefault")),
             "text": bool(s.get("IsTextSubtitleStream"))}
            for s in streams if s["Type"] == "Subtitle"]
    subs.sort(key=lambda s: (s["lang"] != "eng", s["label"]))
    return audios, subs


def default_tracks(audios, subs, anime):
    """Japanese audio + full English subtitles for anime; dubs get Signs & Songs only; non-anime: no subtitles."""
    jp = [a for a in audios if a["lang"].startswith("ja")]
    audio = (jp if anime else []) or audios
    a = audio[0] if audio else None
    eng = [s for s in subs if s["lang"] == "eng"]
    if not a:
        return -1, -1
    if a["lang"].startswith("en") or not anime:
        signs = [s for s in eng if s["kind"] == "signs"]
        return a["i"], (signs[0]["i"] if signs and anime else -1)
    full = [s for s in eng if s["kind"] == "full"] or [s for s in eng if s["kind"] in ("honorifics", "sdh")] or eng or subs
    full.sort(key=lambda s: not s["def"])
    return a["i"], (full[0]["i"] if full else -1)


def skip_marks(item):
    """Opening (start, end) and credits start, from chapter names (BD/WEB releases) or Jellyfin media segments."""
    ch = [(c.get("Name") or "", c["StartPositionTicks"] / 1e7) for c in item.get("Chapters") or []]
    run = (item.get("RunTimeTicks") or 0) / 1e7
    intro = credits = recap = None
    for k, (name, start) in enumerate(ch):
        end = ch[k + 1][1] if k + 1 < len(ch) else run
        if intro is None and OPENING.match(name.strip()) and 20 < end - start < 200:
            intro = [start, end]
        if credits is None and ENDING.match(name.strip()) and start > run * 0.6:
            credits = start
    if intro is None:   # a lone "Intro" chapter of normal OP length
        for k, (name, start) in enumerate(ch):
            end = ch[k + 1][1] if k + 1 < len(ch) else run
            if name.strip().lower() == "intro" and 60 < end - start < 125 and not any(OPENING.match(n) for n, _ in ch):
                intro = [start, end]
    try:
        for seg in jellyfin(f"/MediaSegments/{item['Id']}")["Items"]:
            if seg["Type"] == "Intro" and intro is None:
                intro = [seg["StartTicks"] / 1e7, seg["EndTicks"] / 1e7]
            if seg["Type"] == "Outro" and credits is None:
                credits = seg["StartTicks"] / 1e7
            if seg["Type"] == "Recap" and recap is None:
                recap = [seg["StartTicks"] / 1e7, seg["EndTicks"] / 1e7]
    except Exception:
        pass
    return intro, credits, recap


def stream_token(item_id, exp=None):
    exp = exp or int(time.time()) + 12 * 3600
    return f"{exp}.{hmac.new(CONF['secret'].encode(), f'{item_id}|{exp}|stream'.encode(), hashlib.sha256).hexdigest()[:32]}"


def stream_token_ok(item_id, tok):
    try:
        exp = int(tok.split(".")[0])
        return exp > time.time() and hmac.compare_digest(stream_token(item_id, exp), tok)
    except (ValueError, AttributeError):
        return False


def jellyfin_master_query(item_id, q):
    """Our master.m3u8?m=&a=&s=&q=&d=&psid=&vc=&ac= -> Jellyfin HLS parameters.
    m=remux: the original video/audio packets repackaged (no re-encoding; audio converted only if the browser can't play it).
    m=transcode: H.264 + AAC on the GPU at the chosen bitrate; s= burns that subtitle track in (only for picture subtitles)."""
    dev = re.sub(r"[^A-Za-z0-9-]", "", q.get("d") or "")[:48] or "aniserver-web"
    psid = re.sub(r"[^A-Za-z0-9]", "", q.get("psid") or "")[:40] or uuid.uuid4().hex
    p = {"MediaSourceId": item_id, "DeviceId": dev, "PlaySessionId": psid, "TranscodingMaxAudioChannels": 2,
         "MinSegments": 1, "BreakOnNonKeyFrames": "True",
         "h264-profile": "high,main,baseline,constrainedbaseline", "h264-level": "52", "h264-videobitdepth": "8", "h264-rangetype": "SDR"}
    if q.get("m") == "remux":
        vcs = [c for c in ("h264", "hevc", "av1", "vp9") if c in (q.get("vc") or "h264").split(",")] or ["h264"]
        acs = ["aac"] + [c for c in ("flac", "opus", "eac3", "ac3", "mp3") if c in (q.get("ac") or "").split(",")]
        p.update(VideoCodec=",".join(vcs), AudioCodec=",".join(acs), VideoBitrate=200_000_000, AudioBitrate=320000, MaxWidth=3840,
                 SegmentContainer="mp4", AllowVideoStreamCopy="true", AllowAudioStreamCopy="true",
                 **{"hevc-profile": "main,main10", "hevc-videobitdepth": "10", "hevc-level": "186", "hevc-rangetype": "SDR"})
    else:
        label, bitrate, width = QUALITIES.get(q.get("q"), QUALITIES["1080"])
        p.update(VideoCodec="h264", AudioCodec="aac", VideoBitrate=bitrate, AudioBitrate=192000, MaxWidth=width, SegmentContainer="ts")
    if (q.get("a") or "").lstrip("-").isdigit():
        p["AudioStreamIndex"] = int(q["a"])
    if (q.get("s") or "").isdigit() and q.get("m") != "remux":
        p.update(SubtitleStreamIndex=int(q["s"]), SubtitleMethod="Encode")   # picture subtitles (PGS) burned in on the GPU
    return urllib.parse.urlencode(p)


def abr_master(item_id, q, tok):
    """Adaptive stream: a master playlist with several GPU-encoded qualities (3-second segments, aligned across levels), so the
    player can step down/up with the connection instead of buffering - like Netflix/Crunchyroll."""
    dev = re.sub(r"[^A-Za-z0-9-]", "", q.get("d") or "")[:48] or "aniserver-web"
    psid = re.sub(r"[^A-Za-z0-9]", "", q.get("psid") or "")[:40] or uuid.uuid4().hex
    hevc = q.get("hevc") == "1"
    lines = ["#EXTM3U"]
    for k, (width, bps) in enumerate(LADDER_HEVC if hevc else LADDER):
        p = {"MediaSourceId": item_id, "DeviceId": dev, "PlaySessionId": f"{psid}L{k}", "VideoCodec": "hevc" if hevc else "h264",
             "AudioCodec": "aac", "VideoBitrate": bps, "AudioBitrate": 160000, "MaxWidth": width, "TranscodingMaxAudioChannels": 2,
             "SegmentContainer": "mp4" if hevc else "ts", "MinSegments": 1, "BreakOnNonKeyFrames": "True",
             "h264-profile": "high,main,baseline,constrainedbaseline", "h264-level": "51", "h264-videobitdepth": "8", "h264-rangetype": "SDR",
             "hevc-profile": "main,main10", "hevc-level": "150", "hevc-rangetype": "SDR"}
        if (q.get("a") or "").lstrip("-").isdigit():
            p["AudioStreamIndex"] = int(q["a"])
        height = round(width * 9 / 16 / 2) * 2
        lines.append(f'#EXT-X-STREAM-INF:BANDWIDTH={bps + 160000},AVERAGE-BANDWIDTH={int(bps * 0.85) + 160000},'
                     f'RESOLUTION={width}x{height},CODECS="{"hvc1.1.4.L150.B0" if hevc else "avc1.640033"},mp4a.40.2"')
        lines.append("main.m3u8?" + urllib.parse.urlencode(p) + "&_t=" + urllib.parse.quote(tok))
    return "\n".join(lines) + "\n"


def bitrate_profile(item_id, path):
    """{'avg': bps, 'peak': highest 30-second-average bps} for a file (packet sizes via ffprobe), cached on disk.
    The player streams the original over the internet only when the connection beats the peak."""
    if not _RATES:
        try:
            _RATES.update(json.loads(RATES_CACHE.read_text()))
        except (OSError, ValueError):
            _RATES["_"] = {}
    if item_id in _RATES:
        return _RATES[item_id]
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "packet=pts_time,size", "-of", "csv=p=0", path],
                         capture_output=True, text=True, timeout=300).stdout
    per_sec = {}
    for line in out.splitlines():
        t, _, size = line.partition(",")
        try:
            per_sec[int(float(t))] = per_sec.get(int(float(t)), 0) + int(size.strip(","))
        except ValueError:
            continue
    if not per_sec:
        return None
    n = max(per_sec) + 1
    secs = [per_sec.get(i, 0) * 8 for i in range(n)]
    window = min(30, n)
    run = sum(secs[:window])
    peak = run
    for i in range(window, n):
        run += secs[i] - secs[i - window]
        peak = max(peak, run)
    prof = {"avg": int(sum(secs) / n), "peak": int(peak / window)}
    _RATES[item_id] = prof
    try:
        RATES_CACHE.parent.mkdir(parents=True, exist_ok=True)
        RATES_CACHE.write_text(json.dumps(_RATES))
    except OSError:
        pass
    return prof


def sfnt_names(data):
    """Family / full / PostScript names (lowercase) in a TrueType/OpenType font or collection, from its 'name' table."""
    names = set()

    def one(off):
        num = struct.unpack(">H", data[off + 4:off + 6])[0]
        for k in range(num):
            tag, _, toff, _ = struct.unpack(">4sIII", data[off + 12 + 16 * k:off + 28 + 16 * k])
            if tag != b"name":
                continue
            _, count, soff = struct.unpack(">HHH", data[toff:toff + 6])
            for r in range(count):
                plat, _, _, nid, ln, so = struct.unpack(">HHHHHH", data[toff + 6 + 12 * r:toff + 18 + 12 * r])
                if nid not in (1, 4, 6, 16):
                    continue
                raw = data[toff + soff + so:toff + soff + so + ln]
                try:
                    s = raw.decode("utf-16-be") if plat in (0, 3) else raw.decode("mac_roman")
                except UnicodeDecodeError:
                    continue
                if s.strip():
                    names.add(s.strip().lower())
    if data[:4] == b"ttcf":
        for k in range(struct.unpack(">I", data[8:12])[0]):
            one(struct.unpack(">I", data[12 + 4 * k:16 + 4 * k])[0])
    else:
        one(0)
    return names


def item_fonts(item_id, attachments):
    """{font name: attachment index} for the fonts embedded in a release (fansub styling), read once and cached on disk."""
    if not _FONT_NAMES:
        try:
            _FONT_NAMES.update(json.loads(FONT_CACHE.read_text()))
        except (OSError, ValueError):
            _FONT_NAMES["_"] = {}
    if item_id in _FONT_NAMES:
        return _FONT_NAMES[item_id]
    fonts = [a for a in attachments if re.search(r"\.(ttf|otf|ttc|otc)$", a.get("FileName") or "", re.I)
             or "font" in (a.get("MimeType") or "") or "opentype" in (a.get("MimeType") or "")]

    def fetch(a):
        req = urllib.request.Request(f"{JELLYFIN}/Videos/{item_id}/{item_id}/Attachments/{a['Index']}",
                                     headers={"Authorization": f'MediaBrowser Token="{CONF["jellyfin_key"]}"'})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return a["Index"], sfnt_names(r.read())
        except Exception:
            return a["Index"], set()
    out = {}
    for idx, names in POOL.map(fetch, fonts):
        for n in names:
            out.setdefault(n, idx)
    _FONT_NAMES[item_id] = out
    try:
        FONT_CACHE.parent.mkdir(parents=True, exist_ok=True)
        FONT_CACHE.write_text(json.dumps(_FONT_NAMES))
    except OSError:
        pass
    return out


def tokenize_playlist(text, tok):
    """Child playlists/segments carry the stream token, so playback also works without cookies (AirPlay, Safari)."""
    def add(uri):
        if "_t=" in uri:
            return uri
        return uri + ("&" if "?" in uri else "?") + "_t=" + urllib.parse.quote(tok)
    out = []
    for line in text.splitlines():
        if line and not line.startswith("#"):
            line = add(line.strip())
        elif 'URI="' in line:
            line = re.sub(r'URI="([^"]+)"', lambda m: f'URI="{add(m.group(1))}"', line)
        out.append(line)
    return "\n".join(out) + "\n"


def save_progress(user, item_id, pos, played=False):
    uid = jf_uid(user)
    body = {"PlaybackPositionTicks": 0 if played else max(0, int(pos * 10_000_000)), "Played": bool(played),
            "LastPlayedDate": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())}
    jellyfin(f"/UserItems/{item_id}/UserData?userId={uid}", "POST", body)


def set_played(user, item_id, on):
    jellyfin(f"/UserPlayedItems/{item_id}?userId={jf_uid(user)}", "POST" if on else "DELETE")


def stop_transcode(dev, psid):
    for sid in [psid] + [f"{psid}L{k}" for k in range(len(LADDER))]:
        try:
            jellyfin("/Videos/ActiveEncodings?" + urllib.parse.urlencode({"deviceId": dev, "playSessionId": sid}), "DELETE")
        except Exception:
            pass


def mpv_resume_at(path, seconds):
    """Make the TV (mpv) start at `seconds`: mpv resumes from its watch-later file, named after the MD5 of the path."""
    d = Path.home() / ".local/state/mpv/watch_later"
    d.mkdir(parents=True, exist_ok=True)
    f = d / hashlib.md5(path.encode()).hexdigest().upper()
    if seconds and seconds > 5:
        f.write_text(f"start={seconds:.3f}\n")
    elif f.exists():
        f.unlink()


def tv_play_item(user, item_id, pos=None):
    """Start a library item on the TV at `pos` seconds (default: where this person left off in Jellyfin)."""
    it = jf_item(jf_uid(user), item_id)
    real = media_path(it.get("Path"))
    if pos is None:
        ud = it.get("UserData") or {}
        pos = 0 if ud.get("Played") else (ud.get("PlaybackPositionTicks") or 0) / 1e7
    mpv_resume_at(real, pos)
    kodi_play(real)
    name = f"{it.get('SeriesName')} {ep_code(it)}" if it.get("Type") == "Episode" else it.get("Name")
    return name, pos


TV_PROFILE = CONF.get("tv_profile", OWNER)      # whose Jellyfin progress the living-room TV updates


def mpv_progress(path, pos, dur):
    """mpv on the TV stopped at `pos`: save it to Jellyfin like the website player does (played when near the end)."""
    real = media_path(path)
    paths, _ = jf_maps()
    item = paths.get(real)
    if not item:
        _JF_CACHE["t"] = 0
        item = jf_maps()[0].get(real)
    if not item:
        return False
    played = dur > 0 and pos / dur >= 0.9
    if pos < 30 and not played:
        return False
    save_progress(TV_PROFILE, item, pos, played)
    return True


def playinfo(user, item_id):
    """Everything the player needs for one episode/movie (also used to switch to the next episode in place)."""
    uid = jf_uid(user)
    it = jf_item(uid, item_id)
    if it.get("Type") not in ("Episode", "Movie"):
        raise ValueError("Not a playable item")
    series = jf_item(uid, it["SeriesId"]) if it.get("SeriesId") else it
    eps = series_episodes(uid, it["SeriesId"]) if it.get("SeriesId") else []
    k = next((n for n, e in enumerate(eps) if e["Id"] == it["Id"]), None)
    nxt = eps[k + 1] if k is not None and k + 1 < len(eps) else None
    prev = eps[k - 1] if k else None
    audios, subs = tracks(it)
    anime = is_anime(series)
    a, s = default_tracks(audios, subs, anime)
    ud = it.get("UserData") or {}
    intro, credits, recap = skip_marks(it)
    trick = None
    for widths in (it.get("Trickplay") or {}).values():
        t = widths.get("320") or next(iter(widths.values()), None)
        if t:
            trick = {"w": t["Width"], "h": t["Height"], "tw": t["TileWidth"], "th": t["TileHeight"], "n": t["ThumbnailCount"],
                     "every": t["Interval"] / 1000}
    brief = lambda e: e and {"id": e["Id"], "code": ep_code(e), "name": e.get("Name") or "", "thumb": thumb_of(e, 480)}
    ms = (it.get("MediaSources") or [{}])[0]
    vs = next((s for s in it.get("MediaStreams") or [] if s["Type"] == "Video"), {})
    tok = stream_token(it["Id"])
    fonts = {name: f"/stream/{it['Id']}/att/{idx}?_t={tok}" for name, idx in item_fonts(it["Id"], ms.get("MediaAttachments") or []).items()}
    prof = _RATES.get(it["Id"]) or {"avg": ms.get("Bitrate") or 0, "peak": int((ms.get("Bitrate") or 0) * 2.2)}
    return {"id": it["Id"], "type": it["Type"], "series": series["Id"], "show": series.get("Name") if it.get("SeriesId") else it.get("Name"),
            "code": ep_code(it), "name": it.get("Name") if it.get("SeriesId") else "", "overview": it.get("Overview") or "",
            "duration": (it.get("RunTimeTicks") or 0) / 1e7,
            "pos": 0 if ud.get("Played") else (ud.get("PlaybackPositionTicks") or 0) / 1e7,
            "audios": audios, "subs": subs, "a": a, "s": s, "anime": anime,
            "chapters": [{"name": c.get("Name") or "", "t": c["StartPositionTicks"] / 1e7} for c in it.get("Chapters") or []],
            "intro": intro, "credits": credits, "recap": recap, "trick": trick, "next": brief(nxt), "prev": brief(prev),
            "still": thumb_of(it, 1280), "backdrop": backdrop_of(series, 1280), "logo": logo_of(series),
            "tok": tok, "fonts": fonts, "container": (ms.get("Container") or "").split(",")[0].lower(),
            "bitrate": ms.get("Bitrate") or 0, "peak": prof["peak"], "size": ms.get("Size") or 0,
            "video": {"codec": (vs.get("Codec") or "").lower(), "depth": vs.get("BitDepth") or 8, "profile": vs.get("Profile") or "",
                      "w": vs.get("Width") or 0, "h": vs.get("Height") or 0}}


# ---------- HTML ----------

ICON_TAGS = ('<link rel="icon" href="/static/icon.svg" type="image/svg+xml"><link rel="icon" href="/favicon.ico" sizes="48x48">'
             '<link rel="apple-touch-icon" href="/apple-touch-icon.png"><link rel="manifest" href="/manifest.webmanifest">'
             '<meta name="apple-mobile-web-app-title" content="aniserver">')

SEARCH_ICON = ('<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" '
               'stroke-linecap="round"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>')


def is_public_host(host):
    return host in (CONF.get("public_host"), CONF.get("watch_host"))


NAV = (("home", "/", "Home"), ("anime", "/anime", "Browse"), ("shows", "/shows", "My Shows"), ("search", "/manga", "Manga"),
       ("follows", "/following", "Followed manga"), ("downloads", "/downloads", "Downloads"))


def page(body, active="", host="", title="aniserver", full=False, user=None):
    h = html.escape(host)
    nav = "".join(f'<a href="{href}" class="{"on" if key == active else ""}">{label}</a>' for key, href, label in NAV)
    avatar = ""
    if user:
        name, color = PROFILES.get(user, (user.title(), "#888"))
        avatar = f'<a class="avatar" href="/account" title="{esc(name)} · account" style="background:{color}">{esc(name[0])}</a>'
    tools = f'<div class="tools"><a class="iconlink" href="/anime" aria-label="Search anime" title="Search anime">{SEARCH_ICON}</a>{avatar}</div>'
    chrome = ("", "") if not active else (
        f'<div class="top{" over" if full else ""}"><div class="in"><a class="brand" href="/"><i></i>{BRAND}</a><nav>{nav}</nav>{tools}</div></div>',
        ("""<footer><a href="/logout" style="margin-left:auto">Log out</a></footer>""" if is_public_host(host) else
         f"""<footer><span>Open:</span><a href="http://{h}:8096" target="_blank" rel="noopener">Jellyfin ↗</a>
<a href="http://{h}:25600" target="_blank" rel="noopener">Read manga (Komga) ↗</a>
<a href="http://{h}:4567" target="_blank" rel="noopener">Weekly chapters (Suwayomi) ↗</a>
<a href="/security">Remote access</a>
<a href="/logout" style="margin-left:auto">Log out</a></footer>"""))
    main = f'<main class="full">{body}</main>' if full else f'<div class="wrap">{body}</div>'
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><meta name="theme-color" content="#0e0e11">{ICON_TAGS}
<title>{html.escape(title)}</title><link rel="stylesheet" href="{asset('site.css')}"></head><body class="{'full' if full else ''}">{chrome[0]}{main}{chrome[1]}
<dialog id="details"></dialog><script src="{asset('site.js')}"></script>
<script type="speculationrules">{{"prerender": [{{"where": {{"or": [{{"href_matches": "/title/*"}}, {{"href_matches": "/"}}]}}, "eagerness": "moderate"}}]}}</script></body></html>"""


def fmt_size(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit not in ("B", "KB") else f"{n:.0f} {unit}"
        n /= 1024


def msg_html(qs):
    if "ok" in qs:
        return f'<div class="msg ok">{html.escape(qs["ok"][0])}</div>'
    if "err" in qs:
        return f'<div class="msg err">{html.escape(qs["err"][0])}</div>'
    return ""


FORMAT_LABEL = {"TV": "TV", "TV_SHORT": "Short", "ONA": "ONA", "OVA": "OVA", "SPECIAL": "Special", "MOVIE": "Movie"}


def season_label(c):
    if c["season"] is None:
        return ""
    if c["season"] == 0:
        return "Specials"
    return f"Season {c['season']}" + (" (likely)" if c["guessed"] else "")


def poster_html(c, n):
    m = c["m"]
    cover = (m.get("coverImage") or {}).get("large") or ""
    year = m.get("seasonYear") or (m.get("startDate") or {}).get("year") or ""
    sub = " · ".join(str(x) for x in (season_label(c) or FORMAT_LABEL.get(m["format"], m["format"]), year) if x)
    right = {"have": '<span class="chip ok bl">✓ In library</span>',
             "movie_done": '<span class="chip ok bl">✓ In library</span>',
             "movie_added": '<span class="chip warn bl">Added · downloading</span>',
             "season": '<span class="chip warn bl">Show in library</span>'}.get(c["state"], "")
    left = f'<span class="chip tl">{html.escape(FORMAT_LABEL.get(m["format"], m["format"]))}</span>'
    return f"""<button class="poster" data-t="t{n}" data-al="{m['id']}" type="button">
<div class="img"><img src="{html.escape(cover)}" alt="" loading="lazy">{left}{right}</div>
<div class="t">{html.escape(title_of(m))}</div><div class="m">{html.escape(sub)}</div></button>"""


def details_html(c, n, back, host):
    m = c["m"]
    title, romaji = title_of(m), m["title"]["romaji"]
    cover = (m.get("coverImage") or {}).get("extraLarge") or (m.get("coverImage") or {}).get("large") or ""
    color = (m.get("coverImage") or {}).get("color") or "#2a2a33"
    banner = (f'background-image:url({html.escape(m["bannerImage"])})' if m.get("bannerImage")
              else f"background:linear-gradient(135deg,{html.escape(color)},#0e0e11)")
    facts = []
    for x in (FORMAT_LABEL.get(m["format"], m["format"]), season_label(c),
              m.get("seasonYear") or (m.get("startDate") or {}).get("year"),
              f"{m['episodes']} episodes" if m.get("episodes") else None,
              (m.get("status") or "").replace("_", " ").title() or None,
              f"★ {m['averageScore']}%" if m.get("averageScore") else None,
              ", ".join(s["name"] for s in (m.get("studios") or {}).get("nodes", [])[:2]) or None):
        if x:
            facts.append(f"<span>{html.escape(str(x))}</span>")
    nxt = m.get("nextAiringEpisode")
    if nxt:
        days = max(0, round((nxt["airingAt"] - time.time()) / 86400))
        facts.append(f"<span>Ep {nxt['episode']} {'today' if days == 0 else f'in {days} day' + ('s' if days != 1 else '')}</span>")
    genres = "".join(f'<span class="g">{html.escape(g)}</span>' for g in (m.get("genres") or [])[:5])
    hidden = (f'<input type="hidden" name="tvdb" value="{c["tvdb"] or ""}"><input type="hidden" name="anilist" value="{m["id"]}">'
              f'<input type="hidden" name="season" value="{"" if c["season"] is None else c["season"]}">'
              f'<input type="hidden" name="back" value="{html.escape(back)}">')
    s, si = c["series"], c["season_info"]
    sonarr_link = ""
    mv = c.get("movie")
    radarr_link = ""
    if c["state"] == "movie_new":
        note = "" if c["tmdb"] else '<span class="note">Not in the anime ID list yet; it will be matched by title.</span>'
        actions = (f'<form method="post" action="/anime/addmovie" data-busy><input type="hidden" name="tmdb" value="{c["tmdb"] or ""}">'
                   f'<input type="hidden" name="anilist" value="{m["id"]}"><input type="hidden" name="back" value="{html.escape(back)}">'
                   f'<button>Add movie</button></form>{note}')
    elif c["state"] == "movie_done":
        actions = f'<span class="chip ok">In library</span><span class="muted">Downloaded · watch it in Kodi → Movies</span>{radarr_link}'
    elif c["state"] == "movie_added":
        actions = f'<span class="chip warn">Added</span><span class="muted">Searching / downloading</span>{radarr_link}'
    elif c["state"] == "have":
        stats = (si or {}).get("statistics") or (s.get("statistics") or {})
        got = f'{stats.get("episodeFileCount", 0)}/{stats.get("totalEpisodeCount") or stats.get("episodeCount", 0)} episodes downloaded'
        actions = f'<span class="chip ok">In library</span><span class="muted">{got}</span>{sonarr_link}'
    elif c["state"] == "season":
        actions = (f'<form method="post" action="/anime/add" data-busy>{hidden}'
                   f'<button name="mode" value="season">Add {html.escape(season_label(c).lower() or "this season")}</button></form>'
                   f'{sonarr_link}<span class="note">{html.escape(s["title"])} is in Sonarr, but not this season yet.</span>')
    else:
        buttons = []
        if c["season"] is not None:
            buttons.append(f'<button name="mode" value="season">Add {html.escape(season_label(c).lower())}</button>')
            buttons.append('<button class="ghost" name="mode" value="all">Add all seasons</button>')
        else:
            buttons.append('<button name="mode" value="all">Add all seasons</button>')
        buttons.append('<button class="ghost" name="mode" value="future">Only new episodes</button>')
        note = "" if c["tvdb"] else '<span class="note">Not in the anime ID list yet; it will be matched by title.</span>'
        actions = f'<form method="post" action="/anime/add" data-busy>{hidden}{"".join(buttons)}</form>{note}'
    return f"""<template id="t{n}"><div class="banner" style="{banner}"><button class="close" type="button" aria-label="Close">✕</button></div>
<div class="dbody"><img class="cov" src="{html.escape(cover)}" alt="">
<div class="dinfo"><h3>{html.escape(title)}</h3>{f'<div class="ro">{html.escape(romaji)}</div>' if romaji != title else ''}
<div class="facts">{"".join(facts)}</div><div class="facts">{genres}</div>
<p class="desc">{html.escape(clean_description(m.get("description")))}</p>
<div class="actions">{actions}</div></div></div></template>"""


# ---------- streaming pages: shared look (home, title pages) + the player ----------

BRAND = "aniserver"
esc = html.escape

ICON_PATHS = {
    "play": "M8 5.14v13.72a1 1 0 0 0 1.5.86l11.04-6.86a1 1 0 0 0 0-1.72L9.5 4.28A1 1 0 0 0 8 5.14z",
    "pause": "M6 19h4V5H6v14zm8-14v14h4V5h-4z",
    "info": "M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm1 15h-2v-6h2v6zm0-8h-2V7h2v2z",
    "tv": "M21 3H3c-1.1 0-2 .9-2 2v12c0 1.1.9 2 2 2h5v2h8v-2h5c1.1 0 1.99-.9 1.99-2L23 5c0-1.1-.9-2-2-2zm0 14H3V5h18v12z",
    "check": "M9 16.17 4.83 12l-1.42 1.41L9 19 21 7l-1.41-1.41z",
    "plus": "M19 13h-6v6h-2v-6H5v-2h6V5h2v6h6v2z",
    "back": "M20 11H7.83l5.59-5.59L12 4l-8 8 8 8 1.41-1.41L7.83 13H20v-2z",
    "next": "M6 18l8.5-6L6 6v12zM16 6v12h2V6h-2z",
    "vol": "M3 9v6h4l5 5V4L7 9H3zm13.5 3c0-1.77-1.02-3.29-2.5-4.03v8.05c1.48-.73 2.5-2.25 2.5-4.02zM14 3.23v2.06c2.89.86 5 3.54 5 6.71s-2.11 5.85-5 6.71v2.06c4.01-.91 7-4.49 7-8.77s-2.99-7.86-7-8.77z",
    "mute": "M16.5 12c0-1.77-1.02-3.29-2.5-4.03v2.21l2.45 2.45c.03-.2.05-.41.05-.63zm2.5 0c0 .94-.2 1.82-.54 2.64l1.51 1.51C20.63 14.91 21 13.5 21 12c0-4.28-2.99-7.86-7-8.77v2.06c2.89.86 5 3.54 5 6.71zM4.27 3 3 4.27 7.73 9H3v6h4l5 5v-6.73l4.25 4.25c-.67.52-1.42.93-2.25 1.18v2.06c1.38-.31 2.63-.95 3.69-1.81L19.73 21 21 19.73l-9-9L4.27 3zM12 4 9.91 6.09 12 8.18V4z",
    "cc": "M20 4H4c-1.1 0-2 .9-2 2v12c0 1.1.9 2 2 2h16c1.1 0 2-.9 2-2V6c0-1.1-.9-2-2-2zM4 12h4v2H4v-2zm10 6H4v-2h10v2zm6 0h-4v-2h4v2zm0-4H10v-2h10v2z",
    "gear": "M19.14 12.94c.04-.3.06-.61.06-.94 0-.32-.02-.64-.07-.94l2.03-1.58a.49.49 0 0 0 .12-.61l-1.92-3.32a.49.49 0 0 0-.59-.22l-2.39.96c-.5-.38-1.03-.7-1.62-.94l-.36-2.54a.48.48 0 0 0-.48-.41h-3.84c-.24 0-.43.17-.47.41l-.36 2.54c-.59.24-1.13.57-1.62.94l-2.39-.96a.49.49 0 0 0-.59.22L2.74 8.87c-.12.21-.08.47.12.61l2.03 1.58c-.05.3-.09.63-.09.94s.02.64.07.94l-2.03 1.58a.49.49 0 0 0-.12.61l1.92 3.32c.12.22.37.29.59.22l2.39-.96c.5.38 1.03.7 1.62.94l.36 2.54c.05.24.24.41.48.41h3.84c.24 0 .44-.17.47-.41l.36-2.54c.59-.24 1.13-.56 1.62-.94l2.39.96c.22.08.47 0 .59-.22l1.92-3.32c.12-.22.07-.47-.12-.61l-2.01-1.58zM12 15.6c-1.98 0-3.6-1.62-3.6-3.6s1.62-3.6 3.6-3.6 3.6 1.62 3.6 3.6-1.62 3.6-3.6 3.6z",
    "pip": "M19 7h-8v6h8V7zm2-4H3c-1.1 0-2 .9-2 2v14c0 1.1.9 1.98 2 1.98h18c1.1 0 2-.88 2-1.98V5c0-1.1-.9-2-2-2zm0 16.01H3V4.98h18v14.03z",
    "fs": "M7 14H5v5h5v-2H7v-3zm-2-4h2V7h3V5H5v5zm12 7h-3v2h5v-5h-2v3zM14 5v2h3v3h2V5h-5z",
    "fsx": "M5 16h3v3h2v-5H5v2zm3-8H5v2h5V5H8v3zm6 11h2v-3h3v-2h-5v5zm2-11V5h-2v5h5V8h-3z",
    "left": "M15.41 7.41 14 6l-6 6 6 6 1.41-1.41L10.83 12z",
    "right": "M10 6 8.59 7.41 13.17 12l-4.58 4.59L10 18l6-6z",
    "trash": "M6 19c0 1.1.9 2 2 2h8c1.1 0 2-.9 2-2V7H6v12zM19 4h-3.5l-1-1h-5l-1 1H5v2h14V4z",
}
REPLAY = "M12 5V1L7 6l5 5V7c3.31 0 6 2.69 6 6s-2.69 6-6 6-6-2.69-6-6H4c0 4.42 3.58 8 8 8s8-3.58 8-8-3.58-8-8-8z"


def icon(name, cls="ic"):
    return f'<svg class="{cls}" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="{ICON_PATHS[name]}"/></svg>'


def jump_icon(forward):
    flip = ' transform="matrix(-1 0 0 1 24 0)"' if forward else ""
    return (f'<svg class="ic" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path{flip} d="{REPLAY}"/>'
            '<text x="12" y="16.6" text-anchor="middle" font-size="7.2" font-weight="800" font-family="system-ui,sans-serif">10</text></svg>')




ASSET_URLS, ASSET_FILES = {}, {}


ASSET_DIR = Path(__file__).resolve().parent / "assets"          # the site's own CSS and JavaScript
ASSET_TYPES = {".css": "text/css", ".js": "text/javascript"}


def asset(name):
    """URL of a stylesheet/script in assets/, served as a long-cached file whose name carries a hash of its content (so
    it is downloaded once, and again only after it changes) instead of being repeated inside every page."""
    if not ASSET_URLS:
        for f in sorted(ASSET_DIR.iterdir()):
            if f.suffix in ASSET_TYPES:
                data = f.read_bytes()
                url = f"/assets/{f.stem}.{hashlib.sha256(data).hexdigest()[:12]}{f.suffix}"
                ASSET_URLS[f.name] = url
                ASSET_FILES[url] = (data, gzip.compress(data, 9), ASSET_TYPES[f.suffix] + "; charset=utf-8")
    return ASSET_URLS[name]


def meta_html(i, seasons=None, extra=()):
    bits = []
    if i.get("OfficialRating"):
        bits.append(f'<span class="rating">{esc(i["OfficialRating"])}</span>')
    if year_span(i):
        bits.append(f"<span>{esc(year_span(i))}</span>")
    if i.get("CommunityRating"):
        bits.append(f'<span><span class="star">★</span> {i["CommunityRating"]:.1f}</span>')
    if seasons:
        bits.append(f"<span>{seasons} season{'s' if seasons != 1 else ''}</span>")
    if i.get("Type") == "Movie" and i.get("RunTimeTicks"):
        bits.append(f"<span>{fmt_runtime(i['RunTimeTicks'])}</span>")
    bits += [f"<span>{esc(g)}</span>" for g in nice_genres(i)]
    bits += [f"<span>{esc(x)}</span>" for x in extra if x]
    return '<div class="meta">' + '<span class="dot">•</span>'.join(bits) + "</div>"



PLAY_OVERLAY = f'<div class="pl"><span>{icon("play")}</span></div>'


def ecard(e, t1, t2, tag="", show_dur=True):
    pct = progress_pct(e)
    seen = (e.get("UserData") or {}).get("Played")
    return (f'<a class="ec" href="/watch/{e["Id"]}"><div class="th"><img class="f" src="{thumb_of(e)}" alt="" loading="lazy" '
            f'onload="this.classList.add(\'ok\')">{PLAY_OVERLAY}'
            + (f'<span class="chip accent tag">{esc(tag)}</span>' if tag else "")
            + (f'<span class="seen">{icon("check")}</span>' if seen else "")
            + (f'<span class="dur">{fmt_runtime(e.get("RunTimeTicks"))}</span>' if show_dur and not pct and e.get("RunTimeTicks") else "")
            + (f'<div class="pbar"><i style="width:{pct:.1f}%"></i></div>' if pct and not seen else "")
            + f'</div><div class="t1">{esc(t1)}</div><div class="t2">{esc(t2)}</div></a>')


def pcard(href, img, t1, t2="", badge="", dl_html=""):
    return (f'<a class="pc" href="{esc(href)}"><div class="po"><img class="f" src="{esc(img)}" alt="" loading="lazy" '
            f'onload="this.classList.add(\'ok\')">'
            + (f'<span class="badge">{esc(badge)}</span>' if badge else "") + dl_html
            + f'</div><div class="t1">{esc(t1)}</div>' + (f'<div class="t2">{esc(t2)}</div>' if t2 else "") + "</a>")


def row_html(title, cards, kind="wide", link=None):
    if not cards:
        return ""
    more = f'<a href="{esc(link[0])}">{esc(link[1])} ›</a>' if link else ""
    return (f'<section class="row"><h2>{esc(title)}{more}</h2>'
            f'<button class="nav-arrow l" aria-label="Scroll left">{icon("left")}</button>'
            f'<div class="rail {kind}">{"".join(cards)}</div>'
            f'<button class="nav-arrow r" aria-label="Scroll right">{icon("right")}</button></section>')


def delete_button(card, title, round_btn=True):
    """Opens the 'Delete this?' confirmation (assets/site.js)."""
    if not card:
        return ""
    what = "movie" if card["kind"] == "movie" else "show"
    attrs = (f'data-delete="{card["id"]}" data-name="{esc(title)}" data-what="{what}" '
             f'data-size="{card.get("size", 0) / 1e9:.1f} GB" data-count="{card.get("count", 0)}"')
    if round_btn:
        return f'<button class="round" {attrs} title="Delete this {what} from the server" aria-label="Delete">{icon("trash")}</button>'
    return f'<button class="ghost icon" {attrs}>{icon("trash")}Delete</button>'


def tv_button(item_id, label="Play on the TV"):
    return f'<button class="round" data-tv="{item_id}" title="{esc(label)}" aria-label="{esc(label)}">{icon("tv")}</button>'


# ---------- HTTP ----------

class Handler(BaseHTTPRequestHandler):
    server_version = "HTPCRequests"
    protocol_version = "HTTP/1.1"      # keep-alive: images, segments and API calls reuse connections
    timeout = 120

    def log_message(self, fmt, *args):
        pass

    def host(self):
        return (self.headers.get("Host") or "localhost").rsplit(":", 1)[0]

    def public(self):
        """True when the request came from the internet through the Caddy HTTPS proxy."""
        return self.client_address[0] in ("127.0.0.1", "::1") and bool(self.headers.get("X-Forwarded-For"))

    def client_ip(self):
        if self.public():
            return self.headers["X-Forwarded-For"].split(",")[-1].strip()
        return self.client_address[0]

    def at_home(self):
        ip = self.client_ip()
        return ip in ("127.0.0.1", "::1") or ip.startswith("10.0.0.") or ip == home_public_ip()

    def authed(self):
        """Everyone signs in with their own account (password + a code once per browser), at home and away."""
        self._refresh = None
        c = SimpleCookie(self.headers.get("Cookie") or "")
        user = remote_token_user(c[REMOTE_COOKIE].value) if REMOTE_COOKIE in c else None
        self.remote_user = user
        self.user = user or OWNER
        if not user:
            return False
        # sliding sign-in: every visit pushes the expiry a year out again, so each browser signs in only once
        exp = int(time.time()) + REMOTE_SESSION_DAYS * 86400
        self._refresh = f"{REMOTE_COOKIE}={remote_token(user, exp)}; Max-Age={REMOTE_SESSION_DAYS * 86400}; {cookie_flags(self.public())}"
        return True

    def trusted_user(self):
        c = SimpleCookie(self.headers.get("Cookie") or "")
        return remote_token_user(c[TRUST_COOKIE].value, "trusted") if TRUST_COOKIE in c else None

    def next_url(self, f):
        nxt = f.get("next") or "/"
        return nxt if nxt.startswith("/") and not nxt.startswith("//") and "\\" not in nxt else "/"

    def send(self, body, code=200, headers=None):
        data = body.encode()
        zipped = len(data) > 1400 and "gzip" in (self.headers.get("Accept-Encoding") or "")
        if zipped:
            data = gzip.compress(data, 5)
        self.send_response(code)
        if zipped:
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Vary", "Accept-Encoding")
        if getattr(self, "_refresh", None):
            self.send_header("Set-Cookie", self._refresh)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def redirect(self, to, cookie=None):
        self.send_response(303)
        if getattr(self, "_refresh", None) and cookie is None:
            self.send_header("Set-Cookie", self._refresh)
        self.send_header("Location", to)
        for c in ([cookie] if isinstance(cookie, str) else cookie or []):
            self.send_header("Set-Cookie", c)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def form(self):
        n = int(self.headers.get("Content-Length") or 0)
        return {k: v[0] for k, v in urllib.parse.parse_qs(self.rfile.read(n).decode()).items()}

    # --- GET ---
    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(url.query)
        if url.path == "/login":
            if self.authed():
                return self.redirect(self.next_url({k: v[0] for k, v in qs.items()}))
            return self.send(page(self.login_form(qs), title="aniserver · Log in"))
        if url.path == "/logout":
            self.send_response(303)
            self.send_header("Location", "/login")
            self.send_header("Set-Cookie", f"{REMOTE_COOKIE}=; Max-Age=0; {cookie_flags(self.public())}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if url.path.startswith("/static/"):
            return self.static(url.path)
        if url.path.startswith("/assets/"):
            return self.serve_asset(url.path)
        if url.path in ("/favicon.ico", "/apple-touch-icon.png", "/apple-touch-icon-precomposed.png", "/manifest.webmanifest"):
            return self.static("/static/" + {"/apple-touch-icon-precomposed.png": "apple-touch-icon.png"}.get(url.path, url.path[1:]))
        if url.path.startswith("/stream/"):                 # video: stream token (or session) checked inside
            return self.stream(url.path, url.query)
        authed = self.authed()
        signed = url.path == "/go" and signature_ok(qs)
        if not signed and not authed:
            return self.redirect("/login" + ("?" + urllib.parse.urlencode({"next": self.path}) if self.path not in ("/", "") else ""))
        if url.path == "/go":
            return self.go(qs, authed)
        if url.path.startswith("/img/"):
            return self.image(url.path, qs)
        if url.path == "/":
            return self.send(page(self.home_page(qs), "home", self.host(), "aniserver", full=True, user=self.user))
        if url.path == "/account":
            return self.send(page(self.account_page(qs), "account", self.host(), "aniserver · Account", user=self.user))
        if url.path == "/profiles":                       # old "Who's watching?" page: your login says who you are
            return self.redirect("/account")
        if url.path == "/api/downloads":
            try:
                return self.send_json(live_downloads())
            except Exception as e:
                return self.send_json({"error": str(e)}, 502)
        m = re.fullmatch(r"/(title|watch|play|api/playinfo)/([0-9a-f]{32})", url.path)
        if m:
            kind, iid = m.groups()
            try:
                if kind == "api/playinfo":
                    return self.send_json(playinfo(self.user, iid))
                if kind == "play":
                    target, _ = play_target(jf_uid(self.user), iid)
                    return self.redirect(f"/watch/{target['Id']}" if target else f"/title/{iid}")
                if kind == "watch":
                    return self.send(self.watch_page(iid, qs), headers={"Cache-Control": "no-store"})
                body = self.title_page(iid, qs)
                if isinstance(body, tuple):
                    return self.redirect(body[1])
                return self.send(page(body, "home", self.host(), getattr(self, "page_title", "aniserver"), full=True, user=self.user))
            except (urllib.error.HTTPError, ValueError) as e:
                if kind == "api/playinfo":
                    return self.send_json({"error": str(e)}, 404)
                return self.send(page(f'<div class="msg err">That title isn\'t on the server (any more). <a href="/">Go home</a></div>',
                                      "home", self.host(), "aniserver · Not found", user=self.user), 404)
        if url.path == "/shows":
            return self.send(page(self.shows_page(qs), "shows", self.host(), "aniserver · My Shows", user=self.user))
        if url.path == "/security":
            if self.public() or self.user != OWNER:
                return self.send(page("<p>Not found.</p>", "anime", self.host()), 404)
            return self.send(page(self.security_page(qs), "anime", self.host(), "aniserver · Remote access"))
        if url.path == "/manga":
            return self.send(page(self.search_page(qs), "search", self.host(), "aniserver · Manga", user=self.user))
        if url.path == "/anime":
            return self.send(page(self.anime_page(qs), "anime", self.host(), "aniserver · Anime", user=self.user))
        if url.path == "/downloads":
            return self.send(page(self.downloads_page(qs), "downloads", self.host(), "aniserver · Downloads", user=self.user))
        if url.path == "/following":
            return self.send(page(self.follows_page(qs), "follows", self.host(), "aniserver · Followed manga", user=self.user))
        self.send(page("<p>Not found.</p>", "anime", self.host()), 404)

    # --- POST ---
    def do_POST(self):
        if self.path.startswith("/api/tvplay"):        # notification button: signed link, no session needed
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if not signature_ok(qs):
                return self.send("Link expired or invalid.", 403)
            try:
                kodi_play(qs["p"][0])
                return self.send("Playing on the TV.")
            except Exception as e:
                return self.send(f"Couldn't play on the TV: {e}", 500)
        f = self.form()
        if self.path == "/api/mpv-progress":           # the TV's mpv (this machine only) reporting where it stopped
            if self.public() or self.client_address[0] not in ("127.0.0.1", "::1"):
                return self.send("Forbidden", 403)
            try:
                return self.send_json({"ok": True, "saved": mpv_progress(f.get("path", ""), float(f.get("pos") or 0), float(f.get("dur") or 0))})
            except Exception as e:
                return self.send_json({"ok": False, "msg": str(e)}, 500)
        if self.path == "/login":
            ip = self.client_ip()
            if too_many_fails(ip):
                record_fail(ip, f.get("username", ""))
                return self.redirect("/login?err=" + urllib.parse.quote("Too many failed attempts. Try again in 15 minutes."))
            user, users = f.get("username", "").strip().lower(), remote_users()
            rec = users.get(user)
            pw_ok = bool(rec) and check_password(f.get("password", ""), rec["pw"])
            remembered = pw_ok and self.trusted_user() == user
            if remembered:
                counter = rec.get("last", 0)                # trusted browser: no code needed
            else:
                counter = totp_ok(rec["totp"], f.get("code"), rec.get("last", 0)) if pw_ok else None
            if counter is None:
                record_fail(ip, user)
                return self.redirect("/login?" + urllib.parse.urlencode({"err": "Wrong username, password or code.", "next": self.next_url(f)}))
            rec["last"] = counter
            save_remote_users(users)
            note_login(user, ip, remembered, self.headers.get("User-Agent") or "")
            return self.redirect(self.next_url(f), session_cookies(user, self.public()))
        if not self.authed():
            if self.path.startswith("/api/"):
                return self.send_json({"ok": False, "msg": "Signed out. Reload the page."}, 401)
            return self.redirect("/login")
        if self.path.startswith("/api/"):
            return self.api_post(f)
        if self.path in ("/account/password", "/account/jellyfin"):
            return self.account_post(f)
        if self.path.startswith("/security/"):
            if self.public() or self.user != OWNER:
                return self.redirect("/anime")
            return self.security_post(f)
        back = f.get("back") or "/"
        sep = "&" if "?" in back else "?"
        try:
            if self.path == "/grab":
                grab(f["guid"])
                note = f"Downloading “{f.get('title', '')}”. It shows up in Komga/Komelia (Volumes) a couple of minutes after it finishes."
            elif self.path == "/follow":
                follow(f.get("series", ""), f.get("uploader", ""))
                note = f"Following “{f.get('series')}”. New volumes will download automatically."
            elif self.path == "/unfollow":
                unfollow(f["series"])
                note = f"Stopped following “{f['series']}”."
            elif self.path == "/tv/play":
                kodi_play(f["p"])
                note = f"Playing on the TV: {f.get('label') or os.path.basename(f['p'])}"
            elif self.path == "/anime/add":
                title, result = anime_add(int(f["tvdb"]) if f.get("tvdb") else None, int(f.get("anilist") or 0),
                                          int(f["season"]) if f.get("season") else None, f.get("mode", "all"))
                return self.redirect("/shows?" + urllib.parse.urlencode({"ok": f"{title}: {result}", "focus": title}) + "#top")
            elif self.path == "/anime/addmovie":
                title, result = movie_add(int(f["tmdb"]) if f.get("tmdb") else None, int(f.get("anilist") or 0))
                return self.redirect("/shows?" + urllib.parse.urlencode({"ok": f"{title}: {result}", "focus": title}) + "#top")
            else:
                return self.redirect("/")
            self.redirect(back + sep + "ok=" + urllib.parse.quote(note))
        except Exception as e:  # show backend failures on the page instead of a crash
            self.redirect(back + sep + "err=" + urllib.parse.quote(f"That didn't work: {e}"))

    # --- pages ---
    def go(self, qs, authed=False):
        """Open a downloaded episode/movie in Jellyfin (waits briefly if Jellyfin hasn't picked it up yet)."""
        try:
            real = media_path((qs.get("p") or [""])[0])
        except ValueError as e:
            return self.send(page(f'<div class="msg err">{html.escape(str(e))}</div>', "shows", self.host()), 404)
        paths, _ = jf_maps()
        item = paths.get(real)
        if not item:
            _JF_CACHE["t"] = 0
            paths, _ = jf_maps()
            item = paths.get(real)
        if not item:
            tries = int((qs.get("try") or ["0"])[0])
            if tries == 0:
                try:
                    jellyfin("/Library/Refresh", "POST")
                except Exception:
                    pass
            if tries < 24:
                again = urllib.parse.urlencode({**{k: v[0] for k, v in qs.items()}, "try": tries + 1})
                return self.send(page(f"""<meta http-equiv="refresh" content="5;url=/go?{html.escape(again)}">
<div class="login"><div class="brand"><i></i>aniserver</div><p><span class="spin"></span>Jellyfin is still adding this one, it'll open in a moment…</p></div>""", title="aniserver · Opening…"))
            return self.send(page('<div class="msg err">Jellyfin hasn\'t picked this file up yet. Try again in a minute.</div>', "shows", self.host()), 404)
        if authed:
            return self.redirect(f"/watch/{item}")
        return self.redirect("/login?" + urllib.parse.urlencode({"next": f"/watch/{item}"}))

    def image(self, path, qs):
        """Jellyfin artwork, proxied through this site so it loads on any network (home, Tailscale, internet)."""
        m = re.fullmatch(r"/img/([0-9a-f]{32})/(Primary|Backdrop|Thumb|Logo|Banner)", path)
        if not m:
            return self.send("Not found", 404)
        want = (qs.get("w") or ["480"])[0]
        w = next((x for x in IMG_WIDTHS if x >= int(want)), IMG_WIDTHS[-1]) if want.isdigit() else 480
        key = (m.group(1), m.group(2), w, (qs.get("t") or [""])[0])
        with _IMG_LOCK:                       # recently used artwork is kept in memory (shared by every device)
            hit = _IMG_CACHE.get(key)
            if hit:
                _IMG_CACHE.move_to_end(key)
        if hit:
            data, ctype = hit
        else:
            try:
                with urllib.request.urlopen(f"{JELLYFIN}/Items/{m.group(1)}/Images/{m.group(2)}?maxWidth={w}"
                                            f"&quality={85 if w >= 1280 else 80}&format=Webp", timeout=20) as r:
                    data, ctype = r.read(), r.headers.get("Content-Type", "image/webp")
            except urllib.error.HTTPError as e:
                return self.send("Not found", e.code)
            except Exception:
                return self.send("Unavailable", 502)
            with _IMG_LOCK:
                _IMG_CACHE[key] = (data, ctype)
                while sum(len(d) for d, _ in _IMG_CACHE.values()) > IMG_CACHE_BYTES:
                    _IMG_CACHE.popitem(last=False)
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "private, max-age=604800")
        self.end_headers()
        self.wfile.write(data)

    def watch_buttons(self, path, label):
        q = urllib.parse.urlencode({"p": path})
        return (f'<a class="btn icon" href="/go?{html.escape(q)}" title="Play it here">▶ Watch</a>'
                f'<form class="inline" method="post" action="/tv/play"><input type="hidden" name="p" value="{html.escape(path)}">'
                f'<input type="hidden" name="label" value="{html.escape(label)}"><input type="hidden" name="back" value="/shows">'
                f'<button class="ghost icon" title="Starts playing on the living-room TV (Kodi)">📺 Play on TV</button></form>')

    def shows_page(self, qs):
        try:
            cards = hub_cards()
        except Exception as e:
            return f'<div class="msg err">Couldn\'t load your shows: {html.escape(str(e))}</div>'
        try:
            nu = next_up(self.user)
        except Exception:
            nu = {}
        try:
            resumes = {it["path"]: it["pct"] for it in continue_watching(self.user)}
        except Exception:
            resumes = {}
        try:
            jf_ids = {provider_key(i): i["Id"] for i in library(jf_uid(self.user))}
        except Exception:
            jf_ids = {}
        focus = (qs.get("focus") or [""])[0]
        busy = any(c["downloads"] or c["searching"] for c in cards)
        out = []
        for c in cards:
            if c["downloads"]:
                size = sum(d["size"] for d in c["downloads"]) or 1
                pct = 100 * (size - sum(d["left"] for d in c["downloads"])) / size
                eta = human_eta(max(d["eta"] for d in c["downloads"]))
                n = sum(d["eps"] for d in c["downloads"])
                what = "movie" if c["kind"] == "movie" else (f"{n} episode{'s' if n != 1 else ''}")
                status = (f'<div class="status busy"><span class="spin"></span>Downloading {what} · {pct:.0f}%{" · " + eta if eta else ""}</div>'
                          f'<div class="bar"><i style="width:{pct:.0f}%"></i></div>')
            elif c["searching"]:
                status = '<div class="status busy"><span class="spin"></span>Searching for the best release…</div>'
            elif c["files"] and not c["missing"]:
                status = (f'<div class="status ok">✓ {"Ready to watch" if c["kind"] == "movie" else str(len(c["files"])) + " episodes ready"}</div>')
            elif c["files"]:
                status = f'<div class="status ok">✓ {len(c["files"])} episodes ready</div><div class="status">{c["missing"]} more wanted, waiting for a release</div>'
            elif c["kind"] == "movie" and c["status"] in ("announced", "inCinemas", "tba"):
                status = '<div class="status">Not released for home viewing yet. It downloads automatically once it is.</div>'
            else:
                status = '<div class="status">Waiting for a release. Sonarr/Radarr keep checking automatically.</div>'
            if c["next"]:
                status += f'<div class="status">Next episode: {html.escape(c["next"][:10])}</div>'
            body = ""
            if c["files"]:
                if c["kind"] == "movie":
                    f = c["files"][0]
                    body = f'<div class="nextup"><div class="n"><b>Movie</b>{html.escape(c["title"])}</div>{self.watch_buttons(f["path"], c["title"])}</div>'
                else:
                    real = lambda e: os.path.realpath(e["path"])
                    resume = next((e for e in c["files"] if real(e) in resumes), None)
                    nxt = next((e for e in c["files"] if real(e) in nu), None)
                    if resume:
                        pick, tag = resume, f"Continue ({resumes[real(resume)]:.0f}% watched)"
                    elif nxt:
                        pick, tag = nxt, "Next up"
                    else:
                        pick, tag = c["files"][0], "Start watching"

                    body = (f'<div class="nextup"><div class="n"><b>{tag}</b>{html.escape(pick["label"])} · {html.escape(pick["title"])}</div>'
                            f'{self.watch_buttons(pick["path"], c["title"] + " " + pick["label"])}</div>')
                    if len(c["files"]) > 1:
                        rows = "".join(f'<div class="ep"><span class="n">{html.escape(e["label"])} · {html.escape(e["title"])}</span>'
                                       f'{self.watch_buttons(e["path"], c["title"] + " " + e["label"])}</div>' for e in reversed(c["files"]))
                        body += f'<details><summary>All episodes ({len(c["files"])})</summary>{rows}</details>'
            out.append(f"""<div class="hcard{' focus' if focus and focus == c['title'] else ''}" id="card-{c['id']}">
<img src="{html.escape(c['poster'])}" alt="" loading="lazy"><div class="info"><h3>{f'<a href="/title/{jf_ids[c["id"]]}">{html.escape(c["title"])}</a>' if c["id"] in jf_ids else html.escape(c['title'])}{f' <span class="muted" style="font-weight:400">({c["year"]})</span>' if c['year'] else ''}</h3>
{status}{body}<div class="hact">{delete_button(c, c["title"], round_btn=False)}</div></div></div>""")
        if focus:   # bring the just-added show to the top
            out.sort(key=lambda h: f'>{html.escape(focus)}' not in h and f'>{html.escape(focus)} <' not in h)
        refresh = '<meta http-equiv="refresh" content="15">' if busy or focus else ""
        empty = '<p class="muted">Nothing added yet. Find something on the <a href="/anime">Anime</a> tab.</p>'
        return (f'{refresh}<a id="top"></a>{msg_html(qs)}<h2>My shows</h2>'
                f'<div class="hub">{"".join(out) if out else empty}</div>'
                f'<p class="hint">{"Updates automatically every 15 seconds while something is searching or downloading. " if busy else ""}'
                f'▶ Watch plays it right here · 📺 Play on TV starts it on the TV.</p>')

    # --- streaming: home, title pages, player ---
    def account_page(self, qs):
        name, color = PROFILES.get(self.user, (self.user.title(), "#888"))
        where = "at home" if self.at_home() else "away from home"
        has_remote = self.user in remote_users()
        field = lambda n, ph, ac="new-password": (f'<input type="password" name="{n}" placeholder="{ph}" autocomplete="{ac}" '
                                                  f'{"minlength=12 " if n != "current" else ""}required>')
        if has_remote:
            website = f"""<form method="post" action="/account/password" class="pwform">
{field("current", "Current website password", "current-password")}{field("new", "New password (12+ characters)")}{field("repeat", "Repeat the new password")}
<button>Change website password</button></form>
<p class="hint">Your authenticator code stays the same. Your other devices get signed out and need the new password (and a code) once.</p>"""
        else:
            website = '<p class="hint">This profile has no internet sign-in.</p>'
        return f"""{msg_html(qs)}<h2>Account</h2>
<div class="acct"><div class="card acard"><div class="who1"><div class="avatar big" style="background:{color}">{esc(name[0])}</div>
<div><b>{esc(name)}</b><div class="muted">Signed in {where}</div></div></div>
<div class="row"><a class="btn ghost" href="/logout">Log out</a></div></div>
<div class="card acard"><h3>Website password</h3><p class="muted">For signing in to {esc(CONF.get("public_host") or "this website")} away from home.</p>{website}</div>
<div class="card acard"><h3>Jellyfin password</h3><p class="muted">For the Jellyfin apps and {esc(CONF.get("watch_host") or "Jellyfin in the browser")} (user <b>{esc(self.user)}</b>).</p>
<form method="post" action="/account/jellyfin" class="pwform">{field("current", "Current Jellyfin password", "current-password")}
{field("new", "New password (12+ characters)")}{field("repeat", "Repeat the new password")}<button>Change Jellyfin password</button></form>
<p class="hint">Apps that are already signed in stay signed in.</p></div></div>"""

    def account_post(self, f):
        ip = self.client_ip()
        if too_many_fails(ip):
            return self.redirect("/account?err=" + urllib.parse.quote("Too many wrong passwords. Try again in 15 minutes."))
        current, new, repeat = f.get("current", ""), f.get("new", ""), f.get("repeat", "")
        if self.path == "/account/password":
            err, what = change_website_password(self.user, current, new, repeat), "Website"
        else:
            err, what = change_jellyfin_password(self.user, current, new, repeat), "Jellyfin"
        if err == "wrong-current":
            record_fail(ip, f"{self.user} (account page)")
            return self.redirect("/account?err=" + urllib.parse.quote(f"The current {what} password isn't right."))
        if err:
            return self.redirect("/account?err=" + urllib.parse.quote(err))
        msg = urllib.parse.quote(f"{what} password changed.")
        if what == "Website" and self.remote_user:      # keep this device signed in under the new password
            return self.redirect("/account?ok=" + msg, session_cookies(self.user, self.public()))
        return self.redirect("/account?ok=" + msg)

    def fetch_all(self, **jobs):
        futs = {k: POOL.submit(*v) for k, v in jobs.items()}
        out = {}
        for k, f in futs.items():
            try:
                out[k] = f.result(timeout=45)
            except Exception:
                out[k] = None
        return out

    def hero_slide(self, s, ep, kicker, k):
        bg = backdrop_of(s, 1920)
        logo = logo_of(s)
        img = (f'<img class="bg" src="{bg}" alt="" fetchpriority="high">' if k == 0 else f'<img class="bg" data-src="{bg}" alt="">')
        name = s.get("Name") or ""
        head = f'<img class="logo" src="{logo}" alt="{esc(name)}">' if logo else f"<h1>{esc(name)}</h1>"
        if ep:
            pct = progress_pct(ep)
            code = ep_code(ep)
            href, label = f"/watch/{ep['Id']}", ("Resume" if pct else "Play") + (f" {code}" if code else "")
            left = time_left(ep)
            epline = (f'<div class="hep"><span class="en">{esc(code)}{" · " if code else ""}{esc(ep.get("Name") or "")}</span>'
                      + (f'<span class="mini"><i style="width:{pct:.0f}%"></i></span><span class="left">{esc(left)}</span>' if pct else "")
                      + "</div>") if s.get("Type") == "Series" else ""
            tv = ep["Id"]
        else:
            href, label, epline, tv = (f"/play/{s['Id']}" if s.get("Type") == "Series" else f"/watch/{s['Id']}"), "Play", "", s["Id"]
        return f"""<div class="slide{' on' if k == 0 else ''}">{img}<div class="hc"><div class="kicker">{esc(kicker)}</div>{head}
{meta_html(s, s.get("ChildCount") if s.get("Type") == "Series" else None)}{epline}
<p class="ov">{esc(s.get("Overview") or "")}</p>
<div class="hbtns"><a class="btn play" href="{href}">{icon("play")}{esc(label)}</a>
<a class="btn glass" href="/title/{s['Id']}">{icon("info")}More info</a>{tv_button(tv)}</div></div></div>"""

    def home_page(self, qs):
        uid = jf_uid(self.user)
        d = self.fetch_all(lib=(library, uid), res=(resume_items, uid), nu=(nextup_items, uid), new=(latest_episodes, uid),
                           dl=(downloads_by_key,), tr=(trending_cached,))
        lib, res, nu, new, dl, tr = (d[k] or ([] if k != "dl" else {}) for k in ("lib", "res", "nu", "new", "dl", "tr"))
        by_id = {i["Id"]: i for i in lib}
        by_key = {provider_key(i): i for i in lib if provider_key(i)}
        week_ago = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - 7 * 86400))

        # hero: what you're in the middle of, then what's next, then the newest arrivals
        heroes, seen = [], set()
        for items, kicker in ((res, "Continue watching"), (nu, "Up next for you")):
            for e in items:
                sid = e.get("SeriesId") or e["Id"]
                if sid in by_id and sid not in seen:
                    heroes.append((by_id[sid], e, kicker))
                    seen.add(sid)
        for s in lib:
            if s["Id"] not in seen:
                fresh = (s.get("DateCreated") or "") > week_ago
                heroes.append((s, None, "New on aniserver" if fresh else "From your library"))
                seen.add(s["Id"])
        heroes = heroes[:6]
        if heroes:
            slides = "".join(self.hero_slide(s, e, kick, k) for k, (s, e, kick) in enumerate(heroes))
            dots = ("<div class=\"dots\">" + "".join(f'<button class="{"on" if k == 0 else ""}" aria-label="Show {k + 1}"></button>'
                                                    for k in range(len(heroes))) + "</div>") if len(heroes) > 1 else ""
            hero = f'<section class="hero">{slides}{dots}</section>'
        else:
            hero = """<section class="empty-hero"><h1>Nothing to watch yet</h1>
<p class="muted">Find a show on Browse; it downloads in the best quality and shows up here when it's ready.</p>
<a class="btn play" href="/anime">Browse anime</a></section>"""

        rows = []
        cw = [ecard(e, e.get("SeriesName") or e.get("Name"), " · ".join(x for x in (ep_code(e), time_left(e)) if x)) for e in res]
        started = {e.get("SeriesId") for e in res}
        cw += [ecard(e, e.get("SeriesName") or "", f"{ep_code(e)} · {e.get('Name') or ''}", tag="Next")
               for e in nu if e.get("SeriesId") not in started]
        rows.append(row_html("Continue watching", cw))
        per_show, newc = {}, []
        for e in new:
            if (e.get("UserData") or {}).get("Played"):
                continue
            per_show[e.get("SeriesId")] = per_show.get(e.get("SeriesId"), 0) + 1
            if per_show[e.get("SeriesId")] > 4:
                continue
            tag = "New" if (e.get("DateCreated") or "") > week_ago else ""
            newc.append(ecard(e, e.get("SeriesName") or "", f"{ep_code(e)} · {e.get('Name') or ''}", tag=tag))
        rows.append(row_html("New episodes", newc))
        fresh_series = {e.get("SeriesId") for e in new if (e.get("DateCreated") or "") > week_ago and not (e.get("UserData") or {}).get("Played")}
        shows = []
        for s in lib:
            if s.get("Type") != "Series":
                continue
            card = dl.get(provider_key(s))
            prog = download_progress(card)
            dl_html = (f'<div class="dl">Downloading · {prog[0]:.0f}%<div class="bar"><i style="width:{prog[0]:.0f}%"></i></div></div>' if prog else "")
            unplayed = (s.get("UserData") or {}).get("UnplayedItemCount")
            sub = year_span(s) + (f" · {unplayed} to watch" if unplayed else " · Watched")
            shows.append(pcard(f"/title/{s['Id']}", poster_of_item(s), s["Name"], sub, "New episodes" if s["Id"] in fresh_series else "", dl_html))
        rows.append(row_html("My shows", shows, "tall", ("/shows", "Downloads & status")))
        movies = [pcard(f"/title/{m['Id']}", poster_of_item(m), m["Name"], " · ".join(meta_bits(m)[:2]))
                  for m in lib if m.get("Type") == "Movie"]
        rows.append(row_html("Movies", movies, "tall"))
        coming = []
        for c in dl.values():
            prog = download_progress(c)
            item = by_key.get(c["id"])
            if not (prog or c["searching"] or (not c["count"] and c["missing"]) or (c["next"] and item)):
                continue
            if prog:
                what = f"{prog[2]} episode{'s' if prog[2] != 1 else ''}" if c["kind"] == "show" else "movie"
                sub = f"Downloading {what}" + (f" · {prog[1]}" if prog[1] else "")
                dl_html = f'<div class="dl">{prog[0]:.0f}%<div class="bar"><i style="width:{prog[0]:.0f}%"></i></div></div>'
            elif c["searching"]:
                sub, dl_html = "Finding the best release…", ""
            elif c["next"]:
                ts = calendar.timegm(time.strptime(c["next"][:19], "%Y-%m-%dT%H:%M:%S"))
                sub, dl_html = "New episode " + time.strftime("%a, %b %-d", time.localtime(ts)), ""
            else:
                sub, dl_html = "Waiting for a release", ""
            href = f"/title/{item['Id']}" if item else f"/shows#card-{c['id']}"
            coming.append(pcard(href, poster_of_item(item) if item else c["poster"], c["title"], sub, "", dl_html))
        rows.append(row_html("On the way", coming, "tall", ("/shows", "See all")))
        trend = []
        for c in (tr or [])[:20]:
            m = c["m"]
            cover = (m.get("coverImage") or {}).get("large") or ""
            nxt = m.get("nextAiringEpisode")
            sub = f"Episode {nxt['episode']} soon" if nxt else (", ".join((m.get("genres") or [])[:2]))
            badge = "In library" if c["state"] in ("have", "movie_done") else ""
            trend.append(pcard("/anime?" + urllib.parse.urlencode({"q": title_of(m), "open": m["id"]}), cover, title_of(m), sub, badge))
        rows.append(row_html("Trending this season", trend, "tall", ("/anime", "Browse all")))
        return f'{hero}<div class="rows{" flat" if not heroes else ""}">{"".join(rows)}</div>'

    def title_page(self, iid, qs):
        uid = jf_uid(self.user)
        it = jf_item(uid, iid)
        self.page_title = f"{it.get('Name')} · aniserver"
        if it.get("Type") == "Episode":
            return ("redirect", f"/title/{it['SeriesId']}?season={it.get('ParentIndexNumber', 1)}#ep-{it['Id']}")
        if it.get("Type") not in ("Series", "Movie"):
            raise ValueError("Not a show or movie")
        card = downloads_by_key().get(provider_key(it))
        prog = download_progress(card)
        strip = ""
        if prog:
            what = f"{prog[2]} episode{'s' if prog[2] != 1 else ''}" if it["Type"] == "Series" else "the movie"
            strip = (f'<div class="strip busy"><span><span class="spin"></span><b>Downloading {what}</b> · {prog[0]:.0f}%'
                     f'{" · " + prog[1] if prog[1] else ""}</span><div class="bar"><i style="width:{prog[0]:.0f}%"></i></div></div>')
        elif card and card["searching"]:
            strip = '<div class="strip busy"><span><span class="spin"></span>Finding the best release for the missing episodes…</span></div>'
        if it["Type"] == "Movie":
            ud = it.get("UserData") or {}
            pct = progress_pct(it)
            label = "Resume" if pct and not ud.get("Played") else ("Watch again" if ud.get("Played") else "Play")
            played_btn = (f'<button class="round" data-played="{it["Id"]}" aria-pressed="{"true" if ud.get("Played") else "false"}" '
                          f'title="{"Mark as unwatched" if ud.get("Played") else "Mark as watched"}">{icon("check")}</button>')
            hero = self.title_hero(it, f"/watch/{it['Id']}", label, it["Id"], played_btn,
                                   (f'<div class="hep"><span class="mini"><i style="width:{pct:.0f}%"></i></span><span class="left">{esc(time_left(it))}</span></div>'
                                    if pct and not ud.get("Played") else ""))
            hero = hero.replace("</div></div></section>", delete_button(card, it.get("Name") or "") + "</div></div></section>", 1)
            return hero + f'<div class="tbody">{strip}{self.about_html(it)}</div>'
        eps = series_episodes(uid, iid)
        target, verb = play_target(uid, iid, eps)
        seasons = sorted({e.get("ParentIndexNumber") if e.get("ParentIndexNumber") is not None else 1 for e in eps},
                         key=lambda n: (n == 0, n))
        tvdb = (it.get("ProviderIds") or {}).get("Tvdb")
        soon = upcoming_episodes(tvdb, set(seasons) | ({max(seasons)} if seasons else set())) if tvdb else {}
        seasons = sorted(set(seasons) | set(soon), key=lambda n: (n == 0, n))
        want = (qs.get("season") or [""])[0]
        current = int(want) if want.lstrip("-").isdigit() and int(want) in seasons else (
            target.get("ParentIndexNumber") if target and target.get("ParentIndexNumber") in seasons else (seasons[0] if seasons else 1))
        if card and card.get("next") and not prog:
            ts = calendar.timegm(time.strptime(card["next"][:19], "%Y-%m-%dT%H:%M:%S"))
            strip += f'<div class="strip ok"><span>New episode <b>{time.strftime("%A, %b %-d · %-I:%M %p", time.localtime(ts))}</b> · downloads automatically</span></div>'
        tabs = "".join(f'<button class="{"on" if n == current else ""}" data-season="{n}">{"Specials" if n == 0 else f"Season {n}"}</button>'
                       for n in seasons) if len(seasons) > 1 else (f'<button class="on" data-season="{current}">{"Specials" if current == 0 else f"Season {current}"}</button>')
        season_ids = {e.get("ParentIndexNumber", 1): e.get("SeasonId") for e in eps}
        sacts = ""
        for n in seasons:
            sid = season_ids.get(n)
            if not sid:
                continue
            all_seen = all((e.get("UserData") or {}).get("Played") for e in eps if e.get("ParentIndexNumber", 1) == n)
            sacts += (f'<button class="ghost small" data-played="{sid}" data-scope="season" data-for="{n}" aria-pressed="{"true" if all_seen else "false"}"'
                      f'{"" if n == current else " hidden"}>{icon("check")}{"Mark season unwatched" if all_seen else "Mark season watched"}</button>')
        lists = []
        for n in seasons:
            rows = [self.episode_row(e, target) for e in eps if (e.get("ParentIndexNumber") if e.get("ParentIndexNumber") is not None else 1) == n]
            have = {e.get("IndexNumber") for e in eps if e.get("ParentIndexNumber", 1) == n}
            for u in soon.get(n, []):
                if u["n"] in have:
                    continue
                rows.append(f"""<div class="er soon"><div class="th">E{u['n']}</div><div class="info"><div><h4>{u['n']}. {esc(u['title'])}</h4>
<div class="em"><span class="{'now' if u['kind'] == 'dl' else ''}">{esc(u['status'])}</span></div></div>
<p>{esc(u['overview'])}</p></div><div class="acts"></div></div>""")
            lists.append(f'<div class="eplist" data-list="{n}"{"" if n == current else " hidden"}>{"".join(rows)}</div>')
        if target:
            code = ep_code(target)
            pct = progress_pct(target)
            epline = (f'<div class="hep"><span class="en">{esc(code)} · {esc(target.get("Name") or "")}</span>'
                      + (f'<span class="mini"><i style="width:{pct:.0f}%"></i></span><span class="left">{esc(time_left(target))}</span>' if pct else "")
                      + "</div>")
            hero = self.title_hero(it, f"/watch/{target['Id']}", f"{verb} {code}", target["Id"], delete_button(card, it.get("Name") or ""), epline)
        else:
            hero = self.title_hero(it, None, "", None, delete_button(card, it.get("Name") or ""),
                                   '<div class="hep"><span class="left">No episodes downloaded yet</span></div>')
        return (hero + f'<div class="tbody">{strip}<div class="seasonbar"><div class="seasons">{tabs}</div><div class="sact">{sacts}</div></div>'
                + "".join(lists) + self.about_html(it, eps) + "</div>")

    def title_hero(self, it, href, label, tv_id, extra_btn, epline):
        logo = logo_of(it)
        name = it.get("Name") or ""
        head = f'<img class="logo" src="{logo}" alt="{esc(name)}">' if logo else f"<h1>{esc(name)}</h1>"
        play = f'<a class="btn play" href="{href}">{icon("play")}{esc(label)}</a>' if href else ""
        seasons = it.get("ChildCount") if it.get("Type") == "Series" else None
        return f"""<section class="thero"><img class="bg" src="{backdrop_of(it, 1920)}" alt="" fetchpriority="high">
<div class="hc">{head}{meta_html(it, seasons)}{epline}<p class="ov" title="Show more">{esc(it.get("Overview") or "")}</p>
<div class="hbtns">{play}{tv_button(tv_id) if tv_id else ""}{extra_btn}</div></div></section>"""

    def episode_row(self, e, target):
        ud = e.get("UserData") or {}
        pct = progress_pct(e)
        seen = bool(ud.get("Played"))
        aired = (e.get("PremiereDate") or "")[:10]
        aired_txt = time.strftime("%b %-d, %Y", time.strptime(aired, "%Y-%m-%d")) if aired else ""
        is_next = target and e["Id"] == target["Id"]
        em = [x for x in (('<span class="now">' + ("Continue" if pct and not seen else "Up next") + "</span>") if is_next else "",
                          esc(aired_txt), esc(fmt_runtime(e.get("RunTimeTicks"))),
                          esc(time_left(e)) if pct and not seen else "") if x]
        num = e.get("IndexNumber")
        return f"""<div class="er{' played' if seen else ''}" id="ep-{e['Id']}">
<a class="th" href="/watch/{e['Id']}"><img class="f" src="{thumb_of(e, 640)}" alt="" loading="lazy" onload="this.classList.add('ok')">{PLAY_OVERLAY}
{f'<div class="pbar"><i style="width:{pct:.1f}%"></i></div>' if pct and not seen else ''}</a>
<div class="info"><div><h4><a href="/watch/{e['Id']}">{f"{num}. " if num is not None else ""}{esc(e.get('Name') or '')}</a></h4>
<div class="em">{'<span class="dot">·</span>'.join(f'<span>{x}</span>' if not x.startswith('<span') else x for x in em)}</div></div>
<p>{esc(e.get('Overview') or '')}</p></div>
<div class="acts">{tv_button(e['Id'], 'Play this on the TV')}<button class="round" data-played="{e['Id']}" aria-pressed="{'true' if seen else 'false'}"
title="{'Mark as unwatched' if seen else 'Mark as watched'}">{icon('check')}</button></div></div>"""

    def about_html(self, it, eps=None):
        bits = []
        if it.get("Genres"):
            bits.append(f'<div><b>Genres</b>{esc(", ".join(it["Genres"]))}</div>')
        if it.get("Studios"):
            bits.append(f'<div><b>Studios / network</b>{esc(", ".join(s["Name"] for s in it["Studios"][:4]))}</div>')
        if eps:
            bits.append(f'<div><b>On this server</b>{len(eps)} episode{"s" if len(eps) != 1 else ""} · '
                        f'{sum(1 for e in eps if (e.get("UserData") or {}).get("Played"))} watched</div>')
        if it.get("Status"):
            bits.append(f'<div><b>Status</b>{"Still airing" if it["Status"] == "Continuing" else "Finished"}</div>')
        return f'<div class="about">{"".join(bits)}</div>' if bits else ""

    def watch_page(self, iid, qs):
        info = playinfo(self.user, iid)
        home = self.at_home()
        start = (qs.get("t") or [""])[0]
        ip = self.client_ip()
        net_id = "home" if home else "n" + hashlib.sha256((".".join(ip.split(".")[:2]) if "." in ip else ":".join(ip.split(":")[:3])).encode()).hexdigest()[:10]
        cfg = {"net": "home" if home else "away", "netId": net_id, "start": float(start) if re.fullmatch(r"\d+(\.\d+)?", start) else None,
               "qualities": [["auto", "Auto", "Best your connection can play without buffering"], ["original", "Original", "Always the original file"],
                             ["1080", "1080p", "10 Mbps · converted"], ["720", "720p", "3.5 Mbps · converted"], ["480", "480p", "1.8 Mbps · data saver"]],
               "ladder": LADDER, "ladderHevc": LADDER_HEVC,
               "jassub": "/static/jassub-1.8.8/", "remoteMax": REMOTE_MAX_BITRATE,
               "icons": {k: icon(k) for k in ("play", "pause", "vol", "mute", "fs", "fsx", "check")}}
        js = lambda o: json.dumps(o).replace("</", "<\\/")
        return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><meta name="theme-color" content="#000000">{ICON_TAGS}
<title>{esc((info['code'] + ' · ' if info['code'] else '') + (info['show'] or ''))}</title><link rel="stylesheet" href="{asset('player.css')}"></head><body>
<div id="player" class="paused buffering">
<video id="v" playsinline webkit-playsinline preload="auto" poster="{esc(info['still'])}"></video>
<div class="stage"></div>
<div class="shade-t"></div><div class="shade-b"></div>
<div class="rip l"><div>{jump_icon(False)}10 seconds</div></div><div class="rip r"><div>{jump_icon(True)}10 seconds</div></div>
<div class="spinner"></div>
<div class="ui center"><button class="ib mid" id="crw" aria-label="Back 10 seconds">{jump_icon(False)}</button>
<button class="ib big" aria-label="Play or pause">{icon("play")}</button>
<button class="ib mid" id="cff" aria-label="Forward 10 seconds">{jump_icon(True)}</button></div>
<div class="ui topbar"><a class="ib" id="back" href="/title/{info['series']}" aria-label="Back">{icon("back")}</a><div class="t"><b></b><span></span></div></div>
<button class="skip">Skip Opening {icon("next")}</button>
<div class="upnext"><img alt=""><div class="m"><small>Next episode</small><b></b><button class="go"><i></i>{icon("play")}Play now</button></div>
<button class="x" aria-label="Dismiss">✕</button></div>
<div class="ui bottom">
<div class="seek"><div class="track"><div class="hov"></div><div class="buf"></div><div class="played"></div><div class="chaps"></div></div>
<div class="knob"></div><div class="tip"><div class="tthumb"></div><div class="ttext"></div></div></div>
<div class="bar"><button class="ib" id="pp" aria-label="Play or pause (Space)"></button>
<button class="ib hide-sm" id="rw" title="Back 10 seconds (←)">{jump_icon(False)}</button>
<button class="ib hide-sm" id="ff" title="Forward 10 seconds (→)">{jump_icon(True)}</button>
<div class="vol"><button class="ib" id="mute" title="Mute (M)"></button><input id="vr" type="range" min="0" max="1" step="0.05" aria-label="Volume"></div>
<span class="time">0:00 / 0:00</span><span class="grow"></span>
<button class="ib" id="nextbtn" hidden>{icon("next")}</button>
<button class="ib" id="ccbtn" title="Audio & subtitles (C)">{icon("cc")}</button>
<button class="ib" id="setbtn" title="Quality & speed">{icon("gear")}</button>
<button class="ib" id="tvbtn" title="Continue on the TV from here">{icon("tv")}</button>
<button class="ib hide-sm" id="pip" title="Picture in picture" hidden>{icon("pip")}</button>
<button class="ib" id="fs" title="Full screen (F)"></button></div></div>
<div class="panel"></div>
<div class="cover"><img class="cbg" alt=""><div class="in"><div class="brand"></div><div class="ep"></div><div class="spinner"></div>
<button class="pill tap" id="tapplay">{icon("play")}Play</button></div></div>
<div class="toast"></div>
<div class="sheet" id="tvsheet" hidden><div>{icon("tv", "ic hero")}<h3>Playing on the TV</h3><p id="tvmsg"></p>
<div class="pills"><button class="pill" id="here">{icon("play")}Keep watching here</button><a class="pill ghost" href="/title/{info['series']}">Back to the show</a></div></div></div>
<div class="sheet" id="endsheet" hidden><div><h3>You're all caught up</h3><p>That was the latest episode on the server.</p>
<div class="pills"><a class="pill" href="/title/{info['series']}">Back to the show</a><a class="pill ghost" href="/">Home</a></div></div></div>
<div class="sheet" id="errsheet" hidden><div><h3>Couldn't play this</h3><p id="errmsg"></p>
<div class="pills"><button class="pill" id="retry">Try again</button><a class="pill ghost" href="/title/{info['series']}">Back</a></div></div></div>
</div>
<script type="application/json" id="info">{js(info)}</script><script type="application/json" id="cfg">{js(cfg)}</script>
<script>addEventListener('error', e => {{ try {{ navigator.sendBeacon('/api/log', new URLSearchParams({{ev: 'js-error', mode: '',
id: location.pathname.split('/').pop(), msg: (e.message || '') + ' @' + (e.filename || '').split('/').pop() + ':' + e.lineno}})); }} catch (x) {{}} }});
if (/[?&]debug/.test(location.search)) ['error', 'warn', 'log'].forEach(k => {{ const o = console[k]; console[k] = (...a) => {{ o.apply(console, a);
try {{ navigator.sendBeacon('/api/log', new URLSearchParams({{ev: 'console-' + k, mode: '', id: '', msg: a.map(x => x && x.stack ? x.stack : (typeof x === 'object' ? JSON.stringify(x) : String(x))).join(' ').slice(0, 300)}})); }} catch (x) {{}} }}; }});
addEventListener('unhandledrejection', e => {{ try {{ navigator.sendBeacon('/api/log', new URLSearchParams({{ev: 'js-reject', mode: '',
id: location.pathname.split('/').pop(), msg: String(e.reason && (e.reason.stack || e.reason.message) || e.reason).slice(0, 300)}})); }} catch (x) {{}} }});</script>
<script src="/static/hls-1.6.15.min.js"></script><script src="/static/jassub-1.8.8/jassub.umd.js"></script><script src="{asset('player.js')}"></script></body></html>"""

    # --- streaming: the HLS proxy, static files, JSON API ---
    def stream(self, path, query, head=False):
        m = STREAM_PATH.fullmatch(path)
        if not m:
            return self.send("Not found", 404)
        iid, rest = m.groups()
        pairs = urllib.parse.parse_qsl(query, keep_blank_values=True)
        q = dict(pairs)
        tok = q.get("_t", "")
        if not stream_token_ok(iid, tok):
            if not self.authed():
                return self.send("Forbidden", 403)
            tok = stream_token(iid)
        if rest == "file":
            return self.serve_file(iid, head)
        if rest == "master.m3u8" and q.get("m") == "abr":
            data = abr_master(iid, q, tok).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.apple.mpegurl")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
            return
        if rest == "master.m3u8":
            upstream = f"/Videos/{iid}/master.m3u8?" + jellyfin_master_query(iid, q)
        elif rest.startswith("sub/"):         # text subtitles as .ass (SRT converted), drawn in the browser
            upstream = f"/Videos/{iid}/{iid}/Subtitles/{rest[4:-4]}/0/Stream.ass"
        elif rest.startswith("att/"):         # fonts embedded in the release
            upstream = f"/Videos/{iid}/{iid}/Attachments/{rest[4:]}"
        elif rest.startswith("trick/"):       # seek-bar preview tiles (10x10 thumbnails each)
            width, tile = rest[6:-4].split("/")
            upstream = f"/Videos/{iid}/Trickplay/{width}/{tile}.jpg?mediaSourceId={iid}"
        else:
            upstream = f"/Videos/{iid}/{rest}?" + urllib.parse.urlencode(
                [(k, v) for k, v in pairs if k.lower() not in ("_t", "api_key", "apikey")])
        req = urllib.request.Request(JELLYFIN + upstream, headers={"Authorization": f'MediaBrowser Token="{CONF["jellyfin_key"]}"'})
        try:
            r = urllib.request.urlopen(req, timeout=150)
        except urllib.error.HTTPError as e:
            return self.send(f"Stream error {e.code}", 404 if e.code == 404 else 502)
        except Exception:
            return self.send("Stream unavailable", 502)
        try:
            with r:
                if rest.endswith(".m3u8"):
                    data = tokenize_playlist(r.read().decode("utf-8", "replace"), tok).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/vnd.apple.mpegurl")
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(data)
                    return
                length = r.headers.get("Content-Length")
                data = None if length else r.read()
                self.send_response(200)
                self.send_header("Content-Type", r.headers.get("Content-Type") or "video/mp2t")
                self.send_header("Content-Length", length or str(len(data)))
                self.send_header("Cache-Control", "private, max-age=2592000" if rest.startswith(("att/", "trick/")) else "private, max-age=600")
                self.end_headers()
                if data is not None:
                    self.wfile.write(data)
                else:
                    shutil.copyfileobj(r, self.wfile, 256 * 1024)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass

    def serve_file(self, iid, head=False):
        real = _FILE_PATHS.get(iid)
        if not real:
            try:
                real = _FILE_PATHS[iid] = media_path(jf_item(jf_uid(OWNER), iid).get("Path"))
            except Exception:
                return self.send("Not found", 404)
        size = os.path.getsize(real)
        start, end = 0, size - 1
        rng = (self.headers.get("Range") or "").strip()
        if rng:
            m = re.fullmatch(r"bytes=(\d*)-(\d*)", rng)
            if not m or not (m.group(1) or m.group(2)):
                return self.send("Bad range", 416, {"Content-Range": f"bytes */{size}"})
            if m.group(1):
                start, end = int(m.group(1)), min(int(m.group(2)) if m.group(2) else size - 1, size - 1)
            else:
                start = max(0, size - int(m.group(2)))
            if start > end or start >= size:
                return self.send("Bad range", 416, {"Content-Range": f"bytes */{size}"})
        ext = os.path.splitext(real)[1].lower()
        self.send_response(206 if rng else 200)
        self.send_header("Content-Type", {".mkv": "video/x-matroska", ".mp4": "video/mp4", ".m4v": "video/mp4", ".webm": "video/webm"}.get(ext, "application/octet-stream"))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if rng:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Cache-Control", "private, max-age=3600")
        self.end_headers()
        if head:
            return
        try:
            with open(real, "rb") as fh:
                self.connection.sendfile(fh, start, end - start + 1)
        except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
            pass       # the browser cancels range requests all the time while seeking

    def do_HEAD(self):
        url = urllib.parse.urlparse(self.path)
        if url.path.startswith("/stream/") and url.path.endswith("/file"):
            return self.stream(url.path, url.query, head=True)
        self.send_response(405)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def serve_asset(self, path):
        asset("site.css")                     # make sure they're built
        hit = ASSET_FILES.get(path)
        if not hit:
            return self.send("Not found", 404)
        data, zipped, ctype = hit
        use_gz = "gzip" in (self.headers.get("Accept-Encoding") or "")
        body = zipped if use_gz else data
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "public, max-age=31536000, immutable")
        self.send_header("Vary", "Accept-Encoding")
        if use_gz:
            self.send_header("Content-Encoding", "gzip")
        self.end_headers()
        self.wfile.write(body)

    def static(self, path):
        name = path[len("/static/"):]
        f = STATIC / name
        if not re.fullmatch(r"(?:[A-Za-z0-9._-]+/)?[A-Za-z0-9._-]+\.(js|css|svg|png|woff2|wasm|ico|webmanifest)", name) or ".." in name or not f.is_file():
            return self.send("Not found", 404)
        gz = f.with_name(f.name + ".gz")
        use_gz = gz.is_file() and "gzip" in (self.headers.get("Accept-Encoding") or "")
        data = (gz if use_gz else f).read_bytes()
        ctype = {"js": "text/javascript", "css": "text/css", "svg": "image/svg+xml", "png": "image/png", "woff2": "font/woff2",
                 "wasm": "application/wasm", "ico": "image/x-icon", "webmanifest": "application/manifest+json"}[f.suffix[1:]]
        self.send_response(200)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype.startswith("text") else ""))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "public, max-age=31536000, immutable")
        self.send_header("Vary", "Accept-Encoding")
        if use_gz:
            self.send_header("Content-Encoding", "gzip")
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, obj, code=200):
        self.send(json.dumps(obj), code, {"Content-Type": "application/json", "Cache-Control": "no-store"})

    def api_post(self, f):
        iid = f.get("id", "")
        if self.path in ("/api/progress", "/api/tv", "/api/played") and not HEX32.fullmatch(iid):
            return self.send_json({"ok": False, "msg": "Unknown item"}, 400)
        try:
            if self.path == "/api/progress":
                save_progress(self.user, iid, float(f.get("pos") or 0), f.get("played") == "1")
                return self.send_json({"ok": True})
            if self.path == "/api/log":
                PLAYER_LOG.parent.mkdir(parents=True, exist_ok=True)
                with PLAYER_LOG.open("a") as fh:
                    fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {self.user} {self.client_ip()} {'home' if self.at_home() else 'away'} "
                             f"{f.get('id', '')[:8]} {f.get('mode', '')[:12]} {f.get('ev', '')[:20]}: {f.get('msg', '')[:300]} "
                             f"| {(self.headers.get('User-Agent') or '')[:120]}\n")
                return self.send_json({"ok": True})
            if self.path == "/api/stop":
                stop_transcode(f.get("d", ""), f.get("psid", ""))
                return self.send_json({"ok": True})
            if self.path == "/api/delete":
                key = f.get("key", "")
                if not re.fullmatch(r"[sm]\d+", key):
                    return self.send_json({"ok": False, "msg": "Unknown title"}, 400)
                title, size = delete_title(key, self.user)
                return self.send_json({"ok": True, "msg": f"Deleted {title}" + (f" and freed {size / 1e9:.1f} GB." if size else ".")})
            if self.path == "/api/played":
                set_played(self.user, iid, f.get("on") == "1")
                return self.send_json({"ok": True})
            if self.path == "/api/tv":
                it = jf_item(jf_uid(self.user), iid)
                if it.get("Type") in ("Series", "Season"):
                    target, _ = play_target(jf_uid(self.user), it.get("SeriesId") or iid)
                    if not target:
                        return self.send_json({"ok": False, "msg": "Nothing downloaded for this show yet."})
                    iid = target["Id"]
                pos = float(f["pos"]) if re.fullmatch(r"\d+(\.\d+)?", f.get("pos") or "") else None
                name, at = tv_play_item(self.user, iid, pos)
                return self.send_json({"ok": True, "msg": f"Playing {name} on the TV" + (f" from {int(at // 60)}:{int(at % 60):02d}" if at > 5 else "")})
        except Exception as e:
            return self.send_json({"ok": False, "msg": f"That didn't work: {e}"}, 500)
        return self.send_json({"ok": False, "msg": "Unknown action"}, 404)

    def anime_page(self, qs):
        q = (qs.get("q") or [""])[0].strip()
        form = f"""{msg_html(qs)}<form class="search" method="get" action="/anime">
<div class="bigsearch">{SEARCH_ICON}<input type="text" name="q" value="{html.escape(q)}"
placeholder="Search anime: re zero, jjk, frieren, fmab…" autocomplete="off" {'' if q else 'autofocus'}></div>
<button type="submit">Search</button></form>
<p class="hint">Tap a show to see details and add it. Sonarr then downloads it in the best quality it can find.</p>"""
        try:
            cards = anime_search(q) if q else trending_cached()
        except Exception as e:
            return form + f'<div class="msg err">Couldn\'t load anime: {html.escape(str(e))}</div>'
        if not cards:
            return form + '<p class="muted">No anime found. Try another spelling or a shorter name.</p>'
        back = "/anime?" + urllib.parse.urlencode({"q": q}) if q else "/anime"
        heading = f"Results for “{html.escape(q)}”" if q else "Trending now"
        posters = "".join(poster_html(c, n) for n, c in enumerate(cards))
        templates = "".join(details_html(c, n, back, self.host()) for n, c in enumerate(cards))
        return form + f'<h2>{heading}</h2><div class="posters">{posters}</div>{templates}'

    def login_form(self, qs):
        err = f'<div class="msg err">{html.escape(qs["err"][0])}</div>' if "err" in qs else ""
        nxt = f'<input type="hidden" name="next" value="{html.escape((qs.get("next") or ["/"])[0])}">'
        trusted = self.trusted_user()
        code = ("" if trusted else '<input type="text" name="code" placeholder="6-digit code from your authenticator app" '
                'inputmode="numeric" autocomplete="one-time-code" pattern="[0-9 ]{6,7}" required>')
        note = ('<p class="hint">This device is remembered, so no code needed.</p>' if trusted else
                '<p class="hint">You only need the code once in each browser. A private window, an aniserver icon on your '
                'home screen, or a link opened inside another app counts as a new browser.</p>')
        return f"""<div class="login"><div class="brand"><i></i>aniserver</div>{err}
<form method="post" action="/login">{nxt}<input type="text" name="username" placeholder="Username" autocomplete="username" value="{html.escape(trusted or '')}" {'' if trusted else 'autofocus'} required>
<input type="password" name="password" placeholder="Password" autocomplete="current-password" {'autofocus' if trusted else ''} required>
{code}<button type="submit">Log in</button>{note}</form></div>"""

    # --- remote access setup (home network / Tailscale only) ---
    def security_page(self, qs, setup=None):
        users = remote_users()
        rows = []
        for u in REMOTE_NAMES:
            on = u in users
            rows.append(f"""<tr><td class="title"><b>{u}</b></td><td>{'<span class="chip ok">On</span>' if on else '<span class="muted">Not set up</span>'}</td>
<td class="act"><form method="post" action="/security/start" style="display:flex;gap:8px;flex-wrap:wrap;justify-content:flex-end">
<input type="hidden" name="user" value="{u}"><input type="password" name="pw" placeholder="New remote password (12+ chars)" minlength="12" required style="width:auto">
<input type="password" name="pw2" placeholder="Repeat" minlength="12" required style="width:auto"><button class="small">{'Reset' if on else 'Set up'}</button></form>
{f'<form method="post" action="/security/disable"><input type="hidden" name="user" value="{u}"><button class="ghost small">Turn off</button></form>' if on and u != OWNER else ''}</td></tr>""")
        public = CONF.get("public_host")
        intro = f"""{msg_html(qs)}<h2>Remote access</h2>
<p class="hint">This page only works at home or over Tailscale. From the internet{f' (<b>https://{html.escape(public)}</b>)' if public else ''},
the website asks for a username, the remote password set here, and a 6-digit code from an authenticator app
(Google Authenticator, Microsoft Authenticator, 1Password, Aegis…). Use a long password you don't use anywhere else.</p>"""
        body = intro + f'<div class="card scroll"><table><tbody>{"".join(rows)}</tbody></table></div>'
        if setup:
            u, secret = setup
            uri = f"otpauth://totp/HTPC:{u}?secret={secret}&issuer=HTPC&digits=6&period=30"
            body += f"""<div class="card" style="padding:20px;margin-top:20px"><h2>Scan this for <b>{u}</b></h2>
<div style="display:flex;gap:24px;flex-wrap:wrap;align-items:center"><div id="qr" style="background:#fff;padding:10px;border-radius:10px"></div>
<div style="min-width:0"><p class="hint">Open your authenticator app → add account → scan the QR code. On a phone you can tap
<a href="{html.escape(uri)}">this link</a> instead, or type the key manually:</p>
<p style="font:600 16px ui-monospace,monospace;letter-spacing:.08em;overflow-wrap:anywhere">{" ".join(secret[i:i + 4] for i in range(0, len(secret), 4))}</p>
<form method="post" action="/security/verify" class="search" style="max-width:420px"><input type="hidden" name="user" value="{u}">
<input type="text" name="code" placeholder="Enter the 6-digit code it shows" inputmode="numeric" autocomplete="one-time-code" required>
<button>Confirm</button></form></div></div></div>
<script src="https://cdnjs.cloudflare.com/ajax/libs/qrcodejs/1.0.0/qrcode.min.js"></script>
<script>new QRCode(document.getElementById('qr'), {{text: {json.dumps(uri)}, width: 200, height: 200}});</script>"""
        return body

    def security_post(self, f):
        u = f.get("user", "")
        if u not in REMOTE_NAMES:
            return self.redirect("/security")
        if self.path == "/security/start":
            pw = f.get("pw", "")
            if len(pw) < 12 or pw != f.get("pw2"):
                return self.redirect("/security?err=" + urllib.parse.quote("Passwords must match and be at least 12 characters."))
            if pw.lower() in [p.lower() for p in CONF.get("reserved_passwords", [])]:
                return self.redirect("/security?err=" + urllib.parse.quote("Pick a password you don't use for anything else."))
            secret = base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")
            PENDING[u] = (hash_password(pw), secret)
            return self.send(page(self.security_page({}, (u, secret)), "anime", self.host(), "aniserver · Remote access"))
        if self.path == "/security/verify":
            if u not in PENDING:
                return self.redirect("/security?err=" + urllib.parse.quote("Setup expired, start again."))
            pw_hash, secret = PENDING[u]
            counter = totp_ok(secret, f.get("code"))
            if counter is None:
                return self.send(page('<div class="msg err">That code didn\'t match. Check the app and try again.</div>'
                                      + self.security_page({}, (u, secret)), "anime", self.host(), "aniserver · Remote access"))
            users = remote_users()
            users[u] = {"pw": pw_hash, "totp": secret, "last": counter, "since": time.strftime("%Y-%m-%d")}
            save_remote_users(users)
            PENDING.pop(u, None)
            return self.redirect("/security?ok=" + urllib.parse.quote(f"Remote login is on for {u}."))
        if self.path == "/security/disable":
            if u == OWNER:                               # the only way in is your own login: never switch it off
                return self.redirect("/security?err=" + urllib.parse.quote("You can't turn off your own login (it's the only way in)."))
            users = remote_users()
            users.pop(u, None)
            save_remote_users(users)
            return self.redirect("/security?ok=" + urllib.parse.quote(f"Remote login turned off for {u}."))
        return self.redirect("/security")

    def search_page(self, qs):
        q = (qs.get("q") or [""])[0].strip()
        # hidden "0" + checkbox "1": the checkbox is ticked when any submitted value is "1"; default on
        digital = "1" in (qs.get("digital") or ["1"])
        form = f"""{msg_html(qs)}
<form class="search" method="get" action="/manga" style="flex-wrap:wrap">
<div class="bigsearch">{SEARCH_ICON}<input type="text" name="q" value="{html.escape(q)}" placeholder="Search manga by English title, e.g. Gachiakuta" {'' if q else 'autofocus'}></div>
<button type="submit">Search</button>
<input type="hidden" name="digital" value="0">
<label style="flex-basis:100%"><input type="checkbox" name="digital" value="1" {"checked" if digital else ""}> Official digital releases only</label></form>
<p class="hint">Download grabs what's listed; Follow grabs future volumes from the same uploader.</p>"""
        if not q:
            return form
        try:
            results = search(q, digital)
        except Exception as e:
            return form + f'<div class="msg err">Search failed (Nyaa may be down or slow): {html.escape(str(e))}</div>'
        if not results:
            return form + '<p class="muted">No results. Try a shorter title or untick “Official digital releases only”.</p>'
        back = html.escape("/manga?" + urllib.parse.urlencode({"q": q, "digital": "1" if digital else "0"}))
        rows = []
        for r in results:
            t = r["title"]
            series, uploader = parse_release(t)
            date = (r.get("publishDate") or "")[:10]
            rows.append(f"""<tr><td class="title">{html.escape(t)}</td>
<td class="num">{fmt_size(r.get("size") or 0)}</td><td class="num">{r.get("seeders") or 0}</td><td class="num">{date}</td>
<td class="act"><form method="post" action="/grab"><input type="hidden" name="guid" value="{html.escape(r["guid"])}">
<input type="hidden" name="title" value="{html.escape(t)}"><input type="hidden" name="back" value="{back}"><button class="small">Download</button></form>
<form method="post" action="/follow"><input type="hidden" name="series" value="{html.escape(series)}">
<input type="hidden" name="uploader" value="{html.escape(uploader)}"><input type="hidden" name="back" value="{back}">
<button class="ghost small" title="Auto-download new volumes of “{html.escape(series)}”{" from " + html.escape(uploader) if uploader else ""}">Follow</button></form></td></tr>""")
        return form + f"""<h2>Results for “{html.escape(q)}”</h2><div class="card scroll"><table><thead><tr><th>Release</th><th style="text-align:right">Size</th>
<th style="text-align:right">Seeders</th><th style="text-align:right">Uploaded</th><th></th></tr></thead><tbody>{"".join(rows)}</tbody></table></div>"""

    def downloads_page(self, qs):
        try:
            data = live_downloads()
        except Exception as e:
            return f'<div class="msg err">Couldn\'t reach the downloader: {html.escape(str(e))}</div>'
        js = json.dumps(data).replace("</", "<\\/")
        return f"""{msg_html(qs)}<div class="dlhead"><h2>Downloads</h2><span class="muted" id="dlsum"></span></div>
<div id="dl"></div><p class="hint">Live: updates every 2 seconds. Finished downloads stay listed for 3 days.</p>
<script type="application/json" id="dldata">{js}</script><script src="{asset('downloads.js')}"></script>"""

    def follows_page(self, qs):
        try:
            fl = follows()
        except Exception as e:
            return f'<div class="msg err">Couldn\'t reach qBittorrent: {html.escape(str(e))}</div>'
        if not fl:
            return msg_html(qs) + '<h2>Followed manga</h2><p class="muted">Not following anything yet. Use Follow next to a manga search result.</p>'
        rows = "".join(f"""<tr><td class="title">{html.escape(f["series"])}</td><td>{html.escape(f["uploader"]) or '<span class="muted">any</span>'}</td>
<td class="act"><form method="post" action="/unfollow"><input type="hidden" name="series" value="{html.escape(f["series"])}">
<input type="hidden" name="back" value="/following"><button class="ghost small">Unfollow</button></form></td></tr>""" for f in fl)
        return msg_html(qs) + f"""<h2>Followed manga</h2><div class="card scroll"><table><thead><tr><th>Series</th><th>Uploader</th><th></th></tr></thead>
<tbody>{rows}</tbody></table></div><p class="hint">New single volumes (e.g. “v13 … (Digital)”) are checked every hour and download automatically.</p>"""


def warm_caches():
    """Ahead of time, so nothing waits at play time: AniList trending, download status, every episode's embedded font
    names (subtitles) and bitrate profile (choosing original vs adaptive away from home)."""

    def run():
        for key, ttl, fn in (("trending", 900, trending), ("dlcards", 15, _downloads_by_key)):
            try:
                swr(key, ttl, fn)
            except Exception:
                pass
        while True:
            try:
                items = jellyfin("/Items?Recursive=true&IncludeItemTypes=Episode,Movie&Fields=MediaSources,Path")["Items"]
                if not _RATES:
                    try:
                        _RATES.update(json.loads(RATES_CACHE.read_text()))
                    except (OSError, ValueError):
                        _RATES["_"] = {}
                for it in items:
                    if it["Id"] not in _FONT_NAMES:
                        item_fonts(it["Id"], ((it.get("MediaSources") or [{}])[0]).get("MediaAttachments") or [])
                    if it["Id"] not in _RATES and it.get("Path"):
                        try:
                            bitrate_profile(it["Id"], media_path(it["Path"]))
                        except Exception:
                            pass
            except Exception:
                pass
            time.sleep(1800)
    threading.Thread(target=run, daemon=True).start()


if __name__ == "__main__":
    warm_caches()
    ThreadingHTTPServer(("0.0.0.0", CONF.get("port", 5050)), Handler).serve_forever()
