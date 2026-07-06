#!/bin/bash
for n in $(grep -E "^to_internet|^to_network" /etc/lqos.conf | grep -oE "enp[a-z0-9]+"); do
  ip link set "$n" up
  ip link set "$n" mtu 9216
  ethtool -G "$n" rx 8192 tx 8192 2>/dev/null
done
sleep 3
cd /opt/libreqos/src && python3 LibreQoS.py
