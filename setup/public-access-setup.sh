#!/bin/bash
# Public HTTPS access for Jellyfin + the HTPC website (everything else stays Tailscale-only).
# Run with: sudo bash ~/public-access-setup.sh <website-hostname> <jellyfin-hostname>
#   e.g.    sudo bash ~/public-access-setup.sh htpc.example.com watch-htpc.example.com   (FreeDNS, DuckDNS, any DNS)
set -e
SITE="${1:?usage: sudo bash ~/public-access-setup.sh <website-hostname> <jellyfin-hostname>}"
WATCH="${2:?usage: sudo bash ~/public-access-setup.sh <website-hostname> <jellyfin-hostname>}"
U=${SUDO_USER:?run this with sudo from your normal account}
H=$(getent passwd "$U" | cut -d: -f6)
JF_LOGS=$H/.local/share/jellyfin/log
WEB_LOG=$H/.local/share/htpc-web/auth.log

# --- Caddy (automatic HTTPS certificates) from the official repository ---
apt-get install -y debian-keyring debian-archive-keyring apt-transport-https curl gpg
curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt > /etc/apt/sources.list.d/caddy-stable.list
apt-get update
apt-get install -y caddy fail2ban

cat > /etc/caddy/Caddyfile <<EOF
# Only these two names are served. Anything else (IP scans, other names) gets no certificate and no answer.
(hardening) {
	header {
		Strict-Transport-Security "max-age=31536000"
		X-Content-Type-Options "nosniff"
		Referrer-Policy "same-origin"
		-Server
	}
}

# HTPC website (anime/movie/manga requests): username + password + authenticator code from the internet
$SITE {
	import hardening
	header X-Frame-Options "DENY"
	reverse_proxy 127.0.0.1:5050
}

# Jellyfin (watching); the admin account is blocked from the internet inside Jellyfin
$WATCH {
	import hardening
	reverse_proxy 127.0.0.1:8096
}
EOF
caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
systemctl enable caddy
systemctl restart caddy

# --- SSH hardening (port 22 is forwarded): keys only from the internet, passwords still OK at home/Tailscale ---
# Refuse to lock you out: only switch off passwords if a key is installed for you.
if [ -s "$H/.ssh/authorized_keys" ]; then
  cat > /etc/ssh/sshd_config.d/01-htpc-hardening.conf <<EOF
# Loaded before Ubuntu's own drop-ins, so these values win.
PermitRootLogin no
AllowUsers $U
PubkeyAuthentication yes
PasswordAuthentication no
KbdInteractiveAuthentication no
MaxAuthTries 3
LoginGraceTime 20
X11Forwarding no

# Home network and Tailscale may still use the laptop password
Match Address 10.0.0.0/24,100.64.0.0/10,127.0.0.1
    PasswordAuthentication yes
EOF
  sshd -t
  systemctl try-reload-or-restart ssh.service || true   # socket-activated on Ubuntu; new connections read the config anyway
  echo "SSH: key-only from the internet, passwords allowed at home/Tailscale"
else
  echo "SSH: NOT hardened yet - no key in $H/.ssh/authorized_keys (don't forward port 22 until this is done)"
fi

# --- fail2ban: ban an IP for 1 hour after 5 failed logins in 15 minutes; repeat offenders for a week ---
mkdir -p "$(dirname "$WEB_LOG")"; touch "$WEB_LOG"; chown -R "$U:$U" "$(dirname "$WEB_LOG")"
cat > /etc/fail2ban/filter.d/jellyfin.conf <<'EOF'
[Definition]
failregex = Authentication request for .* has been denied \(IP: "<ADDR>"\)
EOF
cat > /etc/fail2ban/filter.d/htpc-web.conf <<'EOF'
[Definition]
failregex = HTPC AUTH FAIL ip=<ADDR>
EOF
cat > /etc/fail2ban/jail.d/htpc.conf <<EOF
[DEFAULT]
# never ban the home network or Tailscale
ignoreip = 127.0.0.1/8 ::1 10.0.0.0/24 100.64.0.0/10 fd7a:115c:a1e0::/48
findtime = 15m
maxretry = 5
bantime = 1h
backend = auto

[jellyfin]
enabled = true
port = 443
filter = jellyfin
logpath = $JF_LOGS/log_*.log

[htpc-web]
enabled = true
port = 443
filter = htpc-web
logpath = $WEB_LOG

[sshd]
enabled = true
port = 22
backend = systemd
maxretry = 3

[recidive]
enabled = true
bantime = 1w
findtime = 1d
maxretry = 3
EOF
systemctl enable fail2ban
systemctl restart fail2ban
sleep 3

echo
echo "website: https://$SITE   jellyfin: https://$WATCH"
systemctl is-active caddy fail2ban
fail2ban-client status | tail -2
echo "Done. Tell Claude to continue."
