---
name: libreqos-disabled-freeloader-audit
description: Sweep a LibreQoS network for disabled/stopped customers still passing traffic and report who is riding free. Revenue-protection audit.
tools: [libreqos.unknown_ips, libreqos.splynx_service_for_ip, libreqos.traceroute, libreqos.arp_lookup]
---

# Disabled-Customer (Freeloader) Audit

## When to use
Periodic revenue-protection sweep, or "who's getting free service right now?"

## Procedure
1. `unknown_ips(min_mb=1)` — pull unshaped IPs carrying real traffic.
2. Keep only **customer-public (38.x)** entries (10.x / 169.254 / 192.168 are infra/noise — skip).
3. For each candidate: `splynx_service_for_ip(ip)`:
   - **status `disabled`/`stopped`** → freeloader using their assigned IP. The puller patch (`appendThrottledInactiveAccounts`) already names them `DISABLED <name>` and caps them to **200 kbps** — confirm they show capped (`find_circuit_for_ip` ceil 200 k); if not, recommend a `reload_shaper` (gated).
   - **not found in Splynx** → off-grid DHCP-grabber. `traceroute` + `arp_lookup` to pin the router / VLAN / ONU / MAC for manual follow-up.

## What to report
A table — **IP · customer #/name (or "off-grid") · status · ONU/VLAN · traffic · throttled? (y/n)** — plus a total count and rough bandwidth being given away.

## Root cause / escalation (don't fix from here)
Splynx is *supposed* to redirect non-active CPE to a captive-portal signup page, but it isn't firing: these devices DHCP straight off the fiber router and **never traverse Splynx auth/RADIUS**, so Splynx can neither redirect nor shape them. The durable fix is enforcing suspension downstream (drop ONU / deny DHCP / blackhole route) — **flag it for the operator; do not attempt it.**

## Guardrails
**Read-only.** The 200 kbps throttle is applied automatically by the puller; this skill only *reports*.
