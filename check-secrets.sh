#!/bin/bash
# Fails if any real secret from this machine appears in the repo. Reads the actual values from the private files
# (they are never copied here) and searches every file that would be committed.
cd "$(dirname "$0")"
python3 - <<'EOF'
import json, re, subprocess, sys
from pathlib import Path

H = Path.home()
secrets = set()

def add(v):
    if isinstance(v, str) and len(v.strip()) >= 5:
        secrets.add(v.strip())

def read(p):
    try:
        return (H / p).read_text()
    except OSError:
        return ""

try:
    c = json.loads(read(".config/manga-request/config.json"))
    for k in ("secret", "jellyfin_key", "kodi_password", "komga_password", "password", "prowlarr_key"):
        add(c.get(k))
    for p in c.get("reserved_passwords", []):
        add(p)
except ValueError:
    pass
try:
    for u in json.loads(read(".config/manga-request/remote-users.json") or "{}").values():
        add(u.get("pw")); add(u.get("totp"))
except ValueError:
    pass
for app in ("Sonarr", "Radarr", "Prowlarr"):
    m = re.search(r"<ApiKey>(\w+)</ApiKey>", read(f".config/{app}/config.xml"))
    if m:
        add(m.group(1))
add(read(".config/htpc-ntfy-topic"))
add(read(".config/htpc-jellyfin-admin"))
for line in read(".config/ddns-update-urls").splitlines():
    add(line)
    for m in re.finditer(r"(?:password|token)=([^&\s]+)", line):
        add(m.group(1))
for m in re.finditer(r"password:\s*(\S+)", read("htpc-credentials.txt")):
    if m.group(1) != "in" and not m.group(1).startswith("("):      # "(changed on the website …)" etc. are notes, not passwords
        add(m.group(1))
for m in re.finditer(r"\b[0-9a-f]{32}\b", read("htpc-credentials.txt")):
    add(m.group(0))
for line in read(".config/aniserver-private-strings").splitlines():   # e.g. home IP, private Tailscale network name
    add(line)
# whole words that identify people or the live server (names, domain): the repo is public
words = [w.strip() for w in read(".config/aniserver-private-words").splitlines() if w.strip() and not w.startswith("#")]
private = re.compile(r"\b(?:" + "|".join(words) + r")\b", re.I) if words else None

files = subprocess.run(["git", "ls-files", "-co", "--exclude-standard", "-z"], capture_output=True, text=True).stdout.split("\0")
hits = []
for f in filter(None, files):
    try:
        text = Path(f).read_text(errors="ignore")
    except (OSError, IsADirectoryError):
        continue
    for s in secrets:
        if s in text:
            hits.append(f"{f}: contains a secret ({s[:3]}…{len(s)} chars)")
    for n, line in enumerate(text.splitlines() if "\0" not in text else [], 1):     # text files only
        if private and private.search(line):
            hits.append(f"{f}:{n}: mentions a private name ({private.search(line).group(0)[:2]}…)")
if hits:
    print("SECRET CHECK FAILED - do not commit:\n  " + "\n  ".join(sorted(set(hits))))
    sys.exit(1)
print(f"Secret check passed: {len(secrets)} known secrets and {len(words)} private words, none found in {len(files) - 1} files.")
EOF
