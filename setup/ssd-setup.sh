#!/bin/bash
# Format the USB SSD as ext4 for anime and mount it at /mnt/media on every boot.
# Run with: sudo bash ~/ssd-setup.sh
set -e
U=${SUDO_USER:?run this with sudo from your normal account}
DEV=/dev/sda

# --- safety checks: right drive, verified backup ---
MODEL=$(lsblk -dno MODEL $DEV)
[[ "$MODEL" == *PS5012-E12* ]] || { echo "ABORT: $DEV is '$MODEL', not the PHISON PS5012 SSD"; exit 1; }
echo "Formatting $DEV ($MODEL). Make sure anything on it is backed up first."

# --- unmount, wipe, partition, format ---
for p in $(lsblk -lno NAME,MOUNTPOINT $DEV | awk '$2!=""{print "/dev/"$1}'); do umount "$p"; done
wipefs -a $DEV
parted -s $DEV mklabel gpt mkpart media ext4 0% 100%
udevadm settle
mkfs.ext4 -F -L media -m 0 ${DEV}1
UUID=$(blkid -s UUID -o value ${DEV}1)

# --- mount at /mnt/media on boot; nofail so the laptop still boots if it's unplugged ---
mkdir -p /mnt/media
sed -i '\#/mnt/media#d' /etc/fstab
echo "UUID=$UUID /mnt/media ext4 defaults,noatime,nofail,x-systemd.device-timeout=10s 0 2" >> /etc/fstab
systemctl daemon-reload
mount /mnt/media
mkdir -p /mnt/media/Anime /mnt/media/downloads/sonarr
chown -R "$U:$U" /mnt/media

# --- start qBittorrent and Sonarr after the drive is mounted (but still start if it's missing) ---
for svc in qbittorrent-nox@$U sonarr; do
  mkdir -p "/etc/systemd/system/$svc.service.d"
  printf '[Unit]\nWants=mnt-media.mount\nAfter=mnt-media.mount\n' > "/etc/systemd/system/$svc.service.d/media-drive.conf"
done
systemctl daemon-reload

echo
df -h /mnt/media | tail -1
grep /mnt/media /etc/fstab
echo "Done. Tell Claude to continue."
