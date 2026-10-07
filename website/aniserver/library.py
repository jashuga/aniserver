"""What's on the server: Sonarr/Radarr status cards, live downloads, deleting titles, Jellyfin library queries."""
import base64
import calendar
import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from aniserver.config import CONF, KODI, MEDIA_ROOTS, OWNER
from aniserver.services import TORRENT_CATEGORIES, _JF_CACHE, _SWR, jellyfin, jf_maps, kodi_rpc, qbit, qbit_live, radarr, sonarr, swr
from aniserver.manga import parse_release

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
    names, pending, imported = swr("dlnames", 10, _download_names, 6 * 3600)    # names rarely change: never wait for them
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
SEARCH_COMMANDS = ("SeriesSearch", "SeasonSearch", "EpisodeSearch", "MissingEpisodeSearch", "MoviesSearch")


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


def queue_numbers(r, live):
    """Size / left / eta / speed for a Sonarr/Radarr queue record: qBittorrent's live numbers when it has the torrent
    (Sonarr/Radarr only refresh theirs about once a minute)."""
    t = live.get((r.get("downloadId") or "").upper())
    if t:
        return {"size": t.get("size") or 0, "left": t.get("amount_left") or 0, "speed": t.get("dlspeed") or 0,
                "eta": t["eta"] if 0 < (t.get("eta") or 0) < 8640000 and t.get("dlspeed") else 0}
    return {"size": r.get("size") or 0, "left": r.get("sizeleft") or 0, "speed": 0, "eta": parse_timeleft(r.get("timeleft"))}


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


FETCH = ThreadPoolExecutor(8)     # hub_cards' requests run side by side (its own pool: POOL may be the one calling it)
_SHOW_FILES = {}                  # series id -> (files stamp, time, episodefile list, episode list)


def show_files(s):
    """A show's episode files + episodes from Sonarr, kept until its files change (count/size) or 10 minutes pass."""
    st = s.get("statistics") or {}
    stamp = (st.get("episodeFileCount"), st.get("sizeOnDisk"))
    hit = _SHOW_FILES.get(s["id"])
    if hit and hit[0] == stamp and time.time() - hit[1] < 600:
        return hit[2], hit[3]
    files, eps = sonarr("GET", f"/episodefile?seriesId={s['id']}"), sonarr("GET", f"/episode?seriesId={s['id']}")
    _SHOW_FILES[s["id"]] = (stamp, time.time(), files, eps)
    return files, eps


def hub_cards(with_files=True):
    """Everything added to Sonarr/Radarr, with live status (and, for My Shows, the downloaded episode files)."""
    jobs = {"series": (sonarr, "GET", "/series"), "queue": (sonarr, "GET", "/queue?pageSize=1000&includeEpisode=true"),
            "commands": (sonarr, "GET", "/command"), "movies": (radarr, "GET", "/movie"), "mqueue": (radarr, "GET", "/queue?pageSize=1000"),
            "mcommands": (radarr, "GET", "/command"), "live": (qbit_live,)}
    got = {k: f.result() for k, f in {k: FETCH.submit(*job) for k, job in jobs.items()}.items()}
    series, queue, movies, mqueue = got["series"], got["queue"]["records"], got["movies"], got["mqueue"]["records"]
    commands = [c for c in got["commands"] if c["status"] in ("queued", "started") and c["name"] in SEARCH_COMMANDS]
    mcommands = [c for c in got["mcommands"] if c["status"] in ("queued", "started") and c["name"] in SEARCH_COMMANDS]
    live = {t["hash"].upper(): t for t in got["live"]}
    per_show = {s["id"]: FETCH.submit(show_files, s) for s in series if with_files and (s.get("statistics") or {}).get("episodeFileCount")}
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
        if s["id"] in per_show:
            efiles, elist = per_show[s["id"]].result()
            files = {f["id"]: f["path"] for f in efiles}
            for e in elist:
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
