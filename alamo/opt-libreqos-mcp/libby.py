#!/usr/bin/env python3
"""Libby-local: LibreQoS AI ops assistant. LiteLLM (local qwen) + the lqos-api tools (auto from OpenAPI)."""
import os, sys, json, urllib.request
LITELLM_URL = os.environ.get("LITELLM_URL", "http://10.0.60.48:4000")
LITELLM_KEY = os.environ.get("LITELLM_KEY", "")
MODEL       = os.environ.get("LIBBY_MODEL", "qwen3.6-35b-a3b")
LQOS_API    = os.environ.get("LQOS_API", "http://127.0.0.1:9200")
SYSTEM = (
  "You are Libby, the network-operations assistant for Alamo Broadband's LibreQoS shaper "
  "(cap-free, replaced Preseem; ~1280 circuits, ~6.6 Gbit/s peak on 16 cores). "
  "ALWAYS pull live telemetry with the tools before answering; never guess or invent numbers. "
  "Answer briefly and operationally, like a senior netops engineer.\n"
  "CRITICAL domain knowledge — DO NOT get these wrong:\n"
  "- CAKE drops and ECN marks are NORMAL, HEALTHY behavior. CAKE is an AQM whose JOB is to drop/mark packets "
  "per-flow to control bufferbloat. Large cumulative CAKE drop/mark counts do NOT mean congestion, packet loss, "
  "or an unhealthy shaper. NEVER report the shaper as congested or 'dropping traffic' merely because CAKE counters are high.\n"
  "- The REAL trouble signals are: NIC/driver drops, XDP drops, softirq time_squeeze growth, or RX backlog — plus "
  "sustained high RTT and high retransmits on SPECIFIC circuits. If those are absent/low, the shaper is HEALTHY even at full load.\n"
  "- Distinguish RATE from TOTAL. 'right now', 'current', 'bandwidth', 'throughput' mean an instantaneous rate in "
  "Mbit/s — use live throughput / top-talker rate fields, NOT cumulative byte counters. Cumulative bytes/drops are "
  "since-boot totals; never present a total as a current rate.\n"
  "- RTT is per-circuit; a brief few-hundred-ms spike is normal, sustained >100 ms on a circuit is worth noting.\n"
  "IP/context map: 38.x = customer public IPs (what matters for customer questions); 10.0.82.x = Ceph storage "
  "(internal, unshaped — ignore for customer bandwidth questions); 10.250.x = Splynx walled garden "
  "(unauthenticated/blocked devices); other 10.x = management. "
  "Circuit names are '<account#> <customer name> [<access-vendor>]'. "
  "If a tool errors or data is missing, say so plainly rather than inventing. When you use the documentation excerpts, cite the source.")
def http_json(url, headers=None, data=None, timeout=60):
    req = urllib.request.Request(url, data=(json.dumps(data).encode() if data is not None else None), headers=headers or {})
    if data is not None: req.add_header("Content-Type","application/json")
    with urllib.request.urlopen(req, timeout=timeout) as r: return json.load(r)
def build_tools():
    spec = http_json(LQOS_API + "/openapi.json")
    tools=[]; routes={}
    for path, methods in spec.get("paths", {}).items():
        g = methods.get("get")
        if not g: continue
        name = path.strip("/").replace("/","_").replace("{","").replace("}","").replace("-","_")
        props={}; required=[]; pathparams=[]
        for p in g.get("parameters", []):
            sch=p.get("schema", {})
            props[p["name"]]={"type": sch.get("type","string"), "description": p.get("description","") or p["name"]}
            if p.get("required"): required.append(p["name"])
            if p.get("in")=="path": pathparams.append(p["name"])
        tools.append({"type":"function","function":{"name":name,"description":(g.get("summary") or path),
            "parameters":{"type":"object","properties":props,"required":required}}})
        routes[name]=(path, pathparams)
    return tools, routes
def call_tool(routes, name, args):
    if name not in routes: return {"error": f"unknown tool {name}"}
    path, pathparams = routes[name]; q={}
    for k,v in (args or {}).items():
        if k in pathparams: path = path.replace("{"+k+"}", str(v))
        else: q[k]=v
    url = LQOS_API + path + ("?" + "&".join(f"{k}={v}" for k,v in q.items()) if q else "")
    try: return http_json(url, timeout=45)
    except Exception as e: return {"error": str(e)}
def ask(question, tools, routes, max_rounds=6):
    msgs=[{"role":"system","content":SYSTEM},{"role":"user","content":question}]
    hdr={"Authorization":"Bearer "+LITELLM_KEY}
    for _ in range(max_rounds):
        resp=http_json(LITELLM_URL+"/v1/chat/completions", hdr,
            {"model":MODEL,"messages":msgs,"tools":tools,"tool_choice":"auto","temperature":0.2}, timeout=120)
        m=resp["choices"][0]["message"]; tcs=m.get("tool_calls") or []
        msgs.append({"role":"assistant","content":m.get("content"),"tool_calls":(tcs or None)})
        if not tcs: return m.get("content") or "(no answer)"
        for tc in tcs:
            fn=tc["function"]["name"]
            try: args=json.loads(tc["function"].get("arguments") or "{}")
            except Exception: args={}
            sys.stderr.write(f"  [tool: {fn}({args})]\n")
            msgs.append({"role":"tool","tool_call_id":tc["id"],"content":json.dumps(call_tool(routes,fn,args))[:100000]})
    return "(reached max tool rounds)"
if __name__=="__main__":
    tools, routes = build_tools()
    if len(sys.argv)>1:
        print(ask(" ".join(sys.argv[1:]), tools, routes))
    else:
        print(f"Libby ready — {MODEL}, {len(tools)} tools. Ctrl-D to exit.")
        while True:
            try: q=input("\nlibby> ").strip()
            except (EOFError,KeyboardInterrupt): print(); break
            if q: print(ask(q, tools, routes))
