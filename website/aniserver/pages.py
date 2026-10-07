"""HTML building blocks: the page frame, cards, rows, icons, artwork URLs, and the hashed CSS/JS assets."""
import gzip
import hashlib
import html
import threading
import time
from collections import OrderedDict
from pathlib import Path

from aniserver.config import CONF, PROFILES
from aniserver.anime import clean_description, title_of

IMG_WIDTHS = (160, 240, 360, 480, 640, 960, 1280, 1920)


_IMG_CACHE = OrderedDict()
_IMG_LOCK = threading.Lock()
IMG_CACHE_BYTES = 96 * 1024 * 1024


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


ASSET_DIR = Path(__file__).resolve().parent.parent / "assets"          # the site's own CSS and JavaScript
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
