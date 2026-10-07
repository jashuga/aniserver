"""Manga: Nyaa search via Prowlarr, sending volumes to qBittorrent, following series with RSS rules."""
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from aniserver.config import CONF, RSS_FOLDER
from aniserver.services import prowlarr, qbit

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
