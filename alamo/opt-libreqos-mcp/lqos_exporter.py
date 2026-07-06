#!/usr/bin/env python3
"""
LibreQoS Prometheus exporter — exposes shaper metrics at :9101/metrics for
VictoriaMetrics to scrape (→ Grafana history). Reuses the verified collectors
from libreqos_mcp.py. Run on the LibreQoS box.
"""
import sys, re
sys.path.insert(0, "/opt/libreqos-mcp")
import libreqos_mcp as lq
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 9101


def _san(s):
    return re.sub(r'[^A-Za-z0-9_]', '_', str(s))


def collect():
    out = []
    def m(name, val, labels=""):
        try:
            float(val)
        except (TypeError, ValueError):
            return
        out.append(f'{name}{{{labels}}} {val}' if labels else f'{name} {val}')

    try:
        for nic, d in lq.live_throughput(2).items():
            dr = "down" if "download" in d.get("role", "") else "up"
            m("lqos_throughput_gbps", d.get("gbps"), f'dir="{dr}"')
            m("lqos_throughput_pps", d.get("pps"), f'dir="{dr}"')
    except Exception:
        pass
    try:
        c = lq.cpu_load()
        m("lqos_load1", c.get("load_avg", [0])[0])
        m("lqos_cpu_softirq_max_pct", c.get("hottest_softirq_pct"))
        m("lqos_cpu_steal_max_pct", c.get("max_steal_pct"))
        m("lqos_cores_over_85_soft", c.get("cores_over_85pct_soft"))
        m("lqos_ncpu", c.get("ncpu"))
    except Exception:
        pass
    try:
        for dr, d in lq.cake_health().items():
            if "error" in d:
                continue
            m("lqos_cake_drops_total", d.get("drops"), f'dir="{dr}"')
            m("lqos_cake_ecn_marks_total", d.get("ecn_marks"), f'dir="{dr}"')
            m("lqos_cake_backlog_bytes", d.get("backlog_bytes"), f'dir="{dr}"')
            m("lqos_cake_qdiscs", d.get("cake_qdiscs"), f'dir="{dr}"')
    except Exception:
        pass
    try:
        import subprocess as _sp
        n = sum(1 for _ in open("/opt/libreqos/src/ShapedDevices.csv")) - 1
        m("lqos_circuits_total", n)
        up = _sp.run(["systemctl", "is-active", "lqosd"], capture_output=True, text=True).stdout.strip()
        m("lqos_service_up", 1 if up == "active" else 0, 'svc="lqosd"')
    except Exception:
        pass
    try:
        hosts = lq._map_traffic()   # single dump, used for both shaped + unknown
        m("lqos_tracked_ips", len(hosts))
        m("lqos_shaped_ips", sum(1 for _, (d, u, tc) in hosts.items() if tc != 0))
        unk = [(ip, d, u) for ip, (d, u, tc) in hosts.items() if tc == 0 and (d + u) / 1e6 >= 0.1]
        m("lqos_unknown_ips", len(unk))
        bysub = {}
        for ip, d, u in unk:
            s = lq._subnet_class(ip); bysub[s] = bysub.get(s, 0) + (d + u) / 1e6
        for s, mb in bysub.items():
            m("lqos_unknown_mb", round(mb, 1), f'subnet="{_san(s)}"')
    except Exception:
        pass
    return "\n".join(out) + "\n"


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/metrics":
            body = collect().encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404); self.end_headers()
    def log_message(self, *a):
        pass


if __name__ == "__main__":
    print(f"lqos_exporter on :{PORT}/metrics")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
