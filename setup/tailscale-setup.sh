#!/bin/bash
# Install Tailscale on the HTPC so the website, Sonarr etc. work from your phone anywhere (private, nothing exposed).
# Run with: sudo bash ~/tailscale-setup.sh   - then open the login link it prints.
set -e
U=${SUDO_USER:?run this with sudo from your normal account}
curl -fsSL https://tailscale.com/install.sh | sh
systemctl enable --now tailscaled
# let the normal user run "tailscale status" etc. without sudo
tailscale set --operator="$U" || true
echo
echo ">>> Open the link below in a browser and log in (Google/Microsoft/GitHub). Leave this window open until it says Success."
# --ssh: Tailscale SSH, so "ssh <you>@<this machine>" works from any of your Tailscale devices, anywhere (login = your Tailscale account)
tailscale up --operator="$U" --hostname="$(hostname -s)" --ssh
echo
tailscale status | head -5
echo "Done. Tell Claude to continue."
