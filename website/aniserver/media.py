"""Regular movies and TV (everything that isn't anime): found with Radarr's and Sonarr's own lookup (TMDB/TVDB),
Radarr's discover lists for what's trending/popular (no TMDB key needed), and added with the regular profiles and
folders. Anime found here is pointed to the Anime page, which knows its seasons and specials."""
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

from aniserver.anime import drive_root
from aniserver.config import CONF, FILM_FOLDERS, TV, TV_FOLDERS
from aniserver.search import search_later
from aniserver.services import radarr, sonarr, swr

FILM_PROFILE = CONF.get("film_profile", "Movies 1080p")       # TRaSH HD Bluray + WEB (via Recyclarr)
SHOW_PROFILE = CONF.get("show_profile", "TV 1080p")           # TRaSH WEB-1080p + Blu-ray 1080p (via Recyclarr)
WATCH_SEEDERS = 15        # "Add & watch now" wants a well-seeded release, so playback doesn't outrun the download
QUEUE_WAIT = 60           # seconds for a grabbed release to show up in Radarr's queue
TITLE_RESULTS = 12              # per kind (movies, shows) in search results


def is_anime_title(x):
    """Japanese animation: those belong on the Anime page (seasons, specials, the anime profile)."""
    genres = set(x.get("genres") or [])
    return (x.get("originalLanguage") or {}).get("name") == "Japanese" and bool(genres & {"Animation", "Anime"})


def art_url(x, kind, width):
    url = next((i.get("remoteUrl") or i.get("url") for i in x.get("images") or [] if i.get("coverType") == kind), "") or ""
    if kind == "poster" and not url:
        url = x.get("remotePoster") or ""
    return url.replace("/t/p/original/", f"/t/p/{width}/") if url.startswith("http") else ""


def movie_card(x, library):
    have = library.get(x.get("tmdbId"))
    released = bool(x.get("digitalRelease") or x.get("physicalRelease")) and min(
        d for d in (x.get("digitalRelease"), x.get("physicalRelease")) if d) <= time.strftime("%Y-%m-%dT%H:%M:%SZ")
    rating = (x.get("ratings") or {}).get("tmdb") or (x.get("ratings") or {}).get("imdb") or {}
    return {"kind": "movie", "id": x.get("tmdbId"), "title": x.get("title") or "", "year": x.get("year") or "",
            "poster": art_url(x, "poster", "w342"), "cover": art_url(x, "poster", "w500"), "fanart": art_url(x, "fanart", "w1280"),
            "overview": x.get("overview") or "", "genres": (x.get("genres") or [])[:4], "runtime": x.get("runtime") or 0,
            "rating": rating.get("value") or 0, "cert": x.get("certification") or "", "by": x.get("studio") or "",
            "anime": is_anime_title(x), "released": released or x.get("status") == "released",
            "state": ("have" if have["hasFile"] else "added") if have else "new"}


def show_card(x, library):
    have = library.get(x.get("tvdbId"))
    seasons = [s for s in x.get("seasons") or [] if s["seasonNumber"] > 0]
    rating = (x.get("ratings") or {}).get("value") or 0
    stats = (have or {}).get("statistics") or {}
    return {"kind": "show", "id": x.get("tvdbId"), "title": x.get("title") or "", "year": x.get("year") or "",
            "poster": art_url(x, "poster", "w342"), "cover": art_url(x, "poster", "w500"), "fanart": art_url(x, "fanart", "w1280"),
            "overview": x.get("overview") or "", "genres": (x.get("genres") or [])[:4], "seasons": len(seasons),
            "rating": rating, "cert": x.get("certification") or "", "by": x.get("network") or "",
            "status": (x.get("status") or "").title(), "anime": is_anime_title(x) or x.get("seriesType") == "anime",
            "got": f'{stats.get("episodeFileCount", 0)}/{stats.get("episodeCount", 0)} episodes downloaded' if have else "",
            "state": ("have" if stats.get("episodeFileCount") else "added") if have else "new"}


def libraries():
    return ({m["tmdbId"]: m for m in radarr("GET", "/movie")}, {s["tvdbId"]: s for s in sonarr("GET", "/series")})


def find_titles(query):
    """(movie cards, show cards) for a title, best matches first."""
    term = urllib.parse.quote(query)
    with ThreadPoolExecutor(4) as ex:
        movies = ex.submit(radarr, "GET", "/movie/lookup?term=" + term)
        shows = ex.submit(sonarr, "GET", "/series/lookup?term=" + term)
        lib = ex.submit(libraries)
        films, series = lib.result()
        return ([movie_card(x, films) for x in (movies.result() or [])[:TITLE_RESULTS] if x.get("tmdbId")],
                [show_card(x, series) for x in (shows.result() or [])[:TITLE_RESULTS] if x.get("tvdbId")])


def discover():
    """Radarr's discover lists: {"trending": [...], "popular": [...], "because": [...]} (recommendations from the movies
    already here). Movies already in the library are left out."""
    raw = radarr("GET", "/importlist/movie?includeRecommendations=true&includeTrending=true&includePopular=true",
                 timeout=60) or []
    films, _ = libraries()
    out = {"trending": [], "popular": [], "because": []}
    for x in raw:
        if not x.get("tmdbId") or x["tmdbId"] in films or is_anime_title(x):
            continue
        c = movie_card(x, films)
        if x.get("isTrending"):
            out["trending"].append(c)
        elif x.get("isPopular"):
            out["popular"].append(c)
        elif x.get("isRecommendation"):
            out["because"].append(c)
    return out


def discover_cached():
    return swr("discover", 1800, discover, 86400)


def add_movie(tmdb_id, user, watch=False):
    """Add a movie with the regular profile. watch=True picks a well-seeded release and starts it on the TV while it
    downloads (in the background: the release search takes a little while). Returns (title, message)."""
    existing = next((x for x in radarr("GET", "/movie") if x["tmdbId"] == tmdb_id), None)
    if existing:
        return existing["title"], "It's already in the library." + (" Play it from Home." if existing.get("hasFile") else "")
    mv = radarr("GET", f"/movie/lookup/tmdb?tmdbId={tmdb_id}")
    profile = next(p["id"] for p in radarr("GET", "/qualityprofile") if p["name"] == FILM_PROFILE)
    root, tags = drive_root(FILM_FOLDERS, radarr)
    watch = watch and TV
    mv.update(qualityProfileId=profile, rootFolderPath=root, monitored=True, minimumAvailability="released",
              tags=tags, addOptions={"searchForMovie": not watch, "monitor": "movieOnly"})
    added = radarr("POST", "/movie", mv)
    if watch:
        threading.Thread(target=watch_while_downloading, args=(added["id"], user), daemon=True).start()
        return added["title"], "Added. Finding a well-seeded copy; it starts on the TV in a minute or two, while it downloads."
    return added["title"], "Added. Radarr is downloading it now in the best quality it finds."


def pick_for_watching(releases):
    """The best release Radarr accepts that has enough seeders to stay ahead of playback (else the best-seeded one).
    Radarr lists them best first."""
    ok = [r for r in releases if r.get("approved") and r.get("protocol") == "torrent"]
    return next((r for r in ok if (r.get("seeders") or 0) >= WATCH_SEEDERS), None) or max(
        ok, key=lambda r: r.get("seeders") or 0, default=None)


def watch_while_downloading(movie_id, user, log=print):
    from aniserver.streaming import watch_now       # the TV side; imported here so this module loads without it
    try:
        rel = pick_for_watching(radarr("GET", f"/release?movieId={movie_id}", timeout=180) or [])
        if not rel:
            log(f"watch now: no acceptable release for movie {movie_id}; Radarr keeps looking")
            radarr("POST", "/command", {"name": "MoviesSearch", "movieIds": [movie_id]})
            return
        radarr("POST", "/release", {"guid": rel["guid"], "indexerId": rel["indexerId"]})
        deadline = time.time() + QUEUE_WAIT
        while time.time() < deadline:
            q = [x for x in radarr("GET", f"/queue?movieIds={movie_id}")["records"] if x.get("downloadId")]
            if q:
                watch_now(q[0]["downloadId"], user)
                log(f"watch now: {rel['title']}")
                return
            time.sleep(3)
        log(f"watch now: {rel['title']} didn't show up in the download queue")
    except Exception as e:
        log(f"watch now failed for movie {movie_id}: {e}")


def add_show(tvdb_id, mode):
    """mode: 'all' (every season), 'latest' (just the newest season, and what comes next), 'future' (only new episodes).
    Returns (title, message)."""
    existing = next((s for s in sonarr("GET", "/series") if s["tvdbId"] == tvdb_id), None)
    if existing:
        return existing["title"], "It's already in My Shows."
    s = sonarr("GET", f"/series/lookup?term=tvdb:{tvdb_id}")[0]
    profile = next(p["id"] for p in sonarr("GET", "/qualityprofile") if p["name"] == SHOW_PROFILE)
    root, tags = drive_root(TV_FOLDERS, sonarr)
    latest = max((x["seasonNumber"] for x in s["seasons"] if x["seasonNumber"] > 0), default=None)
    for x in s["seasons"]:
        x["monitored"] = x["seasonNumber"] > 0 and (mode == "all" or (mode == "latest" and x["seasonNumber"] == latest))
    monitor = {"all": "all", "latest": "latestSeason", "future": "future"}[mode]
    s.update(qualityProfileId=profile, seriesType="standard", rootFolderPath=root, monitored=True, seasonFolder=True,
             tags=tags, monitorNewItems="all", addOptions={"monitor": monitor, "searchForMissingEpisodes": False})
    added = sonarr("POST", "/series", s)
    if mode == "future":
        return added["title"], "Added. New episodes will download as they air."
    search_later(added["id"])                           # whole seasons first (aniserver/search.py)
    what = "every season" if mode == "all" else f"season {latest}"
    return added["title"], f"Added. Downloading {what} now, whole seasons at a time where it can."
