"""First-time setup: `python3 app.py setup` (install.sh runs it). Finds Sonarr, Radarr, Prowlarr, qBittorrent, Jellyfin
and Kodi on this machine, reads their API keys and media folders, asks a few questions, writes config.json and creates
the owner's login (password + authenticator code). Safe to run again: it starts from the existing settings."""
import base64
import getpass
import json
import os
import re
import secrets
import socket
import urllib.parse
import urllib.request
from pathlib import Path

CONFIG_FILE = Path(os.environ.get("ANISERVER_CONFIG") or Path.home() / ".config/manga-request/config.json")
COLORS = ["#ff7a1a", "#4f8cff", "#a78bfa", "#34d399", "#f472b6", "#facc15"]
# name, default address, where its config.xml (with the API key) usually lives, required?
ARR = (("Sonarr", "http://localhost:8989", "Sonarr", True), ("Radarr", "http://localhost:7878", "Radarr", True),
       ("Prowlarr", "http://localhost:9696", "Prowlarr", False))


def http(url, data=None, headers=None, method=None, timeout=8):
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


def api_key_from_disk(app):
    """The ApiKey Sonarr/Radarr/Prowlarr keep in their config.xml (user install or the usual system locations)."""
    for d in (Path.home() / ".config" / app, Path("/var/lib") / app.lower(), Path("/var/lib") / app):
        try:
            m = re.search(r"<ApiKey>(\w+)</ApiKey>", (d / "config.xml").read_text())
        except OSError:
            continue
        if m:
            return m.group(1)
    return None


def ask(question, default="", secret=False):
    prompt = f"  {question}" + (f" [{default}]" if default and not secret else "") + ": "
    answer = (getpass.getpass(prompt) if secret else input(prompt)).strip()
    return answer or default


def yes(question, default=True):
    return ask(question + (" (Y/n)" if default else " (y/N)"), "y" if default else "n").lower().startswith("y")


def jellyfin_key(url):
    """Create an API key from a Jellyfin admin login (used once, not stored), or take a pasted one."""
    print("  aniserver needs a Jellyfin API key. Sign in with a Jellyfin admin account to create one (not stored),")
    print("  or leave the user name empty to paste a key from Jellyfin → Dashboard → API Keys.")
    user = ask("Jellyfin admin user name")
    if not user:
        return ask("Jellyfin API key")
    pw = ask("Jellyfin admin password", secret=True)
    client = 'MediaBrowser Client="aniserver setup", Device="aniserver", DeviceId="aniserver-setup", Version="1.0"'
    auth = http(f"{url}/Users/AuthenticateByName", json.dumps({"Username": user, "Pw": pw}).encode(),
                {"Content-Type": "application/json", "Authorization": client})
    headers = {"Authorization": f'{client}, Token="{auth["AccessToken"]}"'}
    http(f"{url}/Auth/Keys?App=aniserver", b"", headers, "POST")
    keys = http(f"{url}/Auth/Keys", headers=headers)["Items"]
    return next(k["AccessToken"] for k in reversed(keys) if k.get("AppName") == "aniserver")


def root_folders(url, key):
    try:
        return [f["path"] for f in http(f"{url}/api/v3/rootfolder", headers={"X-Api-Key": key})]
    except (OSError, ValueError, KeyError):
        return []


def nyaa_indexer(url, key):
    """Prowlarr's Nyaa indexer (manga search uses it), or None."""
    try:
        return next((i["id"] for i in http(f"{url}/api/v1/indexer", headers={"X-Api-Key": key}) if "nyaa" in i["name"].lower()), None)
    except (OSError, ValueError, KeyError):
        return None


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


def create_login(user):
    """The owner's login: a password and an authenticator app (checked with one code before it's saved)."""
    from aniserver import auth                          # needs config.json, so only after it's written
    while True:
        pw = ask(f"Password for {user} (12+ characters)", secret=True)
        problem = auth.password_problem(pw, ask("Repeat it", secret=True))
        if not problem:
            break
        print("  " + problem)
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
        print("  That code didn't match. Check the app's clock and try again.")
    return False


def main():
    print("\naniserver setup — writes", CONFIG_FILE, "and creates your login. Press Enter to accept [defaults].\n")
    old = json.loads(CONFIG_FILE.read_text()) if CONFIG_FILE.exists() else {}
    found = {}
    print("1. The other programs")
    for app, default, folder, required in ARR:
        url = old.get(f"{app.lower()}_url", default)
        if not reachable(url):
            print(f"  {app} isn't answering at {url}.")
            url = ask(f"{app} address" + ("" if required else " (empty: skip)"), default if required else "")
            if not url:
                continue
        key = old.get(f"{app.lower()}_key") or api_key_from_disk(folder) or ask(f"{app} API key (Settings → General)")
        found[f"{app.lower()}_url"], found[f"{app.lower()}_key"] = url, key
        print(f"  ✓ {app} at {url}")
    found["anime_folders"] = root_folders(found["sonarr_url"], found["sonarr_key"])
    found["movie_folders"] = root_folders(found["radarr_url"], found["radarr_key"])
    if found.get("prowlarr_key"):
        found["indexer_id"] = nyaa_indexer(found["prowlarr_url"], found["prowlarr_key"])
    qb = old.get("qbittorrent_url", "http://localhost:8090")
    try:
        http(f"{qb}/api/v2/app/version")
        print(f"  ✓ qBittorrent at {qb}")
    except (OSError, ValueError):
        print(f"  ! qBittorrent at {qb} didn't answer without a login: in its Web UI settings, turn on")
        print("    'Bypass authentication for clients on localhost'.")
    found["qbittorrent_url"] = qb
    jf = old.get("jellyfin_url", "http://localhost:8096")
    while not reachable(f"{jf}/health"):
        jf = ask(f"Jellyfin isn't answering at {jf}. Jellyfin address", "http://localhost:8096")
    found["jellyfin_url"] = jf
    found["jellyfin_key"] = old.get("jellyfin_key") or jellyfin_key(jf)
    print(f"  ✓ Jellyfin at {jf}")
    kodi = next((u for u in (old.get("kodi_url"), "http://localhost:8080", "http://localhost:8085") if u and reachable(u + "/jsonrpc")), "")
    if kodi and yes(f"Kodi found at {kodi}. Use it for 'Play on the TV'?"):
        found["kodi_url"], found["kodi_password"] = kodi, old.get("kodi_password") or ask("Kodi web server password (user kodi)", secret=True)
    else:
        found["kodi_url"] = ""
        print("  - no TV (Kodi): the TV buttons stay hidden")
    print(f"  media folders: anime {found['anime_folders'] or '(none yet: add a root folder in Sonarr)'}, "
          f"movies {found['movie_folders'] or '(none yet: add a root folder in Radarr)'}")

    print("\n2. People (each needs a Jellyfin user with the same name, for their own watch history)")
    owner_default = next(iter(old.get("profiles") or {}), getpass.getuser())
    owner = ask("Your user name", owner_default).lower()
    people = [(owner, ask("Your display name", owner.title()))]
    for u in [x.strip().lower() for x in ask("Other people's user names, comma-separated (optional)").split(",") if x.strip()]:
        people.append((u, u.title()))

    print("\n3. From outside the home (optional: put the site behind HTTPS, e.g. Caddy)")
    public = ask("Public address of the website (e.g. tv.example.com, empty: home only)", old.get("public_host", ""))
    watch = ask("Public address of Jellyfin (optional)", old.get("watch_host", "")) if public else ""

    write_config(build_config(old, found, people, public, watch))
    print(f"\n  ✓ settings saved to {CONFIG_FILE}")

    print(f"\n4. Your login ({owner})")
    from aniserver import auth
    if owner in auth.remote_users() and not yes("You already have a login. Make a new password and code?", False):
        pass
    elif not create_login(owner):
        print("  No login saved. Run `python3 app.py setup` again to try once more.")
        return 1
    print("\nDone. Start it with:  systemctl --user restart manga-request   (or: python3 app.py)")
    print(f"Then open http://{socket.gethostname()}.local:{old.get('port', 5050)} and sign in. Logins for the other people:"
          " /security on the website.\n")
    return 0
