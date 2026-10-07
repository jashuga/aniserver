"""Adding anime and movies: AniList search/trending, AniList -> TVDB/TMDB mapping, matching seasons to TVDB episodes by air date."""
import calendar
import html
import json
import os
import re
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from aniserver.config import INTERNAL_ROOT, MOVIE_ROOT_INTERNAL, MOVIE_ROOT_SSD, UA
from aniserver.services import graphql, qbit, radarr, sonarr, swr

MAPPING_URL = "https://raw.githubusercontent.com/Fribb/anime-lists/master/anime-list-full.json"
MAPPING_FILE = Path.home() / ".cache/htpc/anime-list-full.json"
ANIME_PROFILE = "[Anime] Remux-1080p"
_MAPPING = {"mtime": 0, "data": {}, "tmdb": {}}
_TRENDING = {"t": 0, "data": []}
INTERNAL_RESERVE, SSD_RESERVE = 100e9, 20e9   # same drive rule as the Sonarr add hook

MEDIA_FIELDS = """id format status episodes duration season seasonYear startDate{year month day} endDate{year month day}
  title{romaji english} synonyms coverImage{large extraLarge color} bannerImage description(asHtml:false)
  genres averageScore studios(isMain:true){nodes{name}} nextAiringEpisode{episode airingAt} siteUrl"""
SEARCH_QUERY = "query($s:String){Page(perPage:30){media(search:$s,type:ANIME,sort:SEARCH_MATCH,isAdult:false){" + MEDIA_FIELDS + "}}}"
TRENDING_QUERY = ("query{Page(perPage:24){media(type:ANIME,status:RELEASING,sort:TRENDING_DESC,isAdult:false,"
                  "format_in:[TV,TV_SHORT,ONA]){" + MEDIA_FIELDS + "}}}")


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


def trending_cached():
    return swr("trending", 900, trending, 86400)
