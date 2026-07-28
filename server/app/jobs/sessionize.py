"""Collapse detections into plays (spec 2.2, spec 8).

Pure derivation over the immutable detection log. Deleting the plays table and
re-running this must always reproduce it, because `play_gap_minutes` will need
retuning once there is real data to look at.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import ensure_utc
from ..models import Detection, Play, Venue, utcnow


def rebuild(db: Session, venue: Venue, since: dt.datetime | None = None,
            until: dt.datetime | None = None,
            gap_minutes: int | None = None) -> int:
    """Rebuild plays for one venue. Returns the number written."""
    gap = dt.timedelta(minutes=gap_minutes
                       if gap_minutes is not None
                       else get_settings().play_gap_minutes)

    stmt = (select(Detection)
            .where(Detection.venue_id == venue.id,
                   Detection.track_id.is_not(None)))
    if since:
        stmt = stmt.where(Detection.captured_at >= since)
    if until:
        stmt = stmt.where(Detection.captured_at <= until)

    rows = list(db.execute(stmt.order_by(Detection.captured_at)).scalars())

    by_track: dict[int, list[Detection]] = defaultdict(list)
    for row in rows:
        by_track[row.track_id].append(row)

    derived_at = utcnow()
    plays: list[Play] = []
    for track_id, entries in by_track.items():
        start = ensure_utc(entries[0].captured_at)
        last = start
        count = 1
        for det in entries[1:]:
            moment = ensure_utc(det.captured_at)
            if moment - last > gap:
                plays.append(Play(venue_id=venue.id, track_id=track_id,
                                  started_at=start, ended_at=last,
                                  detection_count=count, derived_at=derived_at))
                start, count = moment, 0
            last = moment
            count += 1
        plays.append(Play(venue_id=venue.id, track_id=track_id,
                          started_at=start, ended_at=last,
                          detection_count=count, derived_at=derived_at))

    clear = delete(Play).where(Play.venue_id == venue.id)
    if since:
        clear = clear.where(Play.started_at >= since)
    if until:
        clear = clear.where(Play.started_at <= until)

    db.execute(clear)
    db.add_all(plays)
    db.commit()
    return len(plays)
