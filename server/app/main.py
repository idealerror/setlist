"""FastAPI application (spec 3, spec 6).

Bind to the tailnet address in production. Do not port-forward; the venue
client reaches this over Tailscale, and the dashboard is exposed separately
through a Cloudflare Tunnel for mobile.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI

from .api.detections import router as detections_router
from .api.heartbeat import router as heartbeat_router
from .api.queries import router as queries_router
from .db import init_engine
from .models import utcnow

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")

app = FastAPI(title="Venue setlist", version="1.0")

app.include_router(detections_router, prefix="/api/v1", tags=["ingest"])
app.include_router(heartbeat_router, prefix="/api/v1", tags=["ingest"])
app.include_router(queries_router, prefix="/api/v1", tags=["read"])


@app.on_event("startup")
def _startup() -> None:
    init_engine()


@app.get("/health")
def health() -> dict:
    """Unauthenticated liveness probe for the container healthcheck."""
    return {"ok": True, "server_time": utcnow().isoformat()}
