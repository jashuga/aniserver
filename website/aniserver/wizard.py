"""First-time setup: `python3 app.py setup` (install.sh runs it). Finds Sonarr, Radarr, Prowlarr, qBittorrent, Jellyfin
and Kodi on this machine and reads their API keys; on a fresh install it also connects them (media folders, qBittorrent
as the download client, Prowlarr with Nyaa, Jellyfin's first-run setup and libraries). Then it asks a few questions,
writes config.json and creates the owner's login. Safe to run again: it keeps the existing settings and only adds
what's missing."""
import base64
import getpass
import json
import os
import re
import secrets
import socket
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

CONFIG_FILE = Path(os.environ.get("ANISERVER_CONFIG") or Path.home() / ".config/manga-request/config.json")
MEDIA = Path.home() / "Media"
COLORS = ["#ff7a1a", "#4f8cff", "#a78bfa", "#34d399", "#f472b6", "#facc15"]
CLIENT = 'MediaBrowser Client="aniserver setup", Device="aniserver", DeviceId="aniserver-setup", Version="1.0"'
# name, default port, API version, required?
ARR = (("Sonarr", 8989, "v3", True), ("Radarr", 7878, "v3", True), ("Prowlarr", 9696, "v1", False))


def http(url, data=None, headers=None, method=None, timeout=20):
    if isinstance(data, (dict, list)):
        data, headers = json.dumps(data).encode(), {"Content-Type": "application/json", **(headers or {})}
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        return json.loads(raw) if raw[:1] in (b"{", b"[") else raw.decode(errors="ignore")


def reachable(url):
    try:
        urllib.request.urlopen(url, timeout=3)
        return True
    except urllib.error.HTTPError:
        return True                                    # it answered (maybe "log in first")
    except (OSError, ValueError):
        return False


def arr_settings(app):
    """(port, API key) from the config.xml Sonarr/Radarr/Prowlarr keep (user install or the usual system places)."""
    for d in (Path.home() / ".config" / app, Path("/var/lib") / app.lower(), Path("/var/lib") / app):
        try:
            xml = (d / "config.xml").read_text()
        except OSError:
            continue
        key, port = re.search(r"<ApiKey>(\w+)</ApiKey>", xml), re.search(r"<Port>(\d+)</Port>", xml)
        if key:
            return (int(port.group(1)) if port else None), key.group(1)
    return None, None


def ask(question, default="", secret=False):
    prompt = f"  {question}" + (f" [{default}]" if default and not secret else "") + ": "
    answer = (getpass.getpass(prompt) if secret else input(prompt)).strip()
    return answer or default


def yes(question, default=True):
    return ask(question + (" (Y/n)" if default else " (y/N)"), "y" if default else "n").lower().startswith("y")


# ---------- connecting the programs (only what's missing) ----------

def from_schema(schema, implementation, values, **extra):
    """A Sonarr/Radarr/Prowlarr item built from its own schema, with the named fields filled in."""
    item = dict(next(s for s in schema if s.get("implementation") == implementation or s.get("definitionName") == implementation))
    item["fields"] = [dict(f, value=values[f["name"]]) if f["name"] in values else f for f in item.get("fields", [])]
    item.update(extra)
    return item


def ensure_root_folder(url, key, ver, folder):
    h = {"X-Api-Key": key}
    have = [f["path"] for f in http(f"{url}/api/{ver}/rootfolder", headers=h)]
    if have:
        return have
    Path(folder).mkdir(parents=True, exist_ok=True)
    http(f"{url}/api/{ver}/rootfolder", {"path": str(folder)}, h, "POST")
    return [str(folder)]


def ensure_download_client(url, key, ver, qbit_url, category):
    h = {"X-Api-Key": key}
    if http(f"{url}/api/{ver}/downloadclient", headers=h):
        return False
    q = urllib.parse.urlparse(qbit_url)
    item = from_schema(http(f"{url}/api/{ver}/downloadclient/schema", headers=h), "QBittorrent",
                       {"host": q.hostname, "port": q.port or 80, "tvCategory": category, "movieCategory": category},
                       name="qBittorrent", enable=True, priority=1)
    http(f"{url}/api/{ver}/downloadclient?forceSave=true", item, h, "POST")
    return True


def ensure_prowlarr(url, key, apps):
    """Prowlarr feeds its indexers to Sonarr/Radarr; Nyaa (anime and manga) is added if there are no indexers."""
    h, added = {"X-Api-Key": key}, []
    have = {a["implementation"] for a in http(f"{url}/api/v1/applications", headers=h)}
    schema = None
    for name, app_url, app_key in apps:
        if name not in have:
            schema = schema or http(f"{url}/api/v1/applications/schema", headers=h)
            item = from_schema(schema, name, {"prowlarrUrl": url, "baseUrl": app_url, "apiKey": app_key}, name=name, syncLevel="fullSync")
            http(f"{url}/api/v1/applications?forceSave=true", item, h, "POST")
            added.append(name)
    if not http(f"{url}/api/v1/indexer", headers=h):
        profile = http(f"{url}/api/v1/appprofile", headers=h)[0]["id"]
        item = from_schema(http(f"{url}/api/v1/indexer/schema", headers=h), "nyaasi", {}, name="Nyaa.si", enable=True, appProfileId=profile)
        http(f"{url}/api/v1/indexer?forceSave=true", item, h, "POST")
        added.append("Nyaa.si")
    return added


def jellyfin_first_run(url, user, password):
    """Jellyfin's first-run setup (only on a fresh Jellyfin): the owner becomes its admin. Returns True if it ran."""
    if http(f"{url}/System/Info/Public").get("StartupWizardCompleted"):
        return False
    http(f"{url}/Startup/Configuration", {"UICulture": "en-US", "MetadataCountryCode": "US", "PreferredMetadataLanguage": "en"}, method="POST")
    http(f"{url}/Startup/User")
    http(f"{url}/Startup/User", {"Name": user, "Password": password}, method="POST")
    http(f"{url}/Startup/RemoteAccess", {"EnableRemoteAccess": True, "EnableAutomaticPortMapping": False}, method="POST")
    http(f"{url}/Startup/Complete", b"", method="POST")
    return True


def jellyfin_token(url, user, password):
    return http(f"{url}/Users/AuthenticateByName", {"Username": user, "Pw": password}, {"Authorization": CLIENT}, "POST")["AccessToken"]


def jellyfin_api_key(url, token):
    h = {"Authorization": f'{CLIENT}, Token="{token}"'}
    http(f"{url}/Auth/Keys?App=aniserver", b"", h, "POST")
    return next(k["AccessToken"] for k in reversed(http(f"{url}/Auth/Keys", headers=h)["Items"]) if k.get("AppName") == "aniserver")


def ensure_libraries(url, token, anime, movies):
    h = {"Authorization": f'{CLIENT}, Token="{token}"'}
    have = {v["Name"] for v in http(f"{url}/Library/VirtualFolders", headers=h)}
    added = []
    for name, kind, folders in (("Anime", "tvshows", anime), ("Movies", "movies", movies)):
        if folders and not have:                        # a fresh Jellyfin: one library each
            q = urllib.parse.urlencode({"name": name, "collectionType": kind, "refreshLibrary": "true"})
            http(f"{url}/Library/VirtualFolders?{q}", {"LibraryOptions": {"PathInfos": [{"Path": p} for p in folders]}}, h, "POST")
            added.append(name)
    return added


def jellyfin_key_from_admin(url):
    """An existing Jellyfin: create an API key from an admin login (used once, not stored), or take a pasted one."""
    print("  aniserver needs a Jellyfin API key. Sign in with a Jellyfin admin account to create one (not stored),")
    print("  or leave the user name empty to paste a key from Jellyfin → Dashboard → API Keys.")
    user = ask("Jellyfin admin user name")
    if not user:
        return ask("Jellyfin API key")
    return jellyfin_api_key(url, jellyfin_token(url, user, ask("Jellyfin admin password", secret=True)))


def nyaa_indexer(url, key):
    """Prowlarr's Nyaa indexer for manga search (one with "manga" in its name if there is one), or None."""
    try:
        nyaa = [i for i in http(f"{url}/api/v1/indexer", headers={"X-Api-Key": key}) if "nyaa" in i["name"].lower()]
        return next((i["id"] for i in sorted(nyaa, key=lambda i: "manga" not in i["name"].lower())), None)
    except (OSError, ValueError, KeyError):
        return None


# ---------- settings and the owner's login ----------

def build_config(old, found, people, public_host="", watch_host=""):
    """config.json from the existing settings (kept), what was found on this machine, and the people."""
    conf = dict(old)
    conf.setdefault("port", 5050)
    conf.setdefault("secret", secrets.token_hex(32))
    conf.setdefault("anime_artwork", "japanese")
    for name in ("sonarr", "radarr", "prowlarr", "qbittorrent", "jellyfin", "kodi"):
        if f"{name}_url" in found:
            conf[f"{name}_url"] = found[f"{name}_url"]
    for k in ("sonarr_key", "radarr_key", "prowlarr_key", "jellyfin_key", "kodi_password", "indexer_id", "anime_folders", "movie_folders"):
        if found.get(k) not in (None, "", []):
            conf[k] = found[k]
    conf["manga"] = bool(found.get("prowlarr_key") and found.get("indexer_id"))
    conf["profiles"] = {u: [name, COLORS[n % len(COLORS)]] for n, (u, name) in enumerate(people)}
    conf["tv_profile"] = people[0][0]
    if public_host:
        conf["public_host"] = public_host
    if watch_host:
        conf["watch_host"] = watch_host
    return conf


def write_config(conf):
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(conf, indent=2) + "\n")
    tmp.chmod(0o600)
    tmp.replace(CONFIG_FILE)


def choose_password(user):
    while True:
        pw = ask(f"Password for {user} (12+ characters)", secret=True)
        if len(pw) < 12:
            print("  Use at least 12 characters.")
        elif pw != ask("Repeat it", secret=True):
            print("  The two passwords don't match.")
        else:
            return pw


def create_login(user, pw):
    """The owner's website login: the password and an authenticator app (checked with one code before it's saved)."""
    from aniserver import auth                          # needs config.json, so only after it's written
    totp = base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")
    print("\n  Add aniserver to an authenticator app (Google/Microsoft Authenticator, 1Password, Aegis…):")
    print(f"  - on your phone, open:  otpauth://totp/aniserver:{user}?secret={totp}&issuer=aniserver")
    print(f"  - or enter this key:    {' '.join(totp[i:i + 4] for i in range(0, len(totp), 4))}\n")
    for _ in range(5):
        counter = auth.totp_ok(totp, ask("6-digit code the app shows"))
        if counter:
            users = auth.remote_users()
            users[user] = {"pw": auth.hash_password(pw), "totp": totp, "last": counter}
            auth.save_remote_users(users)
            return True
        print("  That code didn't match. Check the phone's clock and try again.")
    return False


def main():
    print("\naniserver setup — writes", CONFIG_FILE, "and creates your login. Press Enter to accept [defaults].\n")
    old = json.loads(CONFIG_FILE.read_text()) if CONFIG_FILE.exists() else {}

    print("1. You")
    owner = ask("Your user name", next(iter(old.get("profiles") or {}), getpass.getuser())).lower()
    display = ask("Your display name", owner.title())
    users_file = CONFIG_FILE.parent / "remote-users.json"
    has_login = users_file.exists() and owner in json.loads(users_file.read_text() or "{}")
    pw = choose_password(owner) if not has_login or yes("You already have a login. Make a new password and code?", False) else None

    print("\n2. The other programs")
    found = {}
    for app, port, ver, required in ARR:
        disk_port, disk_key = arr_settings(app)
        url = old.get(f"{app.lower()}_url") or f"http://localhost:{disk_port or port}"
        if not reachable(url):
            print(f"  {app} isn't answering at {url}.")
            url = ask(f"{app} address" + ("" if required else " (empty: skip)"), url if required else "")
            if not url:
                continue
        found[f"{app.lower()}_url"] = url
        found[f"{app.lower()}_key"] = old.get(f"{app.lower()}_key") or disk_key or ask(f"{app} API key (Settings → General)")
        print(f"  ✓ {app} at {url}")
    qb = old.get("qbittorrent_url", "http://localhost:8090")
    try:
        http(f"{qb}/api/v2/app/version")
        print(f"  ✓ qBittorrent at {qb}")
    except (OSError, ValueError):
        print(f"  ! qBittorrent at {qb} didn't answer without a login: in its Web UI options, turn on")
        print("    'Bypass authentication for clients on localhost'.")
    found["qbittorrent_url"] = qb
    jf = old.get("jellyfin_url", "http://localhost:8096")
    while not reachable(f"{jf}/health"):
        jf = ask(f"Jellyfin isn't answering at {jf}. Jellyfin address", "http://localhost:8096")
    found["jellyfin_url"] = jf

    print("\n3. Connecting them (only what's missing)")
    for app, cat, sub in (("sonarr", "sonarr", "Anime"), ("radarr", "radarr", "Movies")):
        url, key = found[f"{app}_url"], found[f"{app}_key"]
        found["anime_folders" if app == "sonarr" else "movie_folders"] = ensure_root_folder(url, key, "v3", MEDIA / sub)
        if ensure_download_client(url, key, "v3", qb, cat):
            print(f"  ✓ {app.title()} downloads with qBittorrent")
    print(f"  ✓ media folders: anime {found['anime_folders']}, movies {found['movie_folders']}")
    if found.get("prowlarr_key"):
        for name in ensure_prowlarr(found["prowlarr_url"], found["prowlarr_key"],
                                    [("Sonarr", found["sonarr_url"], found["sonarr_key"]), ("Radarr", found["radarr_url"], found["radarr_key"])]):
            print(f"  ✓ Prowlarr: added {name}")
        found["indexer_id"] = nyaa_indexer(found["prowlarr_url"], found["prowlarr_key"])
    if old.get("jellyfin_key"):
        found["jellyfin_key"] = old["jellyfin_key"]
    elif pw and jellyfin_first_run(jf, owner, pw):
        print(f"  ✓ Jellyfin set up, with {owner} as its admin (same password)")
        token = jellyfin_token(jf, owner, pw)
        found["jellyfin_key"] = jellyfin_api_key(jf, token)
        for name in ensure_libraries(jf, token, found["anime_folders"], found["movie_folders"]):
            print(f"  ✓ Jellyfin library: {name}")
    else:
        found["jellyfin_key"] = jellyfin_key_from_admin(jf)
    kodi = next((u for u in (old.get("kodi_url"), "http://localhost:8080", "http://localhost:8085") if u and reachable(u + "/jsonrpc")), "")
    if kodi and yes(f"Kodi found at {kodi}. Use it for 'Play on the TV'?"):
        found["kodi_url"], found["kodi_password"] = kodi, old.get("kodi_password") or ask("Kodi web server password (user kodi)", secret=True)
    else:
        found["kodi_url"] = ""
        print("  - no TV (Kodi): the TV buttons stay hidden")

    print("\n4. Other people and the internet (optional)")
    people = [(owner, display)]
    for u in [x.strip().lower() for x in ask("Other people's user names, comma-separated").split(",") if x.strip()]:
        people.append((u, u.title()))
    public = ask("Public address of the website (e.g. tv.example.com, behind HTTPS; empty: home only)", old.get("public_host", ""))
    watch = ask("Public address of Jellyfin (optional)", old.get("watch_host", "")) if public else ""

    write_config(build_config(old, found, people, public, watch))
    print(f"\n  ✓ settings saved to {CONFIG_FILE}")
    if pw:
        print(f"\n5. Your login ({owner})")
        if not create_login(owner, pw):
            print("  No login saved. Run `python3 app.py setup` again to try once more.")
            return 1
    print(f"\nDone. Open http://{socket.gethostname()}.local:{old.get('port', 5050)} and sign in."
          " Logins for the other people: /security on the website (each also needs a Jellyfin user of the same name).\n")
    return 0
