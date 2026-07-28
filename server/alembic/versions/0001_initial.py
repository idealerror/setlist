"""Initial schema (spec 4.2).

Types are declared through SQLAlchemy generics with Postgres variants rather
than raw postgresql.* types, so this migration also runs on SQLite. Production
is Postgres 16; the SQLite path exists so the migration itself is testable
without a database daemon.

Revision ID: 0001
Revises:
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

JSON_COL = sa.JSON().with_variant(JSONB, "postgresql")


def upgrade() -> None:
    op.create_table(
        "venues",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("slug", sa.String(64), nullable=False, unique=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("timezone", sa.String(64), nullable=False, server_default="UTC"),
        sa.Column("token_hash", sa.String(64)),
        sa.Column("events_url", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_venues_slug", "venues", ["slug"])
    op.create_index("ix_venues_token_hash", "venues", ["token_hash"])

    op.create_table(
        "tracks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("shazam_key", sa.String(64), nullable=False, unique=True),
        sa.Column("isrc", sa.String(32)),
        sa.Column("title", sa.Text()),
        sa.Column("artist", sa.Text()),
        sa.Column("album", sa.Text()),
        sa.Column("artwork_url", sa.Text()),
        sa.Column("first_seen_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_tracks_shazam_key", "tracks", ["shazam_key"])
    op.create_index("ix_tracks_isrc", "tracks", ["isrc"])

    op.create_table(
        "detections",
        # Client-generated UUID: the basis of idempotent retry (spec 4.2).
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("venue_id", sa.Integer(),
                  sa.ForeignKey("venues.id", ondelete="CASCADE"), nullable=False),
        sa.Column("track_id", sa.Integer(),
                  sa.ForeignKey("tracks.id", ondelete="SET NULL")),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("level_dbfs", sa.Float(), nullable=False),
        sa.Column("method", sa.String(16), nullable=False),
        sa.Column("client_version", sa.String(32), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
    )
    op.create_index("idx_detections_venue_captured", "detections",
                    ["venue_id", "captured_at"])

    op.create_table(
        "events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("venue_id", sa.Integer(),
                  sa.ForeignKey("venues.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("external_id", sa.String(255), nullable=False),
        sa.Column("title", sa.Text()),
        sa.Column("starts_at", sa.DateTime(timezone=True)),
        sa.Column("ends_at", sa.DateTime(timezone=True)),
        sa.Column("doors_at", sa.DateTime(timezone=True)),
        sa.Column("url", sa.Text()),
        sa.Column("performers", JSON_COL),
        # Raw payload retained so re-parsing never needs re-scraping (spec 7).
        sa.Column("raw", JSON_COL),
        sa.Column("scraped_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("venue_id", "source", "external_id",
                            name="uq_events_venue_source_external"),
    )
    op.create_index("idx_events_venue_starts", "events", ["venue_id", "starts_at"])

    op.create_table(
        "plays",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("venue_id", sa.Integer(),
                  sa.ForeignKey("venues.id", ondelete="CASCADE"), nullable=False),
        sa.Column("track_id", sa.Integer(),
                  sa.ForeignKey("tracks.id", ondelete="CASCADE"), nullable=False),
        # NULL is valid and expected: unlisted show, soundcheck, or a scraper
        # that has not caught up (spec 8).
        sa.Column("event_id", sa.Integer(),
                  sa.ForeignKey("events.id", ondelete="SET NULL")),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("detection_count", sa.Integer(), nullable=False),
        sa.Column("derived_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
    )
    op.create_index("idx_plays_venue_started", "plays", ["venue_id", "started_at"])

    op.create_table(
        "heartbeats",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer, "sqlite"),
                  primary_key=True, autoincrement=True),
        sa.Column("venue_id", sa.Integer(),
                  sa.ForeignKey("venues.id", ondelete="CASCADE"), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("client_version", sa.String(32), nullable=False),
        sa.Column("queue_depth", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("uptime_s", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index("idx_heartbeats_venue_at", "heartbeats", ["venue_id", "at"])


def downgrade() -> None:
    op.drop_table("heartbeats")
    op.drop_table("plays")
    op.drop_table("events")
    op.drop_table("detections")
    op.drop_table("tracks")
    op.drop_table("venues")
