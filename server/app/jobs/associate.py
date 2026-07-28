"""Attach plays to the event they happened during (spec 8).

This is the query-time join that spec 2.1 insists on: capture never waited for
the scraper, so association is free to run later, be re-run after the scraper
backfills, and be retuned without touching a single detection.

A play that matches no event stays unassociated. That is a valid and expected
state -- an unlisted show, a soundcheck, or a scraper that has not caught up --
not an error.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import ensure_utc
from ..models import Event, Play, Venue


def event_window(event: Event, before: dt.timedelta, after: dt.timedelta,
                 default_length: dt.timedelta) -> tuple[dt.datetime, dt.datetime] | None:
    """The widened span during which a detection counts as part of this event."""
    start = ensure_utc(event.doors_at) or ensure_utc(event.starts_at)
    if start is None:
        return None
    end = ensure_utc(event.ends_at)
    if end is None:
        # Most listings give no end time; assume a typical event length rather
        # than dropping the event from association entirely.
        end = (ensure_utc(event.starts_at) or start) + default_length
    return start - before, end + after


def run(db: Session, venue: Venue, since: dt.datetime | None = None,
        until: dt.datetime | None = None) -> dict:
    settings = get_settings()
    before = dt.timedelta(minutes=settings.association_before_minutes)
    after = dt.timedelta(minutes=settings.association_after_minutes)
    default_length = dt.timedelta(hours=6)

    events = list(db.execute(
        select(Event).where(Event.venue_id == venue.id)).scalars())
    windows = []
    for event in events:
        window = event_window(event, before, after, default_length)
        if window:
            windows.append((window[0], window[1], event.id))
    # Later-starting events win ties, which is what you want when a listing
    # has an all-day "opening hours" entry overlapping a specific show.
    windows.sort(key=lambda w: w[0])

    stmt = select(Play).where(Play.venue_id == venue.id)
    if since:
        stmt = stmt.where(Play.started_at >= since)
    if until:
        stmt = stmt.where(Play.started_at <= until)
    plays = list(db.execute(stmt).scalars())

    matched = unmatched = changed = 0
    for play in plays:
        started = ensure_utc(play.started_at)
        found = None
        for window_start, window_end, event_id in windows:
            if window_start <= started <= window_end:
                found = event_id
        if found != play.event_id:
            play.event_id = found
            changed += 1
        if found is None:
            unmatched += 1
        else:
            matched += 1

    db.commit()
    return {"venue": venue.slug, "plays": len(plays), "matched": matched,
            "unmatched": unmatched, "changed": changed,
            "events_considered": len(windows)}
