"""Searching for episodes the fast way. Sonarr's own anime search queries every episode separately with every title it
knows (~40 s an episode: 16 minutes for Orb's 25) and decides episode by episode, so it grabs single episodes even when a
whole-season batch exists. Here one Prowlarr query per title finds the batches, Sonarr's parser scores them, and the
best is pushed to Sonarr, which still makes the final call (quality profile, blocklist, what's already on disk).
Seasons without an acceptable batch get Sonarr's normal season search; a few episodes get an episode search."""
import re
import threading
import time
import urllib.parse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor

from aniserver.services import prowlarr, sonarr

BATCH_MIN = 3            # a season with at least this many episodes to fetch tries a batch first
MAX_PUSHES = 4           # batches offered to Sonarr per season before falling back to its own search
ANIME_CATEGORY = 5070


def series_titles(series, limit=3):
    """The show's title and its best-known alternates (romaji first, as batch groups name them), without duplicates."""
    seen, out = set(), []
    for t in [series["title"], *(a["title"] for a in series.get("alternateTitles") or [])]:
        key = re.sub(r"[^a-z0-9]+", "", t.lower())
        if key and key not in seen:
            seen.add(key)
            out.append(t)
    return out[:limit]


def find_releases(titles):
    """Torrents Prowlarr finds for any of the titles (all anime indexers at once), one entry per torrent."""
    def one(title):
        try:
            return prowlarr("GET", "/search?" + urllib.parse.urlencode(
                {"query": title, "type": "search", "categories": ANIME_CATEGORY, "limit": 100}), timeout=60) or []
        except Exception:
            return []
    found = {}
    with ThreadPoolExecutor(max(1, len(titles))) as ex:
        for results in ex.map(one, titles):
            for r in results:
                if r.get("protocol") == "torrent" and (r.get("seeders") or 0) > 0:
                    found.setdefault(r.get("infoHash") or r["guid"], r)
    return list(found.values())


def quality_rank(profile):
    """{quality id: position in the profile} for allowed qualities (later = better; a group's members share one)."""
    rank = {}
    for i, item in enumerate(profile["items"]):
        if item.get("allowed"):
            for q in [item["quality"]] if item.get("quality") else [x["quality"] for x in item.get("items", [])]:
                rank[q["id"]] = i
    return rank


def batch_candidates(series, wanted, releases, max_episode_gb=None):
    """[(release, seasons it covers)] for multi-episode releases of this show that hold every wanted episode of at least
    one season, best first the way Sonarr ranks them: quality (the profile's order), format score, then seeders.
    max_episode_gb leaves out batches bigger than that per episode."""
    profile = sonarr("GET", f"/qualityprofile/{series['qualityProfileId']}")
    rank = quality_rank(profile)

    def parse(r):
        try:
            return r, sonarr("GET", "/parse?title=" + urllib.parse.quote(r["title"]))
        except Exception:
            return r, None
    out = []
    with ThreadPoolExecutor(8) as ex:
        for r, p in ex.map(parse, releases):
            if not p or (p.get("series") or {}).get("id") != series["id"]:
                continue
            ids = {e["id"] for e in p.get("episodes") or []}
            covers = sorted(n for n, want in wanted.items() if want <= ids)
            q = (((p.get("parsedEpisodeInfo") or {}).get("quality") or {}).get("quality") or {}).get("id")
            score = p.get("customFormatScore") or 0
            too_big = max_episode_gb and (r.get("size") or 0) > max_episode_gb * 1e9 * len(ids)
            if len(ids) > 1 and covers and q in rank and score >= profile.get("minFormatScore", 0) and not too_big:
                out.append((rank[q], score, r.get("seeders") or 0, r, covers))
    out.sort(key=lambda x: x[:3], reverse=True)
    return [(r, covers) for *_, r, covers in out]


def push(release, indexer_name):
    """Offer one release to Sonarr; it downloads it if its rules accept it. Returns (accepted, reasons if not)."""
    link = release.get("downloadUrl") or release.get("magnetUrl")
    body = {"title": release["title"], "downloadUrl": link, "protocol": "torrent", "publishDate": release["publishDate"],
            "size": release.get("size"), "seeders": release.get("seeders"), "leechers": release.get("leechers"),
            "infoUrl": release.get("infoUrl"), "indexer": indexer_name}
    if (release.get("magnetUrl") or "").startswith("magnet:"):
        body["magnetUrl"] = release["magnetUrl"]
    res = sonarr("POST", "/release/push", body)
    decision = (res[0] if isinstance(res, list) and res else res) or {}
    return bool(decision.get("approved")), [str(x) for x in decision.get("rejections") or []]


def grab_batches(series, wanted, done, log):
    """Push the best batches until every wanted season is covered. Returns the seasons now downloading as batches."""
    indexers = {i["name"].removesuffix(" (Prowlarr)"): i["name"] for i in sonarr("GET", "/indexer")
                if i.get("enableAutomaticSearch", True)}
    releases = [r for r in find_releases(series_titles(series)) if r.get("indexer") in indexers]
    covered, tries = set(), Counter()
    for r, seasons in batch_candidates(series, wanted, releases):
        left = [n for n in seasons if n not in covered]
        if not left or all(tries[n] >= MAX_PUSHES for n in left):
            continue
        tries.update(left)
        ok, why = push(r, indexers[r["indexer"]])
        if ok:
            covered |= set(seasons)
            done["batches"].append(r["title"])
            log(f"{series['title']}: batch {r['title']}")
        else:
            log(f"{series['title']}: Sonarr turned down {r['title']} ({'; '.join(why)[:160]})")
        if covered >= set(wanted):
            break
    return covered


def search_episodes(series_id, episode_ids, log=lambda msg: None):
    """Search for these episodes: batches first for seasons with several of them, then Sonarr's own searches for the
    rest. Returns what it did: {"batches": [titles], "season_searches": [seasons], "episode_search": [episode ids]}."""
    done = {"batches": [], "season_searches": [], "episode_search": []}
    ids = set(episode_ids)
    if not ids:
        return done
    by_season = defaultdict(set)
    for e in sonarr("GET", f"/episode?seriesId={series_id}"):
        if e["id"] in ids:
            by_season[e["seasonNumber"]].add(e["id"])
    big = {n: want for n, want in by_season.items() if n > 0 and len(want) >= BATCH_MIN}
    covered = set()
    if big:
        try:
            covered = grab_batches(sonarr("GET", f"/series/{series_id}"), big, done, log)
        except Exception as e:                          # Prowlarr down etc.: Sonarr's own search still works
            log(f"batch search failed ({e}); using Sonarr's season search")
    for n, want in sorted(by_season.items()):
        if n in covered:
            continue
        if n in big:
            sonarr("POST", "/command", {"name": "SeasonSearch", "seriesId": series_id, "seasonNumber": n})
            done["season_searches"].append(n)
        else:
            done["episode_search"] += sorted(want)
    if done["episode_search"]:
        sonarr("POST", "/command", {"name": "EpisodeSearch", "episodeIds": done["episode_search"]})
    return done


def wanted_episodes(series_id, wait=30):
    """Monitored episodes that have aired and have no file (waits for a just-added show's episode list)."""
    eps = []
    for _ in range(wait):
        eps = sonarr("GET", f"/episode?seriesId={series_id}")
        if eps:
            break
        time.sleep(1)
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return [e["id"] for e in eps if e["monitored"] and not e.get("hasFile") and e.get("airDateUtc") and e["airDateUtc"] <= now]


def search_later(series_id, episode_ids=None):
    """search_episodes in the background (the website answers right away); None = everything the show is missing."""
    job = lambda: search_episodes(series_id, wanted_episodes(series_id) if episode_ids is None else episode_ids)
    threading.Thread(target=job, daemon=True).start()
