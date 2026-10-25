"""Anime artwork language: "japanese" (official key visual + Japanese title logo) or "english", set on the Account page
(config.json "anime_artwork"). Applied to Jellyfin (the website and the apps) and Kodi (the TV) for every title whose
original language is Japanese; switching back and forth re-applies it."""
import base64
import json
import time
import urllib.parse
import urllib.request
from pathlib import Path

from aniserver.config import CONF, CONFIG_FILE, JELLYFIN, UA
from aniserver.services import graphql, jellyfin, kodi_rpc, radarr, sonarr

ARTWORK_STATE = Path.home() / ".cache/htpc/artwork.json"      # title -> the artwork applied (and in which language)
ARTWORK_LANGS = {"japanese": "ja", "english": "en"}


def artwork_language():
    value = CONF.get("anime_artwork", "japanese")
    return value if value in ARTWORK_LANGS else "japanese"


def set_artwork_language(value):
    if value not in ARTWORK_LANGS:
        raise ValueError(value)
    data = json.loads(CONFIG_FILE.read_text())
    data["anime_artwork"] = CONF["anime_artwork"] = value
    tmp = CONFIG_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    tmp.chmod(0o600)
    tmp.replace(CONFIG_FILE)


def pick_image(item, kind, lang, title):
    """Most-voted image of this kind ("Primary" poster / "Logo") in the language; for Japanese posters, AniList's cover
    (the Japanese key visual) when TMDB/TVDB have none."""
    imgs = jellyfin(f"/Items/{item['Id']}/RemoteImages?type={kind}&includeAllLanguages=true").get("Images") or []
    best = sorted((x for x in imgs if x.get("Language") == lang), key=lambda x: (-(x.get("VoteCount") or 0), -(x.get("CommunityRating") or 0)))
    if best:
        return best[0]["Url"], best[0]["ProviderName"]
    if kind == "Primary" and lang == "ja":
        media = graphql("query($s:String){Media(search:$s,type:ANIME){coverImage{extraLarge}}}", {"s": title}).get("Media") or {}
        url = (media.get("coverImage") or {}).get("extraLarge")
        if url:
            return url, "AniList"
    return None, None


def set_image(item_id, kind, url, provider):
    if provider in ("TheMovieDb", "TheTVDB", "Fanart"):
        jellyfin(f"/Items/{item_id}/RemoteImages/Download?" + urllib.parse.urlencode({"type": kind, "imageUrl": url, "providerName": provider}), "POST")
        return
    data = urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=30).read()   # not a Jellyfin provider: upload it
    req = urllib.request.Request(f"{JELLYFIN}/Items/{item_id}/Images/{kind}", data=base64.b64encode(data), method="POST",
                                 headers={"Content-Type": "image/jpeg", "Authorization": f'MediaBrowser Token="{CONF["jellyfin_key"]}"'})
    urllib.request.urlopen(req, timeout=30).read()


def apply_anime_artwork(dry=False, log=print):
    """Give every Japanese-original show/movie the poster and title logo in the chosen language, in Jellyfin and Kodi."""
    lang = ARTWORK_LANGS[artwork_language()]
    try:
        state = json.loads(ARTWORK_STATE.read_text())
    except (OSError, ValueError):
        state = {}
    titles = {f"s{s['tvdbId']}": s["title"] for s in sonarr("GET", "/series") if (s.get("originalLanguage") or {}).get("name") == "Japanese"}
    titles.update({f"m{m['tmdbId']}": m["title"] for m in radarr("GET", "/movie") if (m.get("originalLanguage") or {}).get("name") == "Japanese"})
    for item in jellyfin("/Items?Recursive=true&IncludeItemTypes=Series,Movie&Fields=ProviderIds")["Items"]:
        ids = item.get("ProviderIds") or {}
        key = f"s{ids.get('Tvdb')}" if item["Type"] == "Series" else f"m{ids.get('Tmdb')}"
        if key not in titles or (state.get(key) or {}).get("lang") == lang:
            continue
        rec = {"lang": lang, "at": int(time.time())}
        for kind in ("Primary", "Logo"):
            url, provider = pick_image(item, kind, lang, titles[key])
            if url:
                log(f"{item['Name']}: {'Japanese' if lang == 'ja' else 'English'} {'poster' if kind == 'Primary' else 'logo'} ({provider})")
                if not dry:
                    set_image(item["Id"], kind, url, provider)
                rec[kind] = url
        state[key] = rec
    try:                                                # the TV: Kodi keeps its own artwork
        shows = kodi_rpc("VideoLibrary.GetTVShows", {"properties": ["uniqueid", "art"]}).get("result", {}).get("tvshows", [])
        movies = kodi_rpc("VideoLibrary.GetMovies", {"properties": ["uniqueid", "art"]}).get("result", {}).get("movies", [])
    except Exception:
        shows = movies = []
    for kind, items, method, idkey in (("s", shows, "VideoLibrary.SetTVShowDetails", "tvshowid"), ("m", movies, "VideoLibrary.SetMovieDetails", "movieid")):
        for t in items:
            rec = state.get(kind + str((t.get("uniqueid") or {}).get("tvdb" if kind == "s" else "tmdb"))) or {}
            have = urllib.parse.unquote(" ".join((t.get("art") or {}).get(a, "") for a in ("poster", "clearlogo")))
            art = {a: rec[k] for a, k in (("poster", "Primary"), ("clearlogo", "Logo")) if rec.get(k) and rec[k] not in have}
            if art:
                log(f"{t['label']}: artwork on the TV")
                if not dry:
                    kodi_rpc(method, {idkey: t[idkey], "art": art})
    if not dry:
        ARTWORK_STATE.parent.mkdir(parents=True, exist_ok=True)
        ARTWORK_STATE.write_text(json.dumps(state))
