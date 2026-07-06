import sys, ipaddress
sys.path.insert(0, "/opt/libreqos-mcp")
import libreqos_mcp as lq
from fastapi import APIRouter, Query, HTTPException
router = APIRouter(prefix="/discovery", tags=["discovery"])

def _ip(ip):
    try: ipaddress.ip_address(ip)
    except ValueError: raise HTTPException(status_code=422, detail=f"invalid IP: {ip}")
    return ip

def _priv_router(ip):
    _ip(ip)
    if not ipaddress.ip_address(ip).is_private:
        raise HTTPException(status_code=422, detail="router_ip must be a private/mgmt address")
    return ip

@router.get("/unknown-ips", summary="Unshaped IPs carrying >= min_mb, by subnet")
def unknown_ips(min_mb: float = Query(1.0, ge=0, le=100000)):
    return lq.unknown_ips(min_mb=min_mb)

@router.get("/traceroute/{ip}", summary="Traceroute to an IP (locate upstream)")
def traceroute(ip: str):
    return lq.traceroute(_ip(ip))

@router.get("/arp-lookup", summary="SNMP ARP lookup of an IP on a (private) router")
def arp_lookup(ip: str, router_ip: str):
    return lq.arp_lookup(_ip(ip), _priv_router(router_ip))

@router.get("/splynx-service/{ip}", summary="Splynx service/customer owning an IP")
def splynx_service_for_ip(ip: str):
    return lq.splynx_service_for_ip(_ip(ip))
