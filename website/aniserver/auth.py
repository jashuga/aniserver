"""Signing in: per-person passwords (PBKDF2), authenticator codes (TOTP), signed cookies, password changes, failed-login tracking."""
import base64
import hashlib
import hmac
import json
import re
import secrets
import struct
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from aniserver.config import CONF, JELLYFIN

# ---------- remote (internet) logins: username + strong password + TOTP code ----------
# Requests that arrive through the Caddy HTTPS proxy (peer 127.0.0.1 with X-Forwarded-For) are "public".
# Everything else reached port 5050 directly, i.e. from the home network or Tailscale (5050 is not forwarded).


REMOTE_USERS_FILE = Path.home() / ".config/manga-request/remote-users.json"
AUTH_LOG = Path.home() / ".local/share/htpc-web/auth.log"      # read by fail2ban
REMOTE_COOKIE = "htpc_remote"
REMOTE_SESSION_DAYS = 365   # sign in with password + code once per device; stays signed in for a year
FAILS = {}            # ip -> [timestamps of failed logins]
PENDING = {}          # user -> (password hash, totp secret) during setup


def remote_users():
    try:
        return json.loads(REMOTE_USERS_FILE.read_text())
    except (OSError, ValueError):
        return {}


def save_remote_users(users):
    tmp = REMOTE_USERS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(users, indent=2))
    tmp.chmod(0o600)
    tmp.replace(REMOTE_USERS_FILE)


def hash_password(pw, salt=None, iterations=400_000):
    salt = salt or secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, iterations)
    return f"pbkdf2${iterations}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def check_password(pw, stored):
    try:
        _, it, salt, dk = stored.split("$")
        return hmac.compare_digest(hash_password(pw, base64.b64decode(salt), int(it)).split("$")[3], dk)
    except (ValueError, AttributeError):
        return False


def hotp(secret, counter):
    key = base64.b32decode(secret + "=" * (-len(secret) % 8))
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    o = digest[-1] & 0x0F
    return f"{(struct.unpack('>I', digest[o:o + 4])[0] & 0x7FFFFFFF) % 1_000_000:06d}"


def totp_ok(secret, code, last_counter=0):
    """RFC 6238 (30 s, 6 digits), ±1 step of clock drift; returns the matched counter (codes can't be reused)."""
    code = (code or "").strip().replace(" ", "")
    now = int(time.time() // 30)
    for c in (now - 1, now, now + 1):
        if c > last_counter and hmac.compare_digest(hotp(secret, c), code):
            return c
    return None


TRUST_COOKIE = "htpc_trusted"
TRUST_DAYS = 730      # a device that passed the 2FA code once only needs the password after that


def remote_token(user, exp, purpose="remote"):
    # "gen" goes up when the person changes their password, which invalidates every sign-in made before that
    gen = (remote_users().get(user) or {}).get("gen", 0)
    sig = hmac.new(CONF["secret"].encode(), (f"{user}:{exp}:{purpose}" + (f":{gen}" if gen else "")).encode(), hashlib.sha256).hexdigest()
    return f"{user}:{exp}:{sig}"


def remote_token_user(token, purpose="remote"):
    try:
        user, exp, sig = token.split(":")
        if int(exp) < time.time() or user not in remote_users():
            return None
        return user if hmac.compare_digest(remote_token(user, exp, purpose), token) else None
    except ValueError:
        return None


def cookie_flags(secure):
    # Secure only over HTTPS: at home the site also opens as plain http://<laptop>:5050, where browsers drop Secure cookies
    return "Path=/; HttpOnly; SameSite=Lax" + ("; Secure" if secure else "")


def session_cookies(user, secure=True):
    """Signed-in for a year (sliding), and this browser remembered as trusted for two."""
    exp, texp = int(time.time()) + REMOTE_SESSION_DAYS * 86400, int(time.time()) + TRUST_DAYS * 86400
    return [f"{REMOTE_COOKIE}={remote_token(user, exp)}; Max-Age={REMOTE_SESSION_DAYS * 86400}; {cookie_flags(secure)}",
            f"{TRUST_COOKIE}={remote_token(user, texp, 'trusted')}; Max-Age={TRUST_DAYS * 86400}; {cookie_flags(secure)}"]


def password_problem(new, repeat, current=""):
    if len(new) < 12:
        return "Use at least 12 characters."
    if new != repeat:
        return "The two new passwords don't match."
    if new == current:
        return "That's the same as the current password."
    if new.lower() in [p.lower() for p in CONF.get("reserved_passwords", [])]:
        return "Pick a password you don't use for anything else."
    return None


def change_website_password(user, current, new, repeat):
    """Internet sign-in password (the authenticator code stays the same). Returns an error message or None."""
    users = remote_users()
    rec = users.get(user)
    if not rec:
        return "This profile doesn't have an internet sign-in yet."
    if not check_password(current, rec["pw"]):
        return "wrong-current"
    problem = password_problem(new, repeat, current)
    if problem:
        return problem
    rec["pw"] = hash_password(new)
    rec["gen"] = rec.get("gen", 0) + 1
    save_remote_users(users)
    note_password_changed("Website", user)
    return None


def change_jellyfin_password(user, current, new, repeat):
    """Jellyfin password (apps and the public Jellyfin address), changed as that user after checking the current one."""
    problem = password_problem(new, repeat, current)
    if problem:
        return problem
    client = 'MediaBrowser Client="aniserver", Device="aniserver account page", DeviceId="aniserver-account", Version="1.0"'
    try:
        req = urllib.request.Request(JELLYFIN + "/Users/AuthenticateByName", data=json.dumps({"Username": user, "Pw": current}).encode(),
                                     headers={"Content-Type": "application/json", "Authorization": client})
        with urllib.request.urlopen(req, timeout=20) as r:
            auth = json.load(r)
    except urllib.error.HTTPError:
        return "wrong-current"
    as_user = {"Content-Type": "application/json", "Authorization": f'{client}, Token="{auth["AccessToken"]}"'}
    req = urllib.request.Request(f"{JELLYFIN}/Users/{auth['User']['Id']}/Password", method="POST", headers=as_user,
                                 data=json.dumps({"CurrentPw": current, "NewPw": new}).encode())
    urllib.request.urlopen(req, timeout=20).read()
    try:
        urllib.request.urlopen(urllib.request.Request(f"{JELLYFIN}/Sessions/Logout", method="POST", headers=as_user, data=b""), timeout=10)
    except Exception:
        pass
    note_password_changed("Jellyfin", user)
    return None


CREDENTIALS_FILE = Path.home() / "htpc-credentials.txt"


def note_password_changed(kind, user):
    """Don't leave a stale password in ~/htpc-credentials.txt (the new one isn't written anywhere)."""
    f = CREDENTIALS_FILE
    try:
        text = f.read_text()
    except OSError:
        return
    stamp = f"password: (changed on the website {time.strftime('%Y-%m-%d')})"
    lines = [re.sub(r"password: \S+(\s*\(ask \w+ to change it\))?", stamp, line)
             if kind in line and re.search(rf"\buser: {re.escape(user)}\b", line) else line for line in text.splitlines()]
    f.write_text("\n".join(lines) + "\n")


LOGIN_LOG = Path.home() / ".local/share/htpc-web/logins.log"


def note_login(user, ip, remembered, agent):
    """Successful internet sign-ins, so "why did it ask for the code again?" can be answered (which browser, which way)."""
    LOGIN_LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOGIN_LOG.open("a") as f:
        f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} user={user} ip={ip} {'remembered browser' if remembered else 'code entered'} agent={agent[:200]!r}\n")


def too_many_fails(ip):
    now = time.time()
    FAILS[ip] = [t for t in FAILS.get(ip, []) if now - t < 900]
    return len(FAILS[ip]) >= 5


def record_fail(ip, user):
    FAILS.setdefault(ip, []).append(time.time())
    AUTH_LOG.parent.mkdir(parents=True, exist_ok=True)
    with AUTH_LOG.open("a") as f:
        f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} HTPC AUTH FAIL ip={ip} user={user!r}\n")
