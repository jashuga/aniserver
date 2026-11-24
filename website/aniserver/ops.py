"""The owner's Server page: whether every service answers (with a restart), the media drives (mounted, space left),
downloads that stopped moving (replaced with another release), the watchdog's recent alerts, and a Jellyfin
password reset / Quick Connect approval that doesn't need the old password (the site sign-in already proved who it is)."""
import os
import re
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from aniserver.config import ANIME_FOLDERS, CONF, FILM_FOLDERS, HOME, JELLYFIN, MOVIE_FOLDERS, TV, TV_FOLDERS
from aniserver.services import jellyfin, kodi_rpc, prowlarr, qbit, radarr, sonarr

USER = os.environ.get("USER") or HOME.name
# name, health probe, unit, whether it's a system unit (restarted with sudo, passwordless on this machine)
OPS_SERVICES = [
    ("Sonarr", lambda: sonarr("GET", "/system/status", timeout=10), "sonarr", True),
    ("Radarr", lambda: radarr("GET", "/system/status", timeout=10), "radarr", False),
    ("Prowlarr", lambda: prowlarr("GET", "/system/status", timeout=10), "prowlarr", True),
    ("qBittorrent", lambda: qbit("/app/version", timeout=10), f"qbittorrent-nox@{USER}", True),
    ("Jellyfin", lambda: jellyfin("/System/Info/Public"), "jellyfin", False),
] + ([("TV (Kodi)", lambda: kodi_rpc("JSONRPC.Ping"), "kodi", False)] if TV else [])
sleep = time.sleep
STUCK_HOURS = 6           # a download that hasn't received anything for this long counts as stuck
WATCHDOG_LOG = HOME / ".local/share/htpc-web/watchdog.log"
WATCHDOG_STATE = HOME / ".local/share/htpc-web/watchdog.json"
RECYCLE = Path(CONF.get("recycle_folder") or HOME / "Media/.recycle")


def service_status():
    """[(name, ok, detail)], probed in parallel."""
    def probe(svc):
        name, check, unit, system = svc
        try:
            check()
            return name, True, ""
        except Exception as e:
            return name, False, str(e)[:120]
    with ThreadPoolExecutor(len(OPS_SERVICES)) as ex:
        return list(ex.map(probe, OPS_SERVICES))


def restart_service(name):
    """Restart one service. The website itself restarts a moment later, after this answer has been sent."""
    if name == "Website":
        threading.Timer(1.5, subprocess.run, (["systemctl", "--user", "restart", "manga-request"],)).start()
        return
    _, _, unit, system = next(s for s in OPS_SERVICES if s[0] == name)
    cmd = ["sudo", "-n", "systemctl", "restart", unit] if system else ["systemctl", "--user", "restart", unit]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
    if r.returncode:
        raise RuntimeError((r.stderr or r.stdout).strip()[:200] or f"exit {r.returncode}")


def fstab_mounts():
    try:
        lines = Path("/etc/fstab").read_text().splitlines()
    except OSError:
        return []
    return [f.split()[1] for f in lines if len(f.split()) > 1 and not f.lstrip().startswith("#")]


def drives():
    """[{"path", "mounted", "total", "free", "removable"}]: the system drive and every fstab drive holding media."""
    folders = [os.path.normpath(f) for f in ANIME_FOLDERS + MOVIE_FOLDERS + FILM_FOLDERS + TV_FOLDERS]
    out = []
    for mnt in ["/"] + [m for m in fstab_mounts() if m != "/" and any(f == m or f.startswith(m.rstrip("/") + "/") for f in folders)]:
        mounted = mnt == "/" or os.path.ismount(mnt)
        use = shutil.disk_usage(mnt) if mounted else None
        out.append({"path": mnt, "mounted": mounted, "total": use.total if use else 0, "free": use.free if use else 0,
                    "removable": mnt != "/"})
    return out


def mount_drive(mnt):
    if mnt not in fstab_mounts() or mnt == "/":
        raise ValueError("not a media drive")
    r = subprocess.run(["sudo", "-n", "mount", mnt], capture_output=True, text=True, timeout=120)
    if r.returncode and not os.path.ismount(mnt):
        raise RuntimeError((r.stderr or r.stdout).strip()[:200] or "mount failed")


def _recycle_inodes():
    """{(device, inode): size} of the recycle bin's files, and its total size."""
    out, total = {}, 0
    for root, _, files in os.walk(RECYCLE):
        for f in files:
            try:
                st = os.lstat(os.path.join(root, f))
            except OSError:
                continue
            out[(st.st_dev, st.st_ino)] = st
            total += st.st_size
    return out, total


def recycle_only_torrents(inodes):
    """Finished Sonarr/Radarr torrents still seeding files that are now only in the recycle bin (a copy that was
    replaced or a title that was deleted): every video file is the torrent's copy plus the recycle bin's, nothing else.
    [(hash, bytes)]."""
    out = []
    for t in qbit("/torrents/info") or []:
        if (t.get("progress") or 0) < 1 or not t.get("category", "").startswith(("sonarr", "radarr")):
            continue
        cp = t.get("content_path") or ""
        paths = [cp] if os.path.isfile(cp) else [os.path.join(r, f) for r, _, fs in os.walk(cp) for f in fs]
        size, only_here = 0, bool(paths)
        for path in paths:
            try:
                st = os.lstat(path)
            except OSError:
                only_here = False
                break
            if st.st_size < 50e6:                     # .nfo, samples, subtitles
                continue
            if (st.st_dev, st.st_ino) not in inodes or st.st_nlink != 2:
                only_here = False
                break
            size += st.st_size
        if only_here and size:
            out.append((t["hash"], size))
    return out


def recycle_size():
    """(bytes in the recycle bin, bytes emptying it frees): files also in a torrent that's still seeding count when
    that torrent only seeds what's in the bin (it goes too); anything else still linked elsewhere frees nothing."""
    inodes, total = _recycle_inodes()
    seeding = {k for k, st in inodes.items() if st.st_nlink > 1}
    freed = sum(st.st_size for st in inodes.values() if st.st_nlink == 1)
    try:
        freed += sum(size for _, size in recycle_only_torrents(inodes))
    except Exception:
        pass
    return total, min(freed, total) if seeding else freed


def empty_recycle_bin():
    """Delete everything in Sonarr/Radarr's recycle bin now instead of after 14 days, and the torrents that were only
    still seeding what's in it. Returns the bytes freed."""
    bin_ = RECYCLE.resolve()
    if bin_ in (Path("/"), HOME.resolve()) or not bin_.is_dir() or len(bin_.parts) < 3:
        raise RuntimeError(f"refusing to empty {bin_}")
    before = shutil.disk_usage(bin_).free
    inodes, _ = _recycle_inodes()
    hashes = [h for h, _ in recycle_only_torrents(inodes)]
    if hashes:
        qbit("/torrents/delete", {"hashes": "|".join(hashes), "deleteFiles": "true"})
    for item in bin_.iterdir():
        if item.is_dir() and not item.is_symlink():
            shutil.rmtree(item)
        else:
            item.unlink()
    sleep(2)                                         # qBittorrent deletes its files in the background
    return max(shutil.disk_usage(bin_).free - before, 0)


def stuck_downloads(now=None):
    """Downloads Sonarr/Radarr are waiting on that haven't received anything for STUCK_HOURS (dead or fake torrents):
    [{"app", "id", "title", "what", "hours", "why"}]."""
    now = now or time.time()
    torrents = {t["hash"].upper(): t for t in qbit("/torrents/info?filter=downloading") or []}
    torrents.update({t["hash"].upper(): t for t in qbit("/torrents/info?filter=stalled_downloading") or []})
    out = []
    for app, api, what in (("sonarr", sonarr, lambda r: f"{r['series']['title']} S{r['episode']['seasonNumber']:02d}E{r['episode']['episodeNumber']:02d}"
                            if r.get("series") and r.get("episode") else r.get("title", "")),
                           ("radarr", radarr, lambda r: (r.get("movie") or {}).get("title") or r.get("title", ""))):
        extra = "includeSeries=true&includeEpisode=true" if app == "sonarr" else "includeMovie=true"
        seen = set()
        for r in api("GET", f"/queue?pageSize=1000&{extra}")["records"]:
            t = torrents.get((r.get("downloadId") or "").upper())
            if not t or t["hash"] in seen or (t.get("progress") or 0) >= 1:
                continue
            last = max(t.get("last_activity") or 0, t.get("added_on") or 0)
            if t["state"] in ("metaDL", "forcedMetaDL") or not t.get("dlspeed"):
                hours = (now - last) / 3600
                if hours >= STUCK_HOURS:
                    seen.add(t["hash"])
                    why = ("never started: no one is sharing it (often a fake upload)" if t["state"] in ("metaDL", "forcedMetaDL")
                           or not t.get("progress") else f"stopped at {t['progress']:.0%}: no one is sharing the rest")
                    out.append({"app": app, "id": r["id"], "title": r.get("title", ""), "what": what(r), "hours": int(hours), "why": why})
    return out


def replace_download(app, queue_id):
    """Remove a stuck download, blocklist that release so it isn't picked again, and let Sonarr/Radarr search again."""
    api = {"sonarr": sonarr, "radarr": radarr}[app]
    api("DELETE", f"/queue/{int(queue_id)}?removeFromClient=true&blocklist=true&skipRedownload=false")


def watchdog_activity(n=10):
    """The watchdog's latest log lines, newest first, as [(time, alert?, text)], and when it last ran (its state file is
    written on every 15-minute check; the log only gets lines when something happened)."""
    try:
        lines = WATCHDOG_LOG.read_text(errors="replace").splitlines()[-n * 4:]
    except OSError:
        lines = []
    out = []
    for line in lines:
        m = re.match(r"(\d{4}-\d\d-\d\d \d\d:\d\d):\d\d (.*)", line)
        if m and not m.group(2).startswith("[dry-run]"):
            alert = m.group(2).startswith("alert: ")
            out.append((m.group(1), alert, m.group(2)[7:].replace(" | ", ": ", 1) if alert else m.group(2)))
    try:
        last = WATCHDOG_STATE.stat().st_mtime
    except OSError:
        last = None
    return out[-n:][::-1], last


def jellyfin_user_id(user):
    return next(u["Id"] for u in jellyfin("/Users") if u["Name"].lower() == user.lower())


def reset_jellyfin_password(user, new, repeat):
    """Set a Jellyfin password without the old one (the admin key does it). Returns an error message or None."""
    if len(new) < 12:
        return "The new password needs at least 12 characters."
    if new != repeat:
        return "The two new passwords don't match."
    jellyfin(f"/Users/{jellyfin_user_id(user)}/Password", "POST", {"CurrentPw": "", "NewPw": new})
    return None


def approve_quick_connect(user, code):
    """Sign a Jellyfin app in as this user with the code it shows on its Quick Connect screen."""
    code = re.sub(r"\s+", "", code or "")
    if not re.fullmatch(r"[0-9A-Za-z]{4,10}", code):
        return "Type the code the Jellyfin app shows."
    try:
        ok = jellyfin(f"/QuickConnect/Authorize?code={code}&userId={jellyfin_user_id(user)}", "POST")
    except Exception:
        ok = False
    return None if ok else "That code didn't work. It may have expired: open Quick Connect in the app again for a new one."


def size_text(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1000 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB", "MB") else f"{n:.1f} {unit}".replace(".0 ", " ")
        n /= 1000
