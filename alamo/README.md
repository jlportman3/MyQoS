# Alamo Broadband — local add-ons (NOT built by LibreQoS)

These are the free, self-hosted tools Alamo runs alongside LibreQoS on the shaper box
(VM403) to replace the paid **Insight** tier. **They are not compiled by `build_dpkg.sh`**
and are not part of the LibreQoS build — they are deployed to the paths mirrored here and
run as their own systemd services. They live in the repo purely as the source of truth so
they survive machine rebuilds and are version-controlled with the rest of the fork.

The in-tree LibreQoS customizations (Splynx patches, Insight-nag suppression, chatbot→Libby)
are on the `alamo-customizations` branch as normal commits and DO build via `build_dpkg.sh`.
This directory is the separate, external half.

## Deploy map
| Repo path | Deploys to |
|---|---|
| `opt-libreqos-mcp/*` | `/opt/libreqos-mcp/` |
| `systemd/*.service`, `*.timer` | `/etc/systemd/system/` → `systemctl enable --now …` |
| `usr-local-bin/libby` | `/usr/local/bin/libby` |

`lqos_api.py` and `libby_web.py` need a venv at `/opt/libreqos-mcp/venv` with `fastapi` +
`uvicorn`; everything else is stdlib-only.

## Secrets are intentionally EXCLUDED (never commit these)
Create by hand on the box after deploy:
- `/opt/libreqos-mcp/.litellm_key` — the LiteLLM API key (600, readable by the service user)
- `/opt/libreqos-mcp/.litellm_env` — `LITELLM_KEY=<key>` (600 root; used via systemd `EnvironmentFile`)

Splynx API credentials are read from `/etc/lqos.conf` (`[splynx_integration]`), not stored here.

## Components
- **libreqos_mcp.py** — read-only MCP server: live shaper telemetry, top-talkers, RTT, CAKE
  health, circuit lookups, unknown-IP/Splynx helpers. `_sh()` uses list-arg subprocess (no shell).
- **lqos_api.py** + **routers/** — FastAPI read-only REST wrapper over the MCP (:9200), the free
  equivalent of Insight's Local API. Firewalled to loopback + the mgmt host.
- **libby.py** + **usr-local-bin/libby** — CLI "Libby" ops assistant (LiteLLM `qwen3.6-35b-a3b`,
  auto-discovers tools from lqos-api's OpenAPI).
- **libby_web.py** + **libby_local.html** + **libby_rag.py** — web Libby (:9201, SSE) with a
  docs-RAG index (`libby_rag.py build` → `/opt/libreqos-mcp/rag_index.json`, NOT committed —
  30 MB, regenerated). The LibreQoS "Ask Libby" nav iframes this.
- **rename_devices.py** — relabels ShapedDevices.csv circuits to `<account#> <name> [<vendor>]`;
  invoked by the `integrationSplynx.py` hook (access-device vendor wins over the customer router).
- **firewall.sh** — locks the MCP (8088) and lqos-api (9200) ports to loopback + `10.0.60.44`.
  Run at boot. NOTE `netfilter-persistent` is not installed on the box; this script is the
  persistence mechanism.
- **lqos-ring-guard.sh** — re-asserts i40e RX/TX rings (8160) + MTU 9216 when XDP resets them
  (idempotent; runs on a ~60 s timer).
- **lqos-kernel-select.sh**, **lqos-resume.sh**, **lqos_exporter.py**, **test_mcp.py**,
  **skills/** — boot/kernel helpers, a metrics exporter, an MCP smoke test, and MCP skill docs.

## Not committed
`venv/`, `__pycache__/`, `rag_index.json` (generated), `.litellm_key`, `.litellm_env`, `*.bak*`.
