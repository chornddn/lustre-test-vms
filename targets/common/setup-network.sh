#!/usr/bin/env bash
# Configure networking for QEMU microvm guests.
#
# VMs receive their IP, gateway, and hostname via kernel cmdline
# (parsed by rc.local). NetworkManager is kept off those interfaces so
# it doesn't fight with the cmdline-assigned addresses.
#
# Expects common/rc.local to already be present at /etc/rc.d/rc.local
# (COPY'd by the Dockerfile before this script runs).
set -euo pipefail

# rc.local: reads IP/GW/hostname from kernel cmdline at boot
mkdir -p /etc/rc.d
chmod +x /etc/rc.d/rc.local
ln -sf /etc/rc.d/rc.local /etc/rc.local

# fstab: root on /dev/vda (virtio block device)
cat > /etc/fstab <<'EOF'
/dev/vda  /  ext4  defaults,noatime  0 1
EOF

# resolv.conf: written at boot by rc.local (reads fc_ip/fc_gw from
# kernel cmdline). No need to set it here -- during container builds
# /etc/resolv.conf is a bind mount that can't be replaced anyway.

# Keep NetworkManager off every interface rc.local addresses.
#
# no-auto-default only stops NM from inventing a DHCP profile; NM still
# manages the device, and taking it over runs a deconfigure that flushes
# whatever addresses are already on it.  rc.local addresses eth0 first
# and the extra NICs one at a time, so the interface NM happens to be
# claiming at that instant loses its IPv4 address silently -- and stays
# up with an IPv6 address, which reads as a live link.
mkdir -p /etc/NetworkManager/conf.d
cat > /etc/NetworkManager/conf.d/00-ltvm.conf <<'EOF'
[main]
no-auto-default=*

[keyfile]
unmanaged-devices=interface-name:eth*
EOF

# Disable wait-online services — rc.local handles networking, so NM/systemd
# never considers the interface "online" and these just block boot for minutes.
systemctl disable NetworkManager-wait-online.service 2>/dev/null || true
systemctl disable systemd-networkd-wait-online.service 2>/dev/null || true
