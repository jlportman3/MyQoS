import libreqos_mcp as m, json
def show(label, fn):
    try:
        print(f"== {label} ==", json.dumps(fn(), default=str)[:600])
    except Exception as e:
        print(f"== {label} ERROR == {e}")
show("status", m.libreqos_status)
show("throughput", lambda: m.live_throughput(2))
show("cpu", m.cpu_load)
show("top_talkers", lambda: m.top_talkers(3))
show("unknown_ips", lambda: m.unknown_ips(1.0))
show("cake_health", m.cake_health)
show("find_circuit .113", lambda: m.find_circuit_for_ip("38.128.163.113"))
show("splynx .113", lambda: m.splynx_service_for_ip("38.128.163.113"))
