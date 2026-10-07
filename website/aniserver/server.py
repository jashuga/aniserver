"""The web server: every route (Handler), background cache warming, and main()."""
import base64
import calendar
import gzip
import hashlib
import html
import json
import os
import re
import secrets
import shutil
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from aniserver.config import CONF, JELLYFIN, OWNER, PROFILES, REMOTE_NAMES, STATIC
from aniserver.services import POOL, _JF_CACHE, home_public_ip, jellyfin, jf_item, jf_maps, jf_uid, swr
from aniserver.auth import PENDING, REMOTE_COOKIE, REMOTE_SESSION_DAYS, TRUST_COOKIE, change_jellyfin_password, change_website_password, check_password, cookie_flags, hash_password, note_login, record_fail, remote_token, remote_token_user, remote_users, save_remote_users, session_cookies, too_many_fails, totp_ok
from aniserver.anime import anime_add, anime_search, movie_add, title_of, trending, trending_cached
from aniserver.manga import follow, follows, grab, parse_release, search, unfollow
from aniserver.library import _download_names, _downloads_by_key, continue_watching, delete_title, download_progress, downloads_by_key, hub_cards, human_eta, kodi_play, latest_episodes, library, live_downloads, media_path, next_up, nextup_items, play_target, provider_key, resume_items, series_episodes, signature_ok, upcoming_episodes
from aniserver.streaming import HEX32, LADDER, LADDER_HEVC, PLAYER_LOG, RATES_CACHE, REMOTE_MAX_BITRATE, STREAM_PATH, _FILE_PATHS, _FONT_NAMES, _RATES, abr_master, bitrate_profile, item_fonts, jellyfin_master_query, mpv_progress, playinfo, save_progress, set_played, stop_transcode, stream_token, stream_token_ok, tokenize_playlist, tv_play_item
from aniserver.pages import ASSET_FILES, ICON_TAGS, IMG_CACHE_BYTES, IMG_WIDTHS, PLAY_OVERLAY, SEARCH_ICON, _IMG_CACHE, _IMG_LOCK, asset, backdrop_of, delete_button, details_html, ecard, ep_code, esc, fmt_runtime, fmt_size, icon, jump_icon, logo_of, meta_bits, meta_html, msg_html, page, pcard, poster_html, poster_of_item, progress_pct, row_html, thumb_of, time_left, tv_button, year_span

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
        for key, ttl, fn in (("trending", 900, trending), ("dlcards", 15, _downloads_by_key), ("dlnames", 10, _download_names)):
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


def main():
    warm_caches()
    ThreadingHTTPServer(("0.0.0.0", CONF.get("port", 5050)), Handler).serve_forever()
