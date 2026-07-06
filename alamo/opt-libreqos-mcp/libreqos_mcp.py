#!/usr/bin/env python3
"""
LibreQoS MCP server — exposes the LibreQoS shaper + WISP/fiber investigation
tools to NetClaw (or any MCP client) over SSE.

Built from the procedures verified deploying/troubleshooting the Alamo Broadband
LibreQoS box: it shells out to tc / bpftool / snmp / traceroute / journalctl and
reads /etc/lqos.conf + ShapedDevices.csv. Run it ON the LibreQoS box.

READ tools are safe/autonomous. The single WRITE tool (reload_shaper) is marked
[GATED] — wire it behind NetClaw's ITSM approval.

Connect NetClaw to:  http://<box-ip>:8088/sse
Env: LQOS_SNMP_COMMUNITY (default "alamo"), LQOS_MCP_PORT (default 8088)
"""
import os, re, json, time, base64, ssl, subprocess, urllib.request
try:
    import tomllib
except ModuleNotFoundError:
    tomllib = None
from mcp.server.fastmcp import FastMCP

CONF_PATH = "/etc/lqos.conf"
LQOS_DIR = "/opt/libreqos/src"
SHAPED = os.path.join(LQOS_DIR, "ShapedDevices.csv")
SNMP_COMMUNITY = os.environ.get("LQOS_SNMP_COMMUNITY", "alamo")
PORT = int(os.environ.get("LQOS_MCP_PORT", "8088"))

mcp = FastMCP("libreqos", host="0.0.0.0", port=PORT)


# ---------- helpers ----------
def _conf():
    if tomllib:
        try:
            with open(CONF_PATH, "rb") as f:
                return tomllib.load(f)
        except Exception:
            pass
    # crude fallback
    out, sect = {}, None
    try:
        for ln in open(CONF_PATH):
            ln = ln.strip()
            m = re.match(r"\[([\w.]+)\]", ln)
            if m:
                sect = m.group(1); out.setdefault(sect, {}); continue
            m = re.match(r'(\w+)\s*=\s*"?([^"]*)"?', ln)
            if m and sect:
                out[sect][m.group(1)] = m.group(2)
    except Exception:
        pass
    return out


def _nics():
    b = _conf().get("bridge", {})
    return b.get("to_internet", ""), b.get("to_network", "")


def _sh(cmd, timeout=30):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout if r.stdout else r.stderr
    except Exception as e:
        return f"ERROR: {e}"


def _splynx_get(path):
    sp = _conf().get("splynx_integration", {})
    key, sec, url = sp.get("api_key", ""), sp.get("api_secret", ""), sp.get("url", "").rstrip("/")
    if not (key and url):
        return None
    auth = base64.b64encode(f"{key}:{sec}".encode()).decode()
    req = urllib.request.Request(f"{url}/api/2.0/{path}", headers={"Authorization": f"Basic {auth}"})
    ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(req, timeout=30, context=ctx) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        return {"error": str(e)}


def _map_traffic():
    """Parse the per-host throughput BPF map -> {ip: (down_bytes, up_bytes, tc_handle)}."""
    raw = _sh(["sudo", "bpftool", "-j", "map", "dump", "pinned", "/sys/fs/bpf/map_traffic"], 60)
    try:
        data = json.loads(raw)
    except Exception:
        return {}
    if not isinstance(data, list):   # e.g. {"error": "...Permission denied"}
        return {}
    def u(b, o, n): return int.from_bytes(bytes(int(x, 16) for x in b[o:o + n]), "little")
    hosts = {}
    for e in data:
        k = [int(x, 16) for x in e["key"]]
        ip = ".".join(str(x) for x in k[12:16]) if k[:12] == [0xff] * 12 else "ipv6"
        dl = ul = tc = 0
        for cv in e["values"]:
            v = cv["value"]; dl += u(v, 0, 8); ul += u(v, 8, 8); tc = tc or u(v, 80, 4)
        d, u2, t = hosts.get(ip, (0, 0, 0))
        hosts[ip] = (d + dl, u2 + ul, t or tc)
    return hosts


def _subnet_class(ip):
    o = ip.split(".")
    if ip == "ipv6": return "IPv6"
    if len(o) < 2: return ip
    if o[0] == "10":
        if len(o) >= 3 and o[1] == "0" and o[2] == "82": return "10.0.82 Ceph cluster (storage, expected unshaped)"
        return "10.x backbone/mgmt"
    if o[0] == "169" and o[1] == "254": return "169.254 link-local (failed DHCP)"
    if o[0] in ("192", "172"): return f"{o[0]}.{o[1]} private"
    if o[0] == "38": return f"{o[0]}.{o[1]} customer-public"
    return f"{o[0]}.{o[1]}.x"


def _pping_rtt(seconds: int = 9):
    """Per-circuit RTT from xdp_pping -> {tc_handle_u32: {avg,min,max,median,samples}} (ms)."""
    out = _sh(["timeout", str(seconds), os.path.join(LQOS_DIR, "bin", "xdp_pping")], seconds + 4)
    rtt = {}
    for line in out.splitlines():
        line = line.strip().rstrip(",")
        if not line.startswith("{"):
            continue
        try:
            d = json.loads(line)
            maj, mn = d["tc"].split(":")
            rtt[(int(maj, 16) << 16) | int(mn, 16)] = d
        except Exception:
            continue
    return rtt


def _flowbee_retransmits():
    """Sum TCP retransmits per tc_handle by reading the flowbee BPF map via the bpf syscall
    (extracts only the 2 needed fields per flow -> ~6s/low-mem vs a 500MB JSON dump)."""
    import ctypes, struct
    libc = ctypes.CDLL(None, use_errno=True); SYS_bpf = 321; ATTR = 128
    def sysbpf(cmd, a): return libc.syscall(SYS_bpf, ctypes.c_int(cmd), ctypes.byref(a), ctypes.c_uint(ATTR))
    mid = None
    for ln in _sh(["sudo", "bpftool", "map", "show"], 15).splitlines():
        if re.search(r"name flowbee\b", ln):
            m = re.match(r"\s*(\d+):", ln)
            if m: mid = int(m.group(1)); break
    if mid is None: return {}
    a = ctypes.create_string_buffer(ATTR); struct.pack_into("=I", a, 0, mid)
    fd = sysbpf(14, a)            # BPF_MAP_GET_FD_BY_ID
    if fd < 0: return {}
    key = ctypes.create_string_buffer(40); nkey = ctypes.create_string_buffer(40); val = ctypes.create_string_buffer(240)
    retr = {}; have = False
    try:
        while True:
            na = ctypes.create_string_buffer(ATTR); struct.pack_into("=I", na, 0, fd)
            struct.pack_into("=Q", na, 8, ctypes.addressof(key) if have else 0)
            struct.pack_into("=Q", na, 16, ctypes.addressof(nkey))
            if sysbpf(4, na) != 0: break        # BPF_MAP_GET_NEXT_KEY -> nonzero = done
            la = ctypes.create_string_buffer(ATTR); struct.pack_into("=I", la, 0, fd)
            struct.pack_into("=Q", la, 8, ctypes.addressof(nkey)); struct.pack_into("=Q", la, 16, ctypes.addressof(val))
            if sysbpf(1, la) == 0:              # BPF_MAP_LOOKUP_ELEM
                v = val.raw
                r0, r1 = struct.unpack_from("=HH", v, 112)   # tcp_retransmits[2]
                tch = struct.unpack_from("=I", v, 208)[0]    # tc_handle
                if tch: retr[tch] = retr.get(tch, 0) + r0 + r1
            ctypes.memmove(key, nkey, 40); have = True
    finally:
        try: os.close(fd)
        except Exception: pass
    return retr


def _tc_to_circuit():
    """tc_handle -> [name, ip, plan_dl, plan_ul, down_bytes, up_bytes] via map_traffic + ShapedDevices."""
    import csv
    ip2c = {}
    try:
        with open(SHAPED, newline="") as f:
            for row in csv.reader(f):
                if len(row) > 11 and row[6] and row[0] != "Circuit ID":
                    for tok in row[6].split(","):
                        ipc = tok.split("/")[0].strip()
                        if ipc: ip2c[ipc] = (row[1] or row[3], row[10], row[11])
    except Exception:
        pass
    tc = {}
    for ip, (d, u, h) in _map_traffic().items():
        if not h: continue
        name, dl, ul = ip2c.get(ip, (None, None, None))
        cur = tc.get(h)
        if cur is None:
            tc[h] = [name, ip, dl, ul, d, u]
        else:
            cur[4] += d; cur[5] += u
            if name and not cur[0]: cur[0], cur[1], cur[2], cur[3] = name, ip, dl, ul
    return tc


# ---------- READ tools ----------
@mcp.tool()
def libreqos_status() -> dict:
    """Overall health: shaper services, queue mode, circuit count, cap decision, NIC links."""
    wan, lan = _nics()
    conf = _conf()
    q = conf.get("queues", {})
    svc = {s: _sh(["systemctl", "is-active", s]).strip() for s in ("lqosd", "lqos_scheduler", "lqos_api")}
    try:
        circuits = sum(1 for _ in open(SHAPED)) - 1
    except Exception:
        circuits = None
    cap = ""
    j = _sh(["sudo", "journalctl", "-u", "lqosd", "--no-pager", "-n", "400"], 20)
    for ln in j.splitlines():
        if "mapped circuit decision" in ln:
            cap = ln.split("WARN")[-1].strip()
    links = {}
    for nic in (wan, lan):
        if nic:
            spd = _sh(["sudo", "ethtool", nic]); m = re.search(r"Speed:\s*(\S+)", spd)
            st = _sh(["cat", f"/sys/class/net/{nic}/operstate"]).strip()
            links[nic] = f"{st}/{m.group(1) if m else '?'}"
    return {"services": svc, "queue_mode": q.get("queue_mode"), "monitor_only": q.get("monitor_only"),
            "circuits": circuits, "to_internet": wan, "to_network": lan, "links": links,
            "cap_decision": cap or "(none in recent log)"}


@mcp.tool()
def live_throughput(seconds: int = 3) -> dict:
    """Current throughput through the shaper (Gbps + pps), sampled over `seconds`."""
    wan, lan = _nics()
    def rd(n, f):
        try: return int(open(f"/sys/class/net/{n}/statistics/{f}").read())
        except Exception: return 0
    a = {n: (rd(n, "rx_bytes"), rd(n, "rx_packets")) for n in (wan, lan) if n}
    time.sleep(max(1, seconds))
    b = {n: (rd(n, "rx_bytes"), rd(n, "rx_packets")) for n in (wan, lan) if n}
    dt = max(1, seconds)
    out = {}
    for n in a:
        gbps = (b[n][0] - a[n][0]) * 8 / dt / 1e9
        pps = (b[n][1] - a[n][1]) // dt
        role = "internet/download-in" if n == wan else "customer/upload-in"
        out[n] = {"role": role, "gbps": round(gbps, 3), "pps": pps}
    return out


@mcp.tool()
def cpu_load() -> dict:
    """Box CPU pressure: load average, hottest-core softirq%, steal%, top consumers."""
    def stat():
        d = {}
        for ln in open("/proc/stat"):
            if ln.startswith("cpu") and len(ln) > 3 and ln[3].isdigit():
                p = ln.split(); d[p[0]] = list(map(int, p[1:11]))
        return d
    a = stat(); time.sleep(1); b = stat()
    soft = {}; steal = {}
    for c in b:
        diff = [b[c][i] - a[c][i] for i in range(10)]; tot = sum(diff) or 1
        soft[c] = round(100 * diff[6] / tot, 1); steal[c] = round(100 * diff[7] / tot, 1)
    hot = max(soft, key=soft.get) if soft else None
    la = open("/proc/loadavg").read().split()[:3]
    top = _sh(["bash", "-c", "top -bn1 -o %CPU | sed -n '8,12p'"]).strip()
    return {"load_avg": la, "ncpu": len(soft), "hottest_core": hot,
            "hottest_softirq_pct": soft.get(hot), "max_steal_pct": max(steal.values()) if steal else 0,
            "cores_over_85pct_soft": sum(1 for v in soft.values() if v >= 85), "top_consumers": top}


@mcp.tool()
def top_talkers(n: int = 20) -> list:
    """Top hosts by traffic from the per-host BPF map, with shaped/unshaped flag."""
    hosts = _map_traffic()
    rows = sorted(hosts.items(), key=lambda x: -(x[1][0] + x[1][1]))[:n]
    return [{"ip": ip, "down_mb": round(d / 1e6, 1), "up_mb": round(u / 1e6, 1),
             "shaped": tc != 0, "subnet": _subnet_class(ip)} for ip, (d, u, tc) in rows]


@mcp.tool()
def unknown_ips(min_mb: float = 1.0) -> dict:
    """Unshaped IPs (not in any circuit) carrying >= min_mb, classified by subnet.
    Customer-public (38.x) entries are likely disabled/rogue freeloaders to investigate."""
    hosts = _map_traffic()
    unk = [(ip, d, u) for ip, (d, u, tc) in hosts.items() if tc == 0 and (d + u) / 1e6 >= min_mb]
    unk.sort(key=lambda x: -(x[1] + x[2]))
    by_sub = {}
    for ip, d, u in unk:
        s = _subnet_class(ip); by_sub[s] = round(by_sub.get(s, 0) + (d + u) / 1e6, 1)
    return {"count": len(unk), "by_subnet_mb": by_sub,
            "top": [{"ip": ip, "down_mb": round(d / 1e6, 1), "up_mb": round(u / 1e6, 1),
                     "subnet": _subnet_class(ip)} for ip, d, u in unk[:25]]}


@mcp.tool()
def cake_health() -> dict:
    """CAKE AQM stats per direction: drops, ECN marks, current backlog (latency proxy)."""
    wan, lan = _nics(); out = {}
    for nic, label in ((lan, "download"), (wan, "upload")):
        if not nic: continue
        try:
            j = json.loads(_sh(["sudo", "tc", "-s", "-j", "qdisc", "show", "dev", nic], 30))
        except Exception:
            out[label] = {"error": "tc parse failed"}; continue
        nq = dr = mk = bl = 0
        for q in j:
            if q.get("kind") != "cake": continue
            nq += 1; dr += q.get("drops", 0); bl += q.get("backlog", 0)
            for t in q.get("tins", []): mk += t.get("ecn_mark", 0)
        out[label] = {"nic": nic, "cake_qdiscs": nq, "drops": dr, "ecn_marks": mk, "backlog_bytes": bl}
    return out


@mcp.tool()
def circuit_quality(limit: int = 20, sort_by: str = "rtt", min_rtt_ms: float = 0) -> list:
    """Per-circuit connection quality, for finding bad customer links. Fuses RTT (ms, passive
    TCP latency via xdp_pping), TCP retransmits (from the flowbee flow map), usage, plan, and
    customer name per circuit. High RTT + high retransmits with LOW usage = a degraded link
    (RF/CPE/distance), not congestion. sort_by='rtt' (default) or 'retransmits'; min_rtt_ms
    filters out healthy circuits. Returns the worst `limit`. Takes ~15-20s (RTT sample + flow scan)."""
    rtt = _pping_rtt()
    retr = _flowbee_retransmits()
    circ = _tc_to_circuit()
    rows = []
    for h in (set(rtt) | set(retr)):
        r = rtt.get(h, {})
        c = circ.get(h, [None, None, None, None, 0, 0])
        rows.append({
            "circuit": c[0] or "(unmapped tc %x:%x)" % (h >> 16, h & 0xffff),
            "ip": c[1], "tc_handle": "%x:%x" % (h >> 16, h & 0xffff),
            "rtt_avg_ms": r.get("avg"), "rtt_max_ms": r.get("max"),
            "rtt_median_ms": r.get("median"), "rtt_samples": r.get("samples"),
            "retransmits": retr.get(h, 0),
            "down_mb": round(c[4] / 1e6, 1), "up_mb": round(c[5] / 1e6, 1),
            "plan_down_mbps": c[2], "plan_up_mbps": c[3],
        })
    if min_rtt_ms:
        rows = [x for x in rows if (x["rtt_avg_ms"] or 0) >= min_rtt_ms]
    rows.sort(key=(lambda x: -(x["rtt_avg_ms"] or 0)) if sort_by == "rtt" else (lambda x: -x["retransmits"]))
    return rows[:limit]


@mcp.tool()
def find_circuit_for_ip(ip: str) -> dict:
    """Is this IP in a shaping circuit? Returns the circuit name + plan, or 'unshaped'."""
    try:
        import csv
        with open(SHAPED, newline="") as f:
            for row in csv.reader(f):
                if len(row) > 11 and row[6]:
                    for tok in row[6].split(","):
                        if tok.split("/")[0].strip() == ip:
                            return {"ip": ip, "shaped": True, "circuit": row[1],
                                    "ipv4": row[6], "down_max_mbps": row[10], "up_max_mbps": row[11]}
    except Exception as e:
        return {"error": str(e)}
    return {"ip": ip, "shaped": False, "note": "not in any circuit (unshaped/unknown)"}


@mcp.tool()
def traceroute(ip: str) -> str:
    """Traceroute to an IP (numeric + names), to locate which router/gateway it sits behind."""
    return _sh(["traceroute", "-w", "2", "-q", "1", "-m", "15", ip], 40)


@mcp.tool()
def arp_lookup(ip: str, router_ip: str) -> dict:
    """SNMP a router's ARP table for `ip` -> MAC + the interface (name/VLAN) it's learned on.
    The MAC's OUI identifies the device vendor; the interface often maps to the ONU/VLAN/customer."""
    walk = _sh(["snmpwalk", "-v2c", "-c", SNMP_COMMUNITY, "-On", "-t", "3", "-r", "1",
                router_ip, "1.3.6.1.2.1.4.22.1.2"], 30)
    mac = ifindex = None
    for ln in walk.splitlines():
        if ln.endswith("." + ip.replace(".", ".")) or ("." + ip + " ") in (ln + " ") or ln.split("=")[0].strip().endswith(ip):
            parts = ln.split("=")
            oid = parts[0].strip().lstrip(".")
            if oid.endswith(ip):
                ifindex = oid.split(".")[-5] if len(oid.split(".")) >= 6 else None
            if len(parts) > 1:
                hexb = re.findall(r"[0-9A-Fa-f]{2}", parts[1])
                if len(hexb) >= 6: mac = ":".join(hexb[:6]).upper()
            break
    res = {"ip": ip, "router": router_ip, "mac": mac, "ifindex": ifindex}
    if ifindex:
        res["ifname"] = _sh(["snmpget", "-v2c", "-c", SNMP_COMMUNITY, "-Oqv", "-t", "3",
                             router_ip, f"1.3.6.1.2.1.31.1.1.1.1.{ifindex}"]).strip().strip('"')
        res["ifalias"] = _sh(["snmpget", "-v2c", "-c", SNMP_COMMUNITY, "-Oqv", "-t", "3",
                              router_ip, f"1.3.6.1.2.1.31.1.1.1.18.{ifindex}"]).strip().strip('"')
    return res


@mcp.tool()
def splynx_service_for_ip(ip: str) -> dict:
    """Look up the Splynx service/customer owning an IP across active/disabled/stopped.
    Reveals status (active vs disabled/stopped = freeloader), customer #/name, and the ONU/login."""
    for status in ("active", "disabled", "stopped"):
        data = _splynx_get(f"admin/customers/customer/0/internet-services?main_attributes%5Bstatus%5D={status}")
        if not isinstance(data, list): continue
        for s in data:
            if str(s.get("ipv4", "")).split("/")[0] == ip or str(s.get("ipv4_route", "")).split("/")[0] == ip:
                cid = s.get("customer_id"); cust = _splynx_get(f"admin/customers/customer/{cid}")
                cust = cust[0] if isinstance(cust, list) and cust else (cust if isinstance(cust, dict) else {})
                return {"ip": ip, "found": True, "status": status, "service_id": s.get("id"),
                        "customer_id": cid, "customer_name": cust.get("name"),
                        "login_onu": s.get("login"), "plan": s.get("description"),
                        "address": cust.get("street_1"), "phone": cust.get("phone")}
    return {"ip": ip, "found": False,
            "note": "no Splynx service assigned this IP — likely DHCP-grabbed off-grid (rogue)"}


# ---------- WRITE tool (gate behind approval) ----------
@mcp.tool()
def reload_shaper() -> str:
    """[GATED-WRITE] Re-pull Splynx + rebuild/apply the shaper (runs LibreQoS.py).
    Requires approval — this re-applies live shaping. Read tools never need this."""
    return _sh(["bash", "-c", f"cd {LQOS_DIR} && sudo python3 LibreQoS.py 2>&1 | tail -8"], 180)


if __name__ == "__main__":
    mcp.run(transport="sse")
