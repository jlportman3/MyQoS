#!/bin/bash
# Keep the shaping NICs' RX/TX rings at the hardware maximum + MTU 9216.
#
# XDP re-attach (lqosd restart, or LibreQoS.py reruns by lqos_scheduler) resets
# the rings to the driver default -> peak rx_out_of_buffer / rx_missed drops.
# That loss is small in aggregate but bursty, and at typical customer RTT it is
# enough to cap TCP well below plan. Hence a timer, not a boot-only unit.
#
# Generalised from the i40e-specific original so one copy works on every shaper:
#   - NIC names come from /etc/lqos.conf (not hardcoded)
#   - the target ring size is read from ethtool's own Pre-set maximums
#     (i40e 8160, mlx5 8192, ...) instead of being a literal
# Idempotent: only re-applies when drifted, because ethtool -G re-inits the
# ring and briefly interrupts the interface. Never fatal.
set -u
for n in $(grep -E "^to_internet|^to_network" /etc/lqos.conf 2>/dev/null | grep -oE "enp[a-z0-9]+"); do
  [ -e "/sys/class/net/$n" ] || { logger -t lqos-ring-guard "$n absent, skipped"; continue; }

  max=$(ethtool -g "$n" 2>/dev/null | awk '/Pre-set maximums/{f=1} f&&/^RX:/{print $2; exit}')
  cur=$(ethtool -g "$n" 2>/dev/null | awk '/^Current hardware/{f=1} f&&/^RX:/{print $2; exit}')
  if [ -n "${max:-}" ] && [ -n "${cur:-}" ] && [ "$cur" != "$max" ]; then
    if err=$(ethtool -G "$n" rx "$max" tx "$max" 2>&1); then
      logger -t lqos-ring-guard "$n RX/TX ring $cur -> $max"
    else
      logger -t lqos-ring-guard "$n ring resize FAILED ($cur -> $max): $err"
    fi
  fi

  m=$(cat "/sys/class/net/$n/mtu" 2>/dev/null)
  if [ -n "${m:-}" ] && [ "$m" != "9216" ]; then
    if err=$(ip link set "$n" mtu 9216 2>&1); then
      logger -t lqos-ring-guard "$n MTU $m -> 9216"
    else
      logger -t lqos-ring-guard "$n MTU change FAILED ($m -> 9216): $err"
    fi
  fi
done
exit 0
