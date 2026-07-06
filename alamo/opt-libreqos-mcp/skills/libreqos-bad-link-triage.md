---
name: libreqos-bad-link-triage
description: Find customers with degraded connections on a LibreQoS-shaped WISP/fiber network (high latency and/or TCP loss) and decide whether it's a sick link (RF/CPE/distance — a truck roll) or just a heavy user — before they call in.
tools: [libreqos.circuit_quality, libreqos.find_circuit_for_ip, libreqos.splynx_service_for_ip, libreqos.traceroute, libreqos.arp_lookup, libreqos.cake_health]
---

# Bad-Link Triage

## When to use
- "Which customers have a bad connection / high latency / high retransmits?"
- A customer reports slowness and you want to know if it's the link, the shaper, or just volume.
- Proactive: build a prioritized truck-roll list of degraded links.

## What the data is (and isn't)
- `circuit_quality` fuses, **per circuit**: passive TCP **RTT** (`rtt_avg/max/median_ms` from xdp_pping), **TCP retransmits** (from the flowbee flow map), **usage** (`down/up_mb`), **plan**, customer name, IP, tc_handle.
- **RTT is end-to-end** (server↔customer device) measured at the shaper. The shaper's own CAKE queue adds ~5-10 ms at most (verify with `cake_health` — backlog is tiny); so **high RTT is almost always downstream of us: the customer's RF link or CPE.**
- **Passive caveat:** only circuits with **active flows during the ~9 s sample** appear. A totally idle bad customer won't show until they pass traffic. Re-run if a known-bad circuit is missing.

## The core call: sick link vs heavy user
Run it **both ways** — they surface different problems:

| Signature | Sort | Meaning | Action |
|---|---|---|---|
| **High RTT + LOW retransmits + LOW usage** | `sort_by="rtt"` | **Sick link** — RF/CPE/distance. Customer suffers at any speed. | **Truck roll / RF check** |
| High retransmits + **LOW RTT** + **HIGH usage** | `sort_by="retransmits"` | **Heavy user** — retransmits scale with volume, link is fine | None — normal |
| **High RTT *and* high retransmits** | either | Busy **and** latent — worst real experience | Investigate first |

- **RTT pegged dead-flat at 1000 ms** (avg=min=max=median=1000) = the measurement **ceiling**: true RTT is **≥1 s**. Severely broken link. Top priority.
- A customer on a big plan (100M-1G) stuck at 120-400 ms is getting a **fraction of what they pay for** — latency, not the plan, is the bottleneck.

## Procedure
1. **Sick links:** `circuit_quality(sort_by="rtt", min_rtt_ms=100, limit=20)`. Anything ≥120 ms avg (or pegged 1000) is a degraded link. Note that the highest RTT with *low* retransmits and *low* usage = classic bad RF.
2. **Rule out the shaper:** if worried it's us, `cake_health` — download/upload backlog should be tiny (a few MB). If it is, the latency is downstream (the customer's side), confirmed.
3. **Rule out "just busy":** cross-check the same customer in `circuit_quality(sort_by="retransmits")`. High RTT but also top-of-usage → busy; high RTT with little usage → genuinely sick link.
4. **Identify the customer:** `splynx_service_for_ip(ip)` → name, ONU/login, plan, address, phone.
5. **Locate the link (optional):** `traceroute(ip)` → last gateway hop; `arp_lookup(ip, <gateway>)` → MAC (CPE vendor) + ifname/VLAN/ONU.
6. **RF confirmation is off-platform:** the airOS radios expose **no general API** and ship with SNMP off; confirm signal/airtime in **UISP** (if a controller is run) or the radio UI. Typical bad-link cause: weak signal at distance → dropped modulation → airtime starvation (e.g. a LiteBeam at -74 dBm / 11.7 km).

## What to report
Per degraded customer: **name + IP**, **RTT** (avg/max — flag if pegged 1000), **retransmits**, **usage vs plan**, the **verdict** (sick link vs heavy user), and the **recommended action** (truck roll for RF/CPE realignment vs no action). Lead with the worst (pegged-RTT, low-usage) — those are customers having a miserable experience who likely haven't called yet.

## Guardrails
**Read-only.** This skill diagnoses; it never changes shaping or config. Route any remediation to the gated path.
