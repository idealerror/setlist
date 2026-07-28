"""SQLAlchemy 2.x models (spec 4.2).

Two portability notes:

* ``detections.id`` is the *client-generated* UUID, not a server sequence. That
  is the whole basis of idempotent retry: ``INSERT ... ON CONFLICT (id) DO
  NOTHING`` makes a replayed batch free (spec 4.2).
* JSONB and native UUID are Postgres types, but the columns are declared
  through variants so the same models run on SQLite. Production is Postgres 16;
  SQLite exists so the test suite needs no database daemon.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import (JSON, BigInteger, DateTime, Float, ForeignKey, Index,
                        Integer, String, Text, UniqueConstraint, Uuid)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

#: JSONB on Postgres, plain JSON elsewhere.
JsonCol = JSON().with_variant(JSONB, "postgresql")


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class Base(DeclarativeBase):
    pass


class Venue(Base):
    __tablename__ = "venues"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255))
    timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    #: sha256 of the bearer token. One token per venue (spec 6); the plaintext
    #: is shown once at creation and never stored.
    token_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    #: Public calendar to scrape (spec 7).
    events_url: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow)


class Track(Base):
    __tablename__ = "tracks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    shazam_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    isrc: Mapped[str | None] = mapped_column(String(32), index=True)
    title: Mapped[str | None] = mapped_column(Text)
    artist: Mapped[str | None] = mapped_column(Text)
    album: Mapped[str | None] = mapped_column(Text)
    artwork_url: Mapped[str | None] = mapped_column(Text)
    first_seen_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow)


class Detection(Base):
    __tablename__ = "detections"

    #: Client-generated UUIDv4. Not a server sequence -- see module docstring.
    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    venue_id: Mapped[int] = mapped_column(
        ForeignKey("venues.id", ondelete="CASCADE"))
    track_id: Mapped[int | None] = mapped_column(
        ForeignKey("tracks.id", ondelete="SET NULL"))
    captured_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    level_dbfs: Mapped[float] = mapped_column(Float)
    method: Mapped[str] = mapped_column(String(16))
    client_version: Mapped[str] = mapped_column(String(32))
    received_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow)

    track: Mapped[Track | None] = relationship(lazy="joined")

    __table_args__ = (
        Index("idx_detections_venue_captured", "venue_id", "captured_at"),
    )


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    venue_id: Mapped[int] = mapped_column(
        ForeignKey("venues.id", ondelete="CASCADE"))
    #: Which rung of the spec 7 ladder produced this: 'ldjson', 'tribe',
    #: 'dice', 'eventbrite', 'html'.
    source: Mapped[str] = mapped_column(String(32))
    external_id: Mapped[str] = mapped_column(String(255))
    title: Mapped[str | None] = mapped_column(Text)
    starts_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    doors_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    url: Mapped[str | None] = mapped_column(Text)
    performers: Mapped[list | None] = mapped_column(JsonCol)
    #: The untouched payload, so re-parsing never requires re-scraping (spec 7).
    raw: Mapped[dict | None] = mapped_column(JsonCol)
    scraped_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        UniqueConstraint("venue_id", "source", "external_id",
                         name="uq_events_venue_source_external"),
        Index("idx_events_venue_starts", "venue_id", "starts_at"),
    )


class Play(Base):
    __tablename__ = "plays"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    venue_id: Mapped[int] = mapped_column(
        ForeignKey("venues.id", ondelete="CASCADE"))
    track_id: Mapped[int] = mapped_column(
        ForeignKey("tracks.id", ondelete="CASCADE"))
    #: NULL is a valid, expected state: an unlisted show, a soundcheck, or a
    #: scraper that has not caught up (spec 8).
    event_id: Mapped[int | None] = mapped_column(
        ForeignKey("events.id", ondelete="SET NULL"))
    started_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    detection_count: Mapped[int] = mapped_column(Integer)
    derived_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow)

    track: Mapped[Track] = relationship(lazy="joined")

    __table_args__ = (
        Index("idx_plays_venue_started", "venue_id", "started_at"),
    )


class Heartbeat(Base):
    __tablename__ = "heartbeats"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"),
                                    primary_key=True)
    venue_id: Mapped[int] = mapped_column(
        ForeignKey("venues.id", ondelete="CASCADE"))
    at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True),
                                            default=utcnow)
    client_version: Mapped[str] = mapped_column(String(32))
    queue_depth: Mapped[int] = mapped_column(Integer, default=0)
    uptime_s: Mapped[int] = mapped_column(Integer, default=0)

    __table_args__ = (
        Index("idx_heartbeats_venue_at", "venue_id", "at"),
    )


__all__ = ["Base", "Venue", "Track", "Detection", "Event", "Play",
           "Heartbeat", "utcnow"]
