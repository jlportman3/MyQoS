import sys
sys.path.insert(0, "/opt/libreqos-mcp")
import libreqos_mcp as lq
from fastapi import APIRouter, Query
router = APIRouter(prefix="/traffic", tags=["traffic"])

@router.get("/hosts", summary="Per-host byte counters {ip:[down,up,tc]}")
def traffic_hosts():
    return lq._map_traffic()

@router.get("/throughput", summary="Current shaper throughput (Gbps/pps)")
def traffic_throughput(seconds: int = Query(3, ge=1, le=30)):
    return lq.live_throughput(seconds=seconds)

@router.get("/top-talkers", summary="Top hosts by traffic")
def traffic_top_talkers(n: int = Query(20, ge=1, le=500)):
    return lq.top_talkers(n=n)
