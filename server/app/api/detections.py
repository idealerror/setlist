"""POST /api/v1/detections -- idempotent batch upsert (spec 6)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import venue_from_token
from ..config import get_settings
from ..db import get_db
from ..models import Detection, Track, Venue
from ..schemas import BatchResult, DetectionBatch

router = APIRouter()


def insert_ignore(db: Session, table, rows: list[dict], index_elements):
    """INSERT ... ON CONFLICT DO NOTHING, on either supported dialect."""
    if not rows:
        return
    dialect = db.bind.dialect.name
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    elif dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
    else:  # pragma: no cover
        db.execute(table.__table__.insert(), rows)
        return
    db.execute(insert(table).on_conflict_do_nothing(
        index_elements=index_elements), rows)


def resolve_tracks(db: Session, batch: DetectionBatch) -> dict[str, int]:
    """Get-or-create a Track per shazam_key in the batch."""
    keys = {d.shazam_key for d in batch.detections if d.shazam_key}
    if not keys:
        return {}

    existing = {
        t.shazam_key: t.id
        for t in db.execute(
            select(Track).where(Track.shazam_key.in_(keys))).scalars()
    }
    missing = keys - existing.keys()
    if missing:
        # Take title/artist from the first detection that carries the key.
        meta = {}
        for det in batch.detections:
            if det.shazam_key in missing and det.shazam_key not in meta:
                meta[det.shazam_key] = det
        insert_ignore(db, Track, [
            {"shazam_key": key, "title": d.title, "artist": d.artist,
             "isrc": d.isrc}
            for key, d in meta.items()
        ], ["shazam_key"])
        db.flush()
        existing = {
            t.shazam_key: t.id
            for t in db.execute(
                select(Track).where(Track.shazam_key.in_(keys))).scalars()
        }
    return existing


@router.post("/detections", response_model=BatchResult)
def post_detections(
    batch: DetectionBatch,
    venue: Venue = Depends(venue_from_token),
    db: Session = Depends(get_db),
) -> BatchResult:
    settings = get_settings()

    if batch.venue != venue.slug:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Token is for venue '{venue.slug}', body says "
                   f"'{batch.venue}'")
    if len(batch.detections) > settings.max_batch:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Batch of {len(batch.detections)} exceeds the "
                   f"{settings.max_batch} cap")
    if not batch.detections:
        return BatchResult(accepted=0, duplicates=0)

    # A batch may repeat an id internally; keep the first occurrence.
    unique: dict = {}
    for det in batch.detections:
        unique.setdefault(det.id, det)

    already = {
        row for row in db.execute(
            select(Detection.id).where(Detection.id.in_(list(unique)))
        ).scalars()
    }
    fresh = [d for d in unique.values() if d.id not in already]
    duplicates = len(batch.detections) - len(fresh)

    track_ids = resolve_tracks(db, batch)
    insert_ignore(db, Detection, [
        {
            "id": d.id,
            "venue_id": venue.id,
            "track_id": track_ids.get(d.shazam_key) if d.shazam_key else None,
            "captured_at": d.captured_at,
            "level_dbfs": d.level_dbfs,
            "method": d.method,
            "client_version": d.client_version,
        }
        for d in fresh
    ], ["id"])
    db.commit()

    return BatchResult(accepted=len(fresh), duplicates=duplicates)
