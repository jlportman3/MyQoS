#!/usr/bin/env bash
# lqos-path-monitor.sh — guard against the "healthy under systemd, shaping nothing" trap.
#
# Two conditions, opposite directions (both were invisible to systemctl on .47 for 2+ months):
#   1) an OUT-OF-PATH box's dataplane RX starts MOVING  -> it silently re-entered the path
#      (dangerous: it shapes with a stale flat config / empty network.json).
#   2) the ACTIVE shaper's dataplane RX STALLS           -> it fell out of path while healthy
#      (the costly one — customers uncapped/unshaped).
#
# The kernel rx_bytes counter is the only honest signal (verified: it DOES increment under
# LibreQoS XDP). Carrier is NOT sufficient — the backup shaper sits carrier-UP but ~idle, so
# we key on RX VOLUME with a byte threshold, and debounce the stall case over N samples so a
# genuinely quiet minute doesn't false-alarm.
#
# NOTE: the primary detector is now the exporter metric lqos_dataplane_rx_bytes_total in
# VictoriaMetrics (scraped from .156/.50) + a Grafana alert. This script is the Grafana-
# independent watchdog AND the home for the .47 check (which needs SSH access to .47 that we
# do not currently have — add a target line below once a key is in place).
#
# Deploy: cron/systemd-timer every 5 min on a host that can SSH to the targets.
# Alerting: logs to journal via `logger`; if LQOS_ALERT_WEBHOOK is set, POSTs the alert text.
set -euo pipefail

STATE="${LQOS_MON_STATE:-/var/lib/lqos-path-monitor}"
SSH_OPTS="-o BatchMode=yes -o ConnectTimeout=6 -o StrictHostKeyChecking=accept-new"
MIN_MOVING_BYTES="${LQOS_MIN_MOVING_BYTES:-1000000}"   # >1 MB/interval = "traffic is flowing"
STALL_SAMPLES="${LQOS_STALL_SAMPLES:-3}"               # consecutive stalled samples before firing
mkdir -p "$STATE"

# target: LABEL|HOST|EXPECT(moving|frozen)|IFACE1,IFACE2
#   moving = ALERT if RX stays below MIN_MOVING_BYTES for STALL_SAMPLES in a row (active-shaper stall)
#   frozen = ALERT the moment RX moves at all (out-of-path box re-entered the path)
TARGETS=(
  "active-156|10.0.63.156|moving|enp1s0np0,enp2s0np1"
  "backup-50|10.0.63.50|frozen|enp2s0np2,enp1s0np3"   # backup should be idle; RX surge => it went active
  # "srcctl-47|10.0.60.47|frozen|<ifaceA>,<ifaceB>"   # ENABLE once SSH access to .47 exists (out of path since 2026-06-27)
)

alert() {  # $1=text
  logger -t lqos-path-monitor "$1"
  echo "ALERT: $1" >&2
  [ -n "${LQOS_ALERT_WEBHOOK:-}" ] && curl -fsS --max-time 10 -X POST \
    -H 'Content-Type: application/json' \
    -d "$(printf '{"source":"lqos-path-monitor","alert":"%s"}' "$1")" \
    "$LQOS_ALERT_WEBHOOK" >/dev/null 2>&1 || true
}

rx_sum() {  # $1=host  $2=csv-ifaces -> summed rx_bytes (or empty on failure)
  local paths; paths=$(printf '/sys/class/net/%s/statistics/rx_bytes ' ${2//,/ })
  ssh $SSH_OPTS "baron@$1" "cat $paths 2>/dev/null" | paste -sd+ | bc 2>/dev/null || true
}

for t in "${TARGETS[@]}"; do
  IFS='|' read -r label host expect ifaces <<<"$t"
  now=$(rx_sum "$host" "$ifaces")
  if [ -z "$now" ]; then alert "$label ($host) UNREACHABLE — cannot read dataplane RX"; continue; fi
  pf="$STATE/$label.rx"; cf="$STATE/$label.stall"
  if [ ! -f "$pf" ]; then echo "$now" >"$pf"; echo 0 >"$cf"; continue; fi
  prev=$(<"$pf"); echo "$now" >"$pf"
  delta=$(( now - prev )); (( delta < 0 )) && delta=0   # counter reset/reboot -> treat as no-info
  if [ "$expect" = frozen ]; then
    (( delta > MIN_MOVING_BYTES )) && alert "$label ($host) RX MOVED +${delta}B this interval — an out-of-path box is passing traffic (check its lqos.conf/network.json)"
  else # moving
    n=$(<"$cf")
    if (( delta < MIN_MOVING_BYTES )); then n=$(( n + 1 )); else n=0; fi
    echo "$n" >"$cf"
    (( n >= STALL_SAMPLES )) && alert "$label ($host) RX STALLED (<${MIN_MOVING_BYTES}B for ${n} samples) — ACTIVE shaper may be out of path; customers may be unshaped"
  fi
done
