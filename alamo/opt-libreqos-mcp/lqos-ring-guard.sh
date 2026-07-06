#!/bin/bash
# Keep i40e RX/TX rings at max (8160) + MTU 9216. XDP re-attach (lqosd / LibreQoS.py
# reruns by lqos_scheduler) resets the ring to default 4096 -> peak rx_missed drops.
# Idempotent: only re-applies (which re-inits the ring) when it has actually drifted.
for n in enp2s0np2 enp1s0np3; do
  cur=$(ethtool -g "$n" 2>/dev/null | awk '/^Current hardware/{f=1} f&&/^RX:/{print $2; exit}')
  if [ -n "$cur" ] && [ "$cur" != "8160" ]; then
    ethtool -G "$n" rx 8160 tx 8160 2>/dev/null && logger -t lqos-ring-guard "$n RX ring $cur -> 8160"
  fi
  m=$(cat "/sys/class/net/$n/mtu" 2>/dev/null)
  if [ "$m" != "9216" ]; then
    ip link set "$n" mtu 9216 2>/dev/null && logger -t lqos-ring-guard "$n MTU $m -> 9216"
  fi
done
