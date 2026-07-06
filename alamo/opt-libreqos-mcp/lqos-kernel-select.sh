#!/bin/bash
# Pick the correct lqosd binary for the running kernel BEFORE lqosd starts:
#   kernel >= 6.17 -> cpumap+frags build (per-circuit enforcement + jumbo)
#   older (e.g. 6.8 fallback) -> no-cpumap build (loads cleanly, avoids the
#   xdp.frags+cpumap -22 crash-loop; jumbo ok, enforcement degraded but the
#   box stays FUNCTIONAL rather than dead inline).
BIN=/opt/libreqos/src/bin
KVER=$(uname -r | cut -d- -f1)
if dpkg --compare-versions "$KVER" ge 6.17; then
  SRC=$BIN/lqosd.cpumap
else
  SRC=$BIN/lqosd.bak-nocpumap
fi
if [ -f "$SRC" ] && ! cmp -s "$SRC" "$BIN/lqosd"; then
  cp "$SRC" "$BIN/lqosd" && chmod +x "$BIN/lqosd"
  logger -t lqos-kernel-select "selected $(basename "$SRC") for kernel $(uname -r)"
fi
exit 0
