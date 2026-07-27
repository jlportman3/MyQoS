# Alamo self-hosted history stack (free replacement for Insight history)

The "history requires Insight" panels in the LibreQoS UI are hardwired to the paid
cloud (insight.libreqos.com) and cannot be lit locally. This is our self-hosted
equivalent: **lqos_exporter -> VictoriaMetrics -> Grafana**, all on hardware we own.

## Topology (built 2026-07-27)
- **Exporter** `../opt-libreqos-mcp/lqos_exporter.py` runs on each shaper as
  `../systemd/lqos-exporter.service` -> Prometheus metrics on **:9101/metrics**
  (throughput, pps, CPU softirq/load/steal, CAKE drops/ecn/backlog/qdiscs,
  circuits, shaped/unshaped/unknown IPs). Runs in the /opt/libreqos-mcp venv
  (needs `mcp`). Firewalled to loopback + the monitoring LXC + mgmt host.
- **Monitoring LXC** `monitoring` (CTID 950) on the beast 10.0.63.157, rootfs on
  **tank** (ZFS mirror), IP **10.0.63.152**, unprivileged, 4c/4G/32G.
  - **VictoriaMetrics** (single-node, :8428, `victoriametrics.service`), scrapes
    both shapers per `victoriametrics.scrape.yml` -> `/etc/victoriametrics/scrape.yml`,
    **12-month retention**, data on the tank-backed rootfs.
  - **Grafana** (:3000), provisioned datasource (`grafana-datasource.yml`) + dashboard
    (`lqos-history-dashboard.json` via `grafana-dashboard-provider.yml`).
    Dashboard: **LibreQoS Shaper — History** (folder LibreQoS).

## Access
- Grafana: http://10.0.63.152:3000  (admin — password set out-of-band, not in git)
- VictoriaMetrics API: http://10.0.63.152:8428 (loopback/LXC only)

## Rebuild recipe (LXC)
1. `pveam download local debian-12-standard...`; `pct create 950 <tmpl> --rootfs tank:32 --net0 ...dhcp --unprivileged 1 --onboot 1`
2. VictoriaMetrics: fetch latest single-node binary -> /usr/local/bin/victoria-metrics; install scrape.yml; victoriametrics.service; enable.
3. Grafana: apt.grafana.com repo -> `apt install grafana`; drop datasource + dashboard-provider into /etc/grafana/provisioning/{datasources,dashboards}/; dashboard JSON into /var/lib/grafana/dashboards/; enable grafana-server; set admin password via API.

## Gap / phase 2
Exporter is box/aggregate-level. Per-circuit RTT + retransmit history (what Insight
gates) is NOT emitted — would need top-N per-circuit series added to the exporter
(cardinality-bounded), or use ntopNG for flow-level history.
