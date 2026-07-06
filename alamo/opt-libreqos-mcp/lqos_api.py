"""LibreQoS Local API - read-only telemetry over libreqos_mcp (free native alternative to Insight)."""
import os, hmac
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import RedirectResponse
from routers.traffic import router as traffic_router
from routers.circuits import router as circuits_router
from routers.discovery import router as discovery_router
from routers.system import router as system_router

def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    expected = os.environ.get("LQOS_API_KEY")
    if not expected:
        return
    if not x_api_key or not hmac.compare_digest(x_api_key, expected):
        raise HTTPException(status_code=401, detail="Invalid or missing API key.",
                            headers={"WWW-Authenticate": "X-API-Key"})

app = FastAPI(title="LibreQoS Local API", description="Read-only telemetry over libreqos_mcp",
              version="1.0", dependencies=[Depends(require_api_key)])
app.include_router(traffic_router)
app.include_router(circuits_router)
app.include_router(discovery_router)
app.include_router(system_router)

@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse(url="/docs")
