---
name: libreqos-capacity-health-check
description: Assess whether the LibreQoS shaper box is healthy and has CPU/throughput headroom, or is approaching saturation. Use for routine checks, before/after cutovers, or to triage customer experience complaints.
tools: [libreqos.libreqos_status, libreqos.cpu_load, libreqos.live_throughput, libreqos.cake_health]
---

# LibreQoS Capacity & Health Check

## When to use
Routine health, "is the box OK?", before/after an RSTP cutover, or investigating customer-experience complaints (latency, bufferbloat, slowness).

## Procedure
1. `libreqos_status` — services active? `queue_mode=shape` + `monitor_only=false`? circuits loaded? links up at expected speed? cap decision shows `dropped=0, effective_limit=unlimited`?
2. `live_throughput` — current down/up Gbps + pps.
3. `cpu_load` — load average, hottest-core softirq%, steal%, count of cores over 85%.
4. `cake_health` — backlog (queuing-latency proxy) + drops/marks per direction.

## What to look for / how to decide
- **LibreQoS is single-thread-bound.** The metric that matters is the **hottest-core softirq%**, NOT the average. One core near 95–100% is a bottleneck even if load average looks fine.
- **Healthy:** hottest-core softirq < ~70% at current load, low/stable backlog, steal ≈ 0. Estimate peak headroom: `hottest_softirq% × (peak_Gbps ÷ current_Gbps)`.
- **Warning:** hottest core ≥ 85% or backlog climbing → approaching saturation; recommend a bigger/faster box or moving the active shaper.
- **High steal%** = the Proxmox host is CPU-contended (a hypervisor problem, separate from LibreQoS).
- **CAKE drops are normal** (that's how it enforces plans); **rising backlog** is the red flag (latency building).

## Reference points (this deployment)
- Xeon **D-1518** (4c/8t): ~87% hottest-core + load 9 at only **1.9 Gbps** → saturated, replaced.
- Dual **E5-2620 v4** (16c/32t, the Dell): ~22% hottest-core at **3 Gbps** → comfortable; peak (6.3 G) projects to ~45%.

## Guardrails
**Read-only.**
