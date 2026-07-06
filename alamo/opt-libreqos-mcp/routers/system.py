import sys
sys.path.insert(0, "/opt/libreqos-mcp")
import libreqos_mcp as lq
from fastapi import APIRouter
router = APIRouter(prefix="/system", tags=["system"])

@router.get("/status", summary="Overall shaper health/services")
def system_status():
    return lq.libreqos_status()

@router.get("/cpu-load", summary="CPU pressure: load, softirq, steal")
def cpu_load():
    return lq.cpu_load()

@router.get("/cake-health", summary="CAKE AQM stats per direction")
def cake_health():
    return lq.cake_health()
