"""Anime artwork language: "japanese" (official key visual + Japanese title logo) or "english". Everyone picks their
own on the Account page for this website; the owner's pick is also what Jellyfin (its apps) and Kodi (the TV) show,
since those are shared. Applies to every title whose original language is Japanese; switching re-applies it."""
import base64
import json
import time
import urllib.parse
import urllib.request
from pathlib import Path

from aniserver.config import CONF, CONFIG_FILE, JELLYFIN, OWNER, UA
from aniserver.services import graphql, jellyfin, kodi_rpc, radarr, sonarr

ARTWORK_STATE = Path.home() / ".cache/htpc/artwork.json"      # title -> both languages' images, the one Jellyfin/Kodi show
ARTWORK_CACHE = Path.home() / ".cache/htpc/artwork"           # <jellyfin id>-<kind>-<ja|en>.<ext>, for people whose pick differs
ARTWORK_PREFS = CONFIG_FILE.parent / "artwork-prefs.json"      # person -> japanese|english (this website)
ARTWORK_LANGS = {"japanese": "ja", "english": "en"}


def artwork_language():
    """The shared pick (Jellyfin apps, the TV): the owner's."""
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


def _prefs():
    try:
        return json.loads(ARTWORK_PREFS.read_text())
    except (OSError, ValueError):
        return {}


def user_artwork(user):
    """This person's pick on the website (the shared one until they choose)."""
    if user == OWNER:
        return artwork_language()
    pick = _prefs().get(user)
    return pick if pick in ARTWORK_LANGS else artwork_language()


def set_user_artwork(user, value):
    """Save someone's pick. The owner's is also the shared one: returns True when Jellyfin/the TV need re-applying."""
    if value not in ARTWORK_LANGS:
        raise ValueError(value)
    if user == OWNER:
        set_artwork_language(value)
        return True
    prefs = _prefs()
    prefs[user] = value
    tmp = ARTWORK_PREFS.with_suffix(".tmp")
    tmp.write_text(json.dumps(prefs, indent=2) + "\n")
    tmp.replace(ARTWORK_PREFS)
    return False


_ROMAJI = {"mtime": None, "jf": {}, "key": {}}


def romaji_title(name, jf_id=None, key=None, pick="english"):
    """The romaji title (AniList's) for people who picked Japanese artwork, else `name`. Looked up by Jellyfin id
    (series/movie) or by Sonarr/Radarr key ('s<tvdb>' / 'm<tmdb>'); anything unknown keeps its name."""
    if pick != "japanese":
        return name
    try:
        mtime = ARTWORK_STATE.stat().st_mtime
        if mtime != _ROMAJI["mtime"]:
            state = json.loads(ARTWORK_STATE.read_text())
            _ROMAJI.update(mtime=mtime, jf={r["jf"]: r["romaji"] for r in state.values() if r.get("jf") and r.get("romaji")},
                           key={k: r["romaji"] for k, r in state.items() if r.get("romaji")})
    except (OSError, ValueError):
        return name
    return _ROMAJI["jf"].get(jf_id) or _ROMAJI["key"].get(key) or name


def romaji_lookup(keys):
    """{'s<tvdb>'/'m<tmdb>': AniList romaji title}: a show's first season (or the movie) on AniList."""
    from aniserver.anime import anilist_to_tmdb, anilist_to_tvdb
    pick = {}
    for aid, (tvdb, season) in anilist_to_tvdb().items():
        k = f"s{tvdb}"
        if k in keys and (k not in pick or ((season or 99), aid) < pick[k][0]):
            pick[k] = (((season or 99), aid), aid)
    for aid, tmdb in anilist_to_tmdb().items():
        k = f"m{tmdb}"
        if k in keys and k not in pick:
            pick[k] = ((0, aid), aid)
    ids = {aid: k for k, (_, aid) in pick.items()}
    out, todo = {}, list(ids)
    for n in range(0, len(todo), 25):
        q = "query{" + " ".join(f"a{a}: Media(id:{a}){{id title{{romaji}}}}" for a in todo[n:n + 25]) + "}"
        try:
            for m in graphql(q).values():
                if m and m["title"].get("romaji"):
                    out[ids[m["id"]]] = m["title"]["romaji"]
        except Exception:
            pass
        time.sleep(1)
    return out


def cached_image(item_id, kind, value):
    """The local copy of a title's poster/logo in `value`'s language, if there is one."""
    code = ARTWORK_LANGS.get(value)
    return next(iter(ARTWORK_CACHE.glob(f"{item_id}-{kind}-{code}.*")), None) if code else None


def sized(url, kind):
    """TMDB serves smaller copies: cards don't need the 2000x3000 original."""
    return url.replace("/t/p/original/", "/t/p/w780/" if kind == "Primary" else "/t/p/w500/")


def cache_image(item_id, kind, code, url):
    ext = Path(urllib.parse.urlsplit(url).path).suffix.lower()
    ext = ext if ext in (".jpg", ".jpeg", ".png", ".webp") else ".jpg"
    data = urllib.request.urlopen(urllib.request.Request(sized(url, kind), headers={"User-Agent": UA}), timeout=30).read()
    ARTWORK_CACHE.mkdir(parents=True, exist_ok=True)
    tmp = ARTWORK_CACHE / f".{item_id}-{kind}-{code}{ext}"
    tmp.write_bytes(data)
    tmp.replace(ARTWORK_CACHE / f"{item_id}-{kind}-{code}{ext}")


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
    """Look up every Japanese-original show/movie's poster and title logo in both languages (kept locally for the
    website), and give Jellyfin and Kodi the shared pick."""
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
        if key not in titles:
            continue
        rec = state.get(key) or {}
        same_item = rec.get("jf") in (None, item["Id"])       # Jellyfin gives a re-added title a new id
        if "urls" not in rec or not same_item:
            urls = {}
            for code in ARTWORK_LANGS.values():
                urls[code] = {}
                for kind in ("Primary", "Logo"):
                    url, provider = pick_image(item, kind, code, titles[key])
                    if url:
                        urls[code][kind] = [url, provider]
            rec = {"lang": rec.get("lang") if same_item else None, "urls": urls}
        rec.update(jf=item["Id"], at=int(time.time()))
        if rec.get("lang") != lang:
            for kind, (url, provider) in rec["urls"][lang].items():
                log(f"{item['Name']}: {'Japanese' if lang == 'ja' else 'English'} {'poster' if kind == 'Primary' else 'logo'} ({provider})")
                if not dry:
                    set_image(item["Id"], kind, url, provider)
            rec["lang"] = lang
        for kind in ("Primary", "Logo"):                       # what the TV shows
            rec[kind] = (rec["urls"][lang].get(kind) or [None])[0]
        if not dry:                                           # both languages kept locally for the website
            for code, kinds in rec["urls"].items():
                for kind, (url, _) in kinds.items():
                    if not next(ARTWORK_CACHE.glob(f"{item['Id']}-{kind}-{code}.*"), None):
                        try:
                            cache_image(item["Id"], kind, code, url)
                        except Exception:
                            pass
        state[key] = rec
    try:                                                # the TV: Kodi keeps its own artwork
        shows = kodi_rpc("VideoLibrary.GetTVShows", {"properties": ["uniqueid", "art"]}).get("result", {}).get("tvshows", [])
        movies = kodi_rpc("VideoLibrary.GetMovies", {"properties": ["uniqueid", "art"]}).get("result", {}).get("movies", [])
    except Exception:
        shows = movies = []
    missing = {k for k, r in state.items() if "romaji" not in r}     # romaji titles for people who picked Japanese
    if missing and not dry:
        found = romaji_lookup(missing)
        for k in missing:
            state[k]["romaji"] = found.get(k, "")
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
