"""Venue-quiet detection (spec 8).

Fires only during a window where an event is actually scheduled. A silent
Tuesday afternoon is not a fault; a silent Friday at 22:00 during a booked show
means the capture client, the interface, or the PC has died.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import ensure_utc
from ..models import Detection, Event, Heartbeat, Venue, utcnow
from .alerts import notify
from .associate import event_window


def check(db: Session, now: dt.datetime | None = None,
          fire: bool = True) -> list[dict]:
    settings = get_settings()
    now = now or utcnow()
    before = dt.timedelta(minutes=settings.association_before_minutes)
    after = dt.timedelta(minutes=settings.association_after_minutes)
    quiet_after = dt.timedelta(minutes=settings.quiet_alert_minutes)

    problems: list[dict] = []
    for venue in db.execute(select(Venue)).scalars():
        events = db.execute(
            select(Event).where(Event.venue_id == venue.id)).scalars()
        active = None
        for event in events:
            window = event_window(event, before, after, dt.timedelta(hours=6))
            if window and window[0] <= now <= window[1]:
                active = event
                break
        if active is None:
            continue

        last_detection = ensure_utc(db.execute(
            select(func.max(Detection.captured_at))
            .where(Detection.venue_id == venue.id)
        ).scalar_one_or_none())
        last_heartbeat = ensure_utc(db.execute(
            select(func.max(Heartbeat.at))
            .where(Heartbeat.venue_id == venue.id)
        ).scalar_one_or_none())

        quiet_for = None if last_detection is None else now - last_detection
        if quiet_for is not None and quiet_for <= quiet_after:
            continue

        problem = {
            "venue": venue.slug,
            "event": active.title,
            "event_id": active.id,
            "last_detection": last_detection.isoformat() if last_detection else None,
            "last_heartbeat": last_heartbeat.isoformat() if last_heartbeat else None,
            "quiet_minutes": (int(quiet_for.total_seconds() // 60)
                              if quiet_for else None),
        }
        problems.append(problem)
        if fire:
            notify("venue_quiet", problem)
    return problems
