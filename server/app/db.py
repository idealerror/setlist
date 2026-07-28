"""Engine and session wiring."""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from .config import get_settings

_engine = None
_SessionLocal = None


def _make_engine(url: str):
    if url.startswith("sqlite"):
        # Tests only. check_same_thread is needed because TestClient runs the
        # app on a worker thread while the test holds the same connection.
        engine = create_engine(
            url, connect_args={"check_same_thread": False}, future=True)

        @event.listens_for(engine, "connect")
        def _fk_on(conn, _record):
            conn.execute("PRAGMA foreign_keys=ON")

        return engine
    return create_engine(url, pool_pre_ping=True, future=True)


def init_engine(url: str | None = None):
    global _engine, _SessionLocal
    _engine = _make_engine(url or get_settings().database_url)
    _SessionLocal = sessionmaker(bind=_engine, autoflush=False,
                                 expire_on_commit=False, future=True)
    return _engine


def get_engine():
    if _engine is None:
        init_engine()
    return _engine


def session_scope() -> Session:
    if _SessionLocal is None:
        init_engine()
    return _SessionLocal()


def get_db() -> Iterator[Session]:
    """FastAPI dependency."""
    db = session_scope()
    try:
        yield db
    finally:
        db.close()


def ensure_utc(moment: dt.datetime | None) -> dt.datetime | None:
    """SQLite drops tzinfo on round-trip; Postgres does not. Normalise so the
    jobs can compare timestamps without caring which backend they are on."""
    if moment is None:
        return None
    if moment.tzinfo is None:
        return moment.replace(tzinfo=dt.timezone.utc)
    return moment.astimezone(dt.timezone.utc)
