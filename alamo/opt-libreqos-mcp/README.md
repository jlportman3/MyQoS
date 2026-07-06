# LibreQoS MCP server + NetClaw skills

Turns the LibreQoS box into a set of MCP tools NetClaw (or any MCP client) can call —
so the network detective work (rogue hunts, capacity checks, freeloader audits) runs
autonomously instead of by hand.

## What's here
- `libreqos_mcp.py` — the MCP server (SSE). Read-mostly; one `[GATED-WRITE]` tool.
- `skills/` — NetClaw skill playbooks (drop into your NetClaw skills directory):
  - `libreqos-rogue-ip-investigation.md`
  - `libreqos-capacity-health-check.md`
  - `libreqos-disabled-freeloader-audit.md`

## Tools exposed
**Read (safe / autonomous):**
`libreqos_status`, `live_throughput`, `cpu_load`, `top_talkers`, `unknown_ips`,
`cake_health`, `find_circuit_for_ip`, `traceroute`, `arp_lookup`, `splynx_service_for_ip`
**Write (gate behind ITSM approval):** `reload_shaper`

## Deploy (on the LibreQoS box — it shells out to tc/bpftool/snmp/traceroute and reads /etc/lqos.conf)
```bash
sudo mkdir -p /opt/libreqos-mcp && sudo cp libreqos_mcp.py /opt/libreqos-mcp/
python3 -m venv /opt/libreqos-mcp/venv
/opt/libreqos-mcp/venv/bin/pip install mcp
# privileged commands (bpftool/journalctl/tc -s/ethtool/LibreQoS.py) use sudo — run as root,
# or as a user with passwordless sudo. tools: snmp-utils, traceroute must be installed.
sudo LQOS_SNMP_COMMUNITY=alamo /opt/libreqos-mcp/venv/bin/python /opt/libreqos-mcp/libreqos_mcp.py
```
Listens on `0.0.0.0:8088` (override `LQOS_MCP_PORT`). Splynx creds + the shaping NIC names
are read automatically from `/etc/lqos.conf`.

## Connect NetClaw
Point a NetClaw MCP server entry at the SSE endpoint:
```
http://<libreqos-box-ip>:8088/sse
```
(e.g. `http://10.0.63.50:8088/sse`). Then copy `skills/*.md` into NetClaw's skills directory.

## Security
- All tools except `reload_shaper` are read-only and safe to leave autonomous.
- `reload_shaper` re-applies live shaping — wire it behind NetClaw's ITSM gating / approval + audit.
- Restrict access to `:8088` to your NetClaw host (it has no auth of its own — front it with the
  network/firewall, or add an MCP gateway in front).
