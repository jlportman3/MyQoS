import sys, ipaddress
sys.path.insert(0, "/opt/libreqos-mcp")
import libreqos_mcp as lq
from fastapi import APIRouter, Query, HTTPException
router = APIRouter(prefix="/circuits", tags=["circuits"])

def _ip(ip):
    try: ipaddress.ip_address(ip)
    except ValueError: raise HTTPException(status_code=422, detail=f"invalid IP: {ip}")
    return ip

@router.get("/rtt", summary="Per-circuit passive TCP RTT (ms) by tc handle")
def rtt(seconds: int = Query(9, ge=1, le=30)):
    return lq._pping_rtt(seconds=seconds)

@router.get("/retransmits", summary="TCP retransmits per tc handle")
def retransmits():
    return lq._flowbee_retransmits()

@router.get("/by-tc-handle", summary="Map tc handle to circuit/IP/plan/usage")
def by_tc_handle():
    return lq._tc_to_circuit()

@router.get("/quality", summary="Per-circuit quality (RTT+retransmits+usage)")
def quality(limit: int = Query(20, ge=1, le=500), sort_by: str = "rtt", min_rtt_ms: float = Query(0, ge=0, le=10000)):
    return lq.circuit_quality(limit=limit, sort_by=sort_by, min_rtt_ms=min_rtt_ms)

@router.get("/for-ip/{ip}", summary="Find shaping circuit for an IP")
def for_ip(ip: str):
    return lq.find_circuit_for_ip(_ip(ip))
