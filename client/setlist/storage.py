"""Local SQLite -- the client's source of truth (spec 2.3).

Two things matter structurally:

* ``detections`` is append-only and immutable, one row per successful
  recognition poll. ``plays`` is *derived* from it by sessionize.py and can be
  thrown away and rebuilt whenever the gap threshold is retuned (spec 2.2).
* ``detections.id`` is a client-generated UUID, which is what makes the
  eventual server sync idempotent under retry (spec 4.2).
"""

from __future__ import annotations

import csv
import datetime as dt
import sqlite3
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

SCHEMA_VERSION = 2

#: A recognition poll that produced a row. 'shazam' and 'nomatch' each consumed
#: an API call; 'cache' was served locally.
METHODS = ("shazam", "cache", "nomatch")


@dataclass
class Detection:
    id: str
    captured_at: str
    shazam_key: str | None
    title: str | None
    artist: str | None
    isrc: str | None
    level_dbfs: float
    method: str
    client_version: str


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso(moment: dt.datetime) -> str:
    return moment.astimezone(dt.timezone.utc).isoformat(timespec="seconds")


def new_id() -> str:
    return str(uuid.uuid4())


# ----------------------------------------------------------------------
# schema
# ----------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS detections (
    id             TEXT PRIMARY KEY,
    captured_at    TEXT NOT NULL,
    shazam_key     TEXT,
    title          TEXT,
    artist         TEXT,
    isrc           TEXT,
    level_dbfs     REAL NOT NULL,
    method         TEXT NOT NULL,
    client_version TEXT NOT NULL,
    synced_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_detections_unsynced
    ON detections(synced_at) WHERE synced_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_detections_captured ON detections(captured_at);

CREATE TABLE IF NOT EXISTS fp_cache (
    id         INTEGER PRIMARY KEY,
    hash       INTEGER NOT NULL,
    offset_s   REAL NOT NULL,
    shazam_key TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fp_hash ON fp_cache(hash);
CREATE INDEX IF NOT EXISTS idx_fp_key ON fp_cache(shazam_key);

CREATE TABLE IF NOT EXISTS cache_tracks (
    shazam_key TEXT PRIMARY KEY,
    title      TEXT,
    artist     TEXT,
    isrc       TEXT,
    added_at   TEXT NOT NULL,
    hit_count  INTEGER NOT NULL DEFAULT 0
);

-- Derived from detections; safe to delete and rebuild (spec 2.2).
CREATE TABLE IF NOT EXISTS plays (
    id              INTEGER PRIMARY KEY,
    shazam_key      TEXT NOT NULL,
    title           TEXT,
    artist          TEXT,
    started_at      TEXT NOT NULL,
    ended_at        TEXT NOT NULL,
    detection_count INTEGER NOT NULL,
    derived_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_plays_started ON plays(started_at);

-- Every outbound recognizer call, including failures, so the daily ceiling in
-- spec 5.4 counts what actually left the machine rather than what succeeded.
CREATE TABLE IF NOT EXISTS api_calls (
    id      INTEGER PRIMARY KEY,
    at      TEXT NOT NULL,
    outcome TEXT NOT NULL          -- 'match' | 'nomatch' | 'error' | 'ratelimit'
);
CREATE INDEX IF NOT EXISTS idx_api_calls_at ON api_calls(at);
"""


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {r[1] for r in rows}


def _migrate(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version >= SCHEMA_VERSION:
        return

    # The phase-1 prototype stored a flat `plays` table with different columns.
    # Preserve it rather than dropping the user's history: it holds real
    # observations that the new derived table cannot reconstruct.
    existing = {
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    if "plays" in existing and "first_heard" in _table_columns(conn, "plays"):
        if "plays_v1_legacy" not in existing:
            conn.execute("ALTER TABLE plays RENAME TO plays_v1_legacy")
            print("  migrated: old plays table preserved as plays_v1_legacy")
        else:
            conn.execute("DROP TABLE plays")

    conn.executescript(_SCHEMA)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()


def connect(path: str | Path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    # WAL survives an unclean shutdown -- the venue PC will lose power.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    _migrate(conn)
    return conn


# ----------------------------------------------------------------------
# detections
# ----------------------------------------------------------------------

def insert_detection(conn: sqlite3.Connection, det: Detection) -> None:
    if det.method not in METHODS:
        raise ValueError(f"unknown method {det.method!r}")
    conn.execute(
        "INSERT INTO detections (id, captured_at, shazam_key, title, artist,"
        " isrc, level_dbfs, method, client_version, synced_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
        (det.id, det.captured_at, det.shazam_key, det.title, det.artist,
         det.isrc, det.level_dbfs, det.method, det.client_version),
    )
    conn.commit()


def unsynced_count(conn: sqlite3.Connection) -> int:
    """Queue depth, reported in the heartbeat once phase 3 exists."""
    return conn.execute(
        "SELECT COUNT(*) FROM detections WHERE synced_at IS NULL"
    ).fetchone()[0]


def detection_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM detections").fetchone()[0]


def unsynced_batch(conn: sqlite3.Connection, limit: int) -> list[sqlite3.Row]:
    """Oldest first, so a partial sync still makes forward progress (spec 6)."""
    return conn.execute(
        "SELECT id, captured_at, shazam_key, title, artist, isrc, level_dbfs,"
        " method, client_version FROM detections"
        " WHERE synced_at IS NULL ORDER BY captured_at LIMIT ?",
        (limit,),
    ).fetchall()


def mark_synced(conn: sqlite3.Connection, ids: list[str]) -> None:
    if not ids:
        return
    now = iso(utc_now())
    conn.executemany(
        "UPDATE detections SET synced_at = ? WHERE id = ?",
        [(now, i) for i in ids],
    )
    conn.commit()


# ----------------------------------------------------------------------
# api call accounting (spec 5.4)
# ----------------------------------------------------------------------

def record_api_call(conn: sqlite3.Connection, outcome: str) -> None:
    conn.execute(
        "INSERT INTO api_calls (at, outcome) VALUES (?, ?)",
        (iso(utc_now()), outcome),
    )
    conn.commit()


def api_calls_since(conn: sqlite3.Connection, since: dt.datetime) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM api_calls WHERE at >= ?", (iso(since),)
    ).fetchone()[0]


def api_calls_today(conn: sqlite3.Connection) -> int:
    midnight = utc_now().replace(hour=0, minute=0, second=0, microsecond=0)
    return api_calls_since(conn, midnight)


# ----------------------------------------------------------------------
# recognition cache (spec 5.3)
# ----------------------------------------------------------------------

def upsert_cache_track(conn: sqlite3.Connection, key: str, title: str | None,
                       artist: str | None, isrc: str | None) -> None:
    conn.execute(
        "INSERT INTO cache_tracks (shazam_key, title, artist, isrc, added_at)"
        " VALUES (?, ?, ?, ?, ?)"
        " ON CONFLICT(shazam_key) DO UPDATE SET"
        "   title=excluded.title, artist=excluded.artist, isrc=excluded.isrc",
        (key, title, artist, isrc, iso(utc_now())),
    )
    conn.commit()


def get_cache_track(conn: sqlite3.Connection, key: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM cache_tracks WHERE shazam_key = ?", (key,)
    ).fetchone()


def cached_hash_count(conn: sqlite3.Connection, key: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM fp_cache WHERE shazam_key = ?", (key,)
    ).fetchone()[0]


def store_fingerprints(conn: sqlite3.Connection, key: str,
                       hashes: list[tuple[int, float]], limit: int) -> int:
    """Add a chunk's hashes to the cache. Returns how many were stored."""
    have = cached_hash_count(conn, key)
    room = max(0, limit - have)
    if room <= 0:
        return 0
    payload = [(h, off, key) for h, off in hashes[:room]]
    conn.executemany(
        "INSERT INTO fp_cache (hash, offset_s, shazam_key) VALUES (?, ?, ?)",
        payload,
    )
    conn.commit()
    return len(payload)


def bump_hit_count(conn: sqlite3.Connection, key: str) -> None:
    conn.execute(
        "UPDATE cache_tracks SET hit_count = hit_count + 1 WHERE shazam_key = ?",
        (key,),
    )
    conn.commit()


def match_fingerprints(conn: sqlite3.Connection, hashes: list[tuple[int, float]],
                       align_bin_s: float) -> tuple[str, int] | None:
    """
    Return (shazam_key, aligned_count) for the best candidate, or None.

    A shared hash on its own means nothing -- noise collides. What identifies a
    track is many hashes agreeing on the *same* time offset between the query
    and the stored copy, so we histogram the offset deltas and take the mode.
    """
    if not hashes:
        return None

    conn.execute("CREATE TEMP TABLE IF NOT EXISTS q (hash INTEGER, offset_s REAL)")
    conn.execute("DELETE FROM q")
    conn.executemany("INSERT INTO q (hash, offset_s) VALUES (?, ?)", hashes)

    rows = conn.execute(
        "SELECT c.shazam_key AS k, c.offset_s - q.offset_s AS delta"
        " FROM fp_cache c JOIN q ON c.hash = q.hash"
    ).fetchall()
    conn.execute("DELETE FROM q")

    if not rows:
        return None

    per_track: dict[str, Counter] = defaultdict(Counter)
    for row in rows:
        per_track[row["k"]][round(row["delta"] / align_bin_s)] += 1

    best_key, best_score = None, 0
    for key, histogram in per_track.items():
        _, score = histogram.most_common(1)[0]
        if score > best_score:
            best_key, best_score = key, score
    if best_key is None:
        return None
    return best_key, best_score


def cache_stats(conn: sqlite3.Connection) -> dict:
    tracks = conn.execute("SELECT COUNT(*) FROM cache_tracks").fetchone()[0]
    hashes = conn.execute("SELECT COUNT(*) FROM fp_cache").fetchone()[0]
    hits = conn.execute(
        "SELECT COUNT(*) FROM detections WHERE method = 'cache'"
    ).fetchone()[0]
    api = conn.execute(
        "SELECT COUNT(*) FROM detections WHERE method IN ('shazam', 'nomatch')"
    ).fetchone()[0]
    return {"tracks": tracks, "hashes": hashes, "cache_hits": hits,
            "api_detections": api}


# ----------------------------------------------------------------------
# export
# ----------------------------------------------------------------------

def export_plays_csv(conn: sqlite3.Connection, out_path: str | Path) -> int:
    rows = conn.execute(
        "SELECT started_at, ended_at, artist, title, detection_count, shazam_key"
        " FROM plays ORDER BY started_at"
    ).fetchall()
    # utf-8-sig so Excel detects the encoding instead of mangling accents.
    with open(out_path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["started_at", "ended_at", "artist", "title",
                    "detection_count", "shazam_key"])
        w.writerows(tuple(r) for r in rows)
    return len(rows)


def export_detections_csv(conn: sqlite3.Connection, out_path: str | Path) -> int:
    rows = conn.execute(
        "SELECT captured_at, artist, title, method, level_dbfs, shazam_key, isrc"
        " FROM detections ORDER BY captured_at"
    ).fetchall()
    with open(out_path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["captured_at", "artist", "title", "method", "level_dbfs",
                    "shazam_key", "isrc"])
        w.writerows(tuple(r) for r in rows)
    return len(rows)
