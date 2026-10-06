#!/bin/bash
# Faster, steadier streaming to devices away from home (campus Wi-Fi, phones on LTE):
# - BBR congestion control (what YouTube/Netflix servers use): keeps full speed when the far end's Wi-Fi drops packets,
#   where Linux's default (cubic) slows to a crawl.
# - fq pacing (BBR's partner), and no speed reset after short pauses between video chunks.
# Run with: sudo bash ~/streaming-tuning.sh
set -e
modprobe tcp_bbr
echo tcp_bbr > /etc/modules-load.d/htpc-bbr.conf
cat > /etc/sysctl.d/90-htpc-streaming.conf <<'EOF'
net.core.default_qdisc = fq
net.ipv4.tcp_congestion_control = bbr
net.ipv4.tcp_slow_start_after_idle = 0
net.ipv4.tcp_notsent_lowat = 131072
net.core.wmem_max = 16777216
net.core.rmem_max = 16777216
net.ipv4.tcp_wmem = 4096 65536 16777216
net.ipv4.tcp_rmem = 4096 131072 16777216
EOF
sysctl --system >/dev/null
for dev in $(ls /sys/class/net | grep -v '^lo$'); do tc qdisc replace dev "$dev" root fq 2>/dev/null || true; done
systemctl restart caddy
echo
sysctl net.ipv4.tcp_congestion_control net.core.default_qdisc net.ipv4.tcp_slow_start_after_idle
echo "Done. Tell Claude to continue."
