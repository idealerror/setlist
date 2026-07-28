"""Read endpoints: events, plays, top-tracks (spec 6)."""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..auth import venue_from_token
from ..db import get_db
from ..models import Detection, Event, Play, Track, Venue
from ..schemas import EventOut, PlayOut, TopTrack, TopTracks, TrackOut

router = APIRouter()


def _check_venue(requested: str, venue: Venue) -> None:
    if requested and requested != venue.slug:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail=f"Token is for venue '{venue.slug}'")


@router.get("/events", response_model=list[EventOut])
def get_events(
    venue_slug: str = Query(default="", alias="venue"),
    date_from: dt.datetime | None = Query(default=None, alias="from"),
    date_to: dt.datetime | None = Query(default=None, alias="to"),
    venue: Venue = Depends(venue_from_token),
    db: Session = Depends(get_db),
):
    _check_venue(venue_slug, venue)
    stmt = select(Event).where(Event.venue_id == venue.id)
    if date_from:
        stmt = stmt.where(Event.starts_at >= date_from)
    if date_to:
        stmt = stmt.where(Event.starts_at <= date_to)
    return list(db.execute(stmt.order_by(Event.starts_at)).scalars())


@router.get("/plays", response_model=list[PlayOut])
def get_plays(
    venue_slug: str = Query(default="", alias="venue"),
    event_id: int | None = None,
    date_from: dt.datetime | None = Query(default=None, alias="from"),
    date_to: dt.datetime | None = Query(default=None, alias="to"),
    venue: Venue = Depends(venue_from_token),
    db: Session = Depends(get_db),
):
    _check_venue(venue_slug, venue)
    stmt = select(Play).where(Play.venue_id == venue.id)
    if event_id is not None:
        stmt = stmt.where(Play.event_id == event_id)
    if date_from:
        stmt = stmt.where(Play.started_at >= date_from)
    if date_to:
        stmt = stmt.where(Play.started_at <= date_to)
    return list(db.execute(stmt.order_by(Play.started_at)).scalars())


@router.get("/stats/top-tracks", response_model=TopTracks)
def top_tracks(
    venue_slug: str = Query(default="", alias="venue"),
    date_from: dt.datetime | None = Query(default=None, alias="from"),
    date_to: dt.datetime | None = Query(default=None, alias="to"),
    limit: int = Query(default=50, ge=1, le=500),
    venue: Venue = Depends(venue_from_token),
    db: Session = Depends(get_db),
):
    _check_venue(venue_slug, venue)
    stmt = (
        select(Track,
               func.count(Play.id).label("plays"),
               func.coalesce(func.sum(Play.detection_count), 0).label("dets"))
        .join(Play, Play.track_id == Track.id)
        .where(Play.venue_id == venue.id)
        .group_by(Track.id)
        .order_by(func.count(Play.id).desc(), Track.id)
        .limit(limit)
    )
    if date_from:
        stmt = stmt.where(Play.started_at >= date_from)
    if date_to:
        stmt = stmt.where(Play.started_at <= date_to)

    return TopTracks(items=[
        TopTrack(track=TrackOut.model_validate(track),
                 play_count=plays, detection_count=int(dets))
        for track, plays, dets in db.execute(stmt).all()
    ])


@router.get("/stats/summary")
def summary(
    venue_slug: str = Query(default="", alias="venue"),
    venue: Venue = Depends(venue_from_token),
    db: Session = Depends(get_db),
):
    _check_venue(venue_slug, venue)
    detections = db.execute(
        select(func.count(Detection.id)).where(Detection.venue_id == venue.id)
    ).scalar_one()
    by_method = {
        method: count for method, count in db.execute(
            select(Detection.method, func.count(Detection.id))
            .where(Detection.venue_id == venue.id)
            .group_by(Detection.method)
        ).all()
    }
    served = by_method.get("cache", 0) + by_method.get("shazam", 0) \
        + by_method.get("nomatch", 0)
    return {
        "venue": venue.slug,
        "detections": detections,
        "by_method": by_method,
        # Spec 8 wants cache hit rate on the dashboard.
        "cache_hit_rate": (by_method.get("cache", 0) / served) if served else 0.0,
        "plays": db.execute(
            select(func.count(Play.id)).where(Play.venue_id == venue.id)
        ).scalar_one(),
        "events": db.execute(
            select(func.count(Event.id)).where(Event.venue_id == venue.id)
        ).scalar_one(),
        "unassociated_plays": db.execute(
            select(func.count(Play.id))
            .where(Play.venue_id == venue.id, Play.event_id.is_(None))
        ).scalar_one(),
    }
