"""POST /api/v1/heartbeat (spec 6, spec 8)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ..auth import venue_from_token
from ..db import get_db
from ..jobs.alerts import notify
from ..models import Heartbeat, Venue, utcnow
from ..schemas import HeartbeatIn, HeartbeatResult

router = APIRouter()


@router.post("/heartbeat", response_model=HeartbeatResult)
def post_heartbeat(
    body: HeartbeatIn,
    venue: Venue = Depends(venue_from_token),
    db: Session = Depends(get_db),
) -> HeartbeatResult:
    if body.venue != venue.slug:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Token is for venue '{venue.slug}'")

    db.add(Heartbeat(
        venue_id=venue.id,
        client_version=body.client_version,
        queue_depth=body.queue_depth,
        uptime_s=body.uptime_s,
    ))
    db.commit()

    if body.ceiling_tripped:
        # Fire on the heartbeat that reports it: the daily ceiling is
        # client-side state the server cannot otherwise observe (spec 8).
        notify("api_ceiling_tripped", {
            "venue": venue.slug,
            "client_version": body.client_version,
            "queue_depth": body.queue_depth,
        })

    return HeartbeatResult(server_time=utcnow())
