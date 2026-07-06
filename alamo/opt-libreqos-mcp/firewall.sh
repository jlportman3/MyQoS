#!/bin/bash
# Lock LibreQoS MCP (8088) and read-only lqos-api (9200) to mgmt host + localhost.
# Libby-web (9201) is INTENTIONALLY open network-wide (matches the :9123 UI) — not locked here.
# Idempotent (-C guards); never fatal. Run at boot by libreqos-mcp.service.
for port in 8088 9200; do
  for src in 10.0.60.44 127.0.0.1; do
    iptables -C INPUT -p tcp --dport "$port" -s "$src" -j ACCEPT 2>/dev/null \
      || iptables -I INPUT 1 -p tcp --dport "$port" -s "$src" -j ACCEPT
  done
  iptables -C INPUT -p tcp --dport "$port" -j DROP 2>/dev/null \
    || iptables -A INPUT -p tcp --dport "$port" -j DROP
done
exit 0
