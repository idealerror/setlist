"""Derive `plays` from `detections` (spec 2.2, spec 8).

This is pure derivation. It reads the append-only detection log and rewrites
the plays table; it never modifies detections. Re-running it after retuning
`play_gap_minutes` is the whole point, so it must be idempotent over any range.

Grouping is per track rather than over the raw sequence: a brief
misidentification in the middle of a song would otherwise split one play into
three. Spec 8's rule -- "the same track reappearing after a gap exceeding the
threshold is a new play" -- is applied to each track's own timeline.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from collections import defaultdict

from . import storage


def _parse(stamp: str) -> dt.datetime:
    moment = dt.datetime.fromisoformat(stamp)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.timezone.utc)
    return moment


def rebuild(conn: sqlite3.Connection, gap_minutes: float,
            since: str | None = None, until: str | None = None) -> int:
    """
    Rebuild plays from detections. Returns the number of plays written.

    A play is only cleared and rewritten if it starts inside [since, until],
    so a bounded backfill leaves the rest of the table alone.
    """
    gap = dt.timedelta(minutes=gap_minutes)

    where = ["shazam_key IS NOT NULL"]
    params: list[str] = []
    if since:
        where.append("captured_at >= ?")
        params.append(since)
    if until:
        where.append("captured_at <= ?")
        params.append(until)

    rows = conn.execute(
        "SELECT captured_at, shazam_key, title, artist FROM detections"
        f" WHERE {' AND '.join(where)} ORDER BY captured_at",
        params,
    ).fetchall()

    by_track: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        by_track[row["shazam_key"]].append(row)

    plays: list[tuple] = []
    derived_at = storage.iso(storage.utc_now())
    for key, entries in by_track.items():
        start = _parse(entries[0]["captured_at"])
        last = start
        count = 1
        title, artist = entries[0]["title"], entries[0]["artist"]
        for row in entries[1:]:
            moment = _parse(row["captured_at"])
            if moment - last > gap:
                plays.append((key, title, artist, storage.iso(start),
                              storage.iso(last), count, derived_at))
                start, count = moment, 0
            last = moment
            count += 1
            # Keep the most recent labelling; titles can differ between a
            # cache hit and a later API refresh of the same key.
            title, artist = row["title"] or title, row["artist"] or artist
        plays.append((key, title, artist, storage.iso(start),
                      storage.iso(last), count, derived_at))

    delete_where = []
    delete_params: list[str] = []
    if since:
        delete_where.append("started_at >= ?")
        delete_params.append(since)
    if until:
        delete_where.append("started_at <= ?")
        delete_params.append(until)
    clause = f" WHERE {' AND '.join(delete_where)}" if delete_where else ""

    with conn:  # one transaction: never leave plays half-rebuilt
        conn.execute(f"DELETE FROM plays{clause}", delete_params)
        conn.executemany(
            "INSERT INTO plays (shazam_key, title, artist, started_at,"
            " ended_at, detection_count, derived_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            plays,
        )
    return len(plays)
