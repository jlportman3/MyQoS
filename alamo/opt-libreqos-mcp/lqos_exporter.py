#!/usr/bin/env python3
"""
LibreQoS Prometheus exporter — exposes shaper metrics for VictoriaMetrics (→ Grafana history).
Reuses the verified collectors from libreqos_mcp.py. Run on the LibreQoS box.

Two endpoints:
  :9101/metrics          box/aggregate metrics — cheap, scrape every ~15s.
  :9101/metrics/circuits PER-CIRCUIT metrics for per-customer drill-down — computed by a
                         background thread every CIRCUIT_REFRESH s (the RTT sample is slow),
                         served from cache so the scrape never blocks. Scrape every ~60s.

Per-circuit series are labelled by a STABLE key: account (leading digits of the circuit name)
+ ip. The human name/plan live in lqos_circuit_info so renames (e.g. vendor-tag changes)
don't churn the metric series.
"""
import sys, re, threading, time
sys.path.insert(0, "/opt/libreqos-mcp")
import libreqos_mcp as lq
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 9101
CIRCUIT_REFRESH = 60          # seconds between per-circuit recomputes

_CIRCUIT_CACHE = "# per-circuit metrics warming up\n"
_CIRCUIT_LOCK = threading.Lock()


def _san(s):
    return re.sub(r'[^A-Za-z0-9_]', '_', str(s))


def _esc(s):
    """Escape a Prometheus label VALUE."""
    return str(s).replace('\\', '\\\\').replace('"', '\\"').replace('\n', ' ')


def collect():
    """Box/aggregate metrics — fast."""
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
        # HONEST dataplane signal: raw kernel rx/tx byte counters + carrier for the
        # bump-in-the-wire NICs (from lqos.conf). rate()==0 on the ACTIVE shaper = it has
        # silently fallen OUT of path while systemd still looks healthy (the .47 failure mode).
        # Verified: these counters DO increment under XDP. Read /sys directly (no bpftool dep).
        conf = open("/etc/lqos.conf").read()
        ifaces = set(re.findall(r'(?:to_internet|to_network|isp_interface|internet_interface)\s*=\s*"([^"]+)"', conf))
        for i in ifaces:
            try:
                rx = int(open(f"/sys/class/net/{i}/statistics/rx_bytes").read())
                tx = int(open(f"/sys/class/net/{i}/statistics/tx_bytes").read())
                car = open(f"/sys/class/net/{i}/carrier").read().strip()
                m("lqos_dataplane_rx_bytes_total", rx, f'iface="{i}"')
                m("lqos_dataplane_tx_bytes_total", tx, f'iface="{i}"')
                m("lqos_dataplane_carrier", 1 if car == "1" else 0, f'iface="{i}"')
            except Exception:
                pass
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


def collect_circuits():
    """PER-CIRCUIT metrics for per-customer drill-down. Slow (~10-15s: RTT sample + flow scan)."""
    out = []
    def m(name, val, labels):
        try:
            float(val)
        except (TypeError, ValueError):
            return
        out.append(f'{name}{{{labels}}} {val}')

    rtt = lq._pping_rtt()             # tc_handle -> {avg,median,max,samples}
    retr = lq._flowbee_retransmits()  # tc_handle -> count
    circ = lq._tc_to_circuit()        # tc_handle -> [name, ip, plan_dl, plan_ul, down_bytes, up_bytes]
    seen = 0
    for h in (set(circ) | set(rtt) | set(retr)):
        c = circ.get(h, [None, None, None, None, 0, 0])
        name = c[0] or ""
        ip = c[1] or ""
        # STABLE key: leading account digits of the circuit name; skip truly unmapped rows
        am = re.match(r'\s*(\d+)', name)
        account = am.group(1) if am else ""
        if not account and not ip:
            continue  # unmapped tc handle — not a customer circuit
        tc = "%x:%x" % (h >> 16, h & 0xffff)
        lab = f'account="{_esc(account)}",ip="{_esc(ip)}"'
        seen += 1
        # throughput as cumulative byte counters -> rate() in Grafana = bps
        m("lqos_circuit_bytes_total", c[4], lab + ',dir="down"')
        m("lqos_circuit_bytes_total", c[5], lab + ',dir="up"')
        # plan (gauge, Mbps)
        if c[2] is not None: m("lqos_circuit_plan_mbps", c[2], lab + ',dir="down"')
        if c[3] is not None: m("lqos_circuit_plan_mbps", c[3], lab + ',dir="up"')
        # RTT (ms) — only when we have passive samples this window
        r = rtt.get(h)
        if r:
            if r.get("avg") is not None:    m("lqos_circuit_rtt_ms", r["avg"], lab + ',stat="avg"')
            if r.get("median") is not None: m("lqos_circuit_rtt_ms", r["median"], lab + ',stat="median"')
            if r.get("max") is not None:    m("lqos_circuit_rtt_ms", r["max"], lab + ',stat="max"')
            if r.get("samples") is not None: m("lqos_circuit_rtt_samples", r["samples"], lab)
        # TCP retransmits (counter-ish over the flow window)
        m("lqos_circuit_retransmits", retr.get(h, 0), lab)
        # info: human name + tc handle for Grafana display / joins (name kept OUT of the metric labels)
        out.append(f'lqos_circuit_info{{{lab},name="{_esc(name)}",tc="{_esc(tc)}"}} 1')
    out.append(f'lqos_circuit_export_count {seen}')
    return "\n".join(out) + "\n"


def _circuit_refresher():
    global _CIRCUIT_CACHE
    while True:
        t0 = time.monotonic()
        try:
            text = collect_circuits()
        except Exception as e:
            text = f'# per-circuit collect failed: {_esc(e)}\nlqos_circuit_export_count 0\n'
        with _CIRCUIT_LOCK:
            _CIRCUIT_CACHE = text
        # keep a steady ~CIRCUIT_REFRESH cadence regardless of collect duration
        time.sleep(max(5, CIRCUIT_REFRESH - (time.monotonic() - t0)))


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/metrics":
            body = collect().encode()
        elif self.path == "/metrics/circuits":
            with _CIRCUIT_LOCK:
                body = _CIRCUIT_CACHE.encode()
        else:
            self.send_response(404); self.end_headers(); return
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *a):
        pass


if __name__ == "__main__":
    threading.Thread(target=_circuit_refresher, daemon=True).start()
    print(f"lqos_exporter on :{PORT}/metrics (+ /metrics/circuits, refresh {CIRCUIT_REFRESH}s)")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
