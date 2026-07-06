---
name: libreqos-rogue-ip-investigation
description: Identify and locate an unshaped/"unknown" IP on a LibreQoS-shaped WISP/fiber network — determine whether it's a disabled/rogue customer freeloading, and exactly who and where.
tools: [libreqos.unknown_ips, libreqos.find_circuit_for_ip, libreqos.traceroute, libreqos.arp_lookup, libreqos.splynx_service_for_ip]
---

# Rogue / Unshaped IP Investigation

## When to use
- `libreqos.unknown_ips` shows a customer-public (38.x) IP carrying real traffic, or
- You're asked "who is `<IP>`?", "why is `<IP>` unshaped?", or "is someone stealing service?"

## How this network actually works (don't assume otherwise)
- The box is a **transparent bump-in-the-wire** shaper. IPs in a circuit get shaped; IPs in no circuit pass **unshaped** and show as "unknown."
- The MMR switches are a **routed L2 fabric** — the bottom switch forwards by destination IP. **There is no simple linear topology.**
- Address plan: customers = public **38.x**; backbone/mgmt = **10.x**; **169.254** = link-local (failed DHCP); **192.168/172.16** = private. Only 38.x-with-real-traffic is worth chasing.
- Known failure mode: a **disabled/stopped** customer's CPE (often a **GL.iNet**, MAC OUI `94:83:C4`) keeps a DHCP lease off the fiber router and rides free — disabled in Splynx (so it drops out of shaping → shows "unknown") but never actually cut off at the network.

## Procedure
1. **Confirm unshaped:** `find_circuit_for_ip(ip)`. If shaped → stop, it's a normal customer.
2. **Classify:** from `unknown_ips`, ignore 10.x / 169.254 / 192.168 (infra/noise). A **38.x with real bytes** is the target.
3. **Locate:** `traceroute(ip)` → note the **last gateway hop** before the target (e.g. `10.0.94.63`). That's the router it sits behind.
4. **Identify device + interface:** `arp_lookup(ip, <gateway_ip>)` → **MAC** (OUI = vendor; `94:83:C4` = GL.iNet CPE) and **ifname/ifalias** (usually a per-customer **VLAN/ONU**).
5. **Tie to the account:** `splynx_service_for_ip(ip)`.
   - **found + status `disabled`/`stopped`** → **confirmed freeloader**: a disabled customer still holding their assigned IP. Report customer #, name, ONU/login, address, plan.
   - **not found** → IP was DHCP-grabbed and Splynx never issued it (**off-grid rogue**). Use the `arp_lookup` VLAN/ONU + gateway to pinpoint, and flag the fiber-router DHCP lease for the operator.

## What to report
**Who** (customer #/name, or "off-grid"), **where** (router + VLAN/ONU + address), **device** (MAC + vendor), **how much** traffic, and the **recommended action**: Splynx-known disabled accounts are auto-capped to 200 kbps by the puller patch (`appendThrottledInactiveAccounts`); off-grid grabbers require fiber-router enforcement.

## Guardrails
**Read-only.** Never change config from this skill — route any remediation to the gated path.
