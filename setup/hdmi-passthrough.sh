#!/bin/bash
# Dolby Digital over HDMI: switch the audio from Intel's SOF driver (HDMI = 2ch PCM only, no passthrough) to the
# legacy HDA driver, which can send Dolby Digital to a soundbar. The laptop's built-in digital mic stops working
# (an HTPC doesn't need it). Takes effect after a reboot: this waits until the TV player (mpv) has closed, then reboots.
# Undo: sudo rm /etc/modprobe.d/htpc-legacy-hda.conf && sudo update-initramfs -u && sudo reboot
set -e
[ "$(id -u)" = 0 ] || { echo "Run it with sudo."; exit 1; }
echo "options snd-intel-dspcfg dsp_driver=1" > /etc/modprobe.d/htpc-legacy-hda.conf
update-initramfs -u >/dev/null
echo "✓ legacy HDA audio driver set for the next boot"
systemd-run --unit=htpc-reboot-after-movie --description="Reboot once the TV player closes" \
  bash -c 'while pgrep -x mpv >/dev/null; do sleep 30; done; sleep 60; systemctl reboot' >/dev/null
echo "✓ the machine reboots about a minute after the TV player closes (to cancel: sudo systemctl stop htpc-reboot-after-movie)"
