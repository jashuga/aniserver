"""Playback: tracks and subtitles, skip markers, HLS playlists and the adaptive ladder, fonts, bitrates, progress, the TV."""
import hashlib
import hmac
import json
import re
import struct
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from aniserver.config import CONF, JELLYFIN, TV_PROFILE
from aniserver.services import POOL, _JF_CACHE, is_anime, jellyfin, jf_item, jf_maps, jf_uid
from aniserver.library import kodi_play, media_path, series_episodes
from aniserver.pages import backdrop_of, ep_code, logo_of, thumb_of

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
