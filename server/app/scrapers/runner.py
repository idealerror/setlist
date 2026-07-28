"""Run the spec 7 ladder and persist the result.

Stop at the first rung that yields events. A scrape failure must never affect
capture -- and it structurally cannot here, because capture runs on a different
machine and never consults this data (spec 2.1). The worst case is stale event
associations, which the association job fixes on its next run.
"""

from __future__ import annotations

import datetime as dt
import logging
import zoneinfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Event, Venue, utcnow
from .base import ScrapedEvent, make_client
from .html import HtmlScraper
from .ldjson import LdJsonScraper
from .ticketing import TicketingScraper
from .tribe import TribeScraper

log = logging.getLogger(__name__)


def venue_tz(venue: Venue):
    try:
        return zoneinfo.ZoneInfo(venue.timezone or "UTC")
    except Exception:
        log.warning("venue %s has unknown timezone %r; assuming UTC",
                    venue.slug, venue.timezone)
        return dt.timezone.utc


def ladder(tz):
    """Ordered per spec 7. Do not reorder: ld+json is both the cleanest and
    the cheapest, and HTML is genuinely a last resort."""
    return [LdJsonScraper(tz), TribeScraper(tz), TicketingScraper(tz),
            HtmlScraper(tz)]


def persist(db: Session, venue: Venue, events: list[ScrapedEvent]) -> dict:
    created = updated = 0
    now = utcnow()
    for event in events:
        existing = db.execute(
            select(Event).where(Event.venue_id == venue.id,
                                Event.source == event.source,
                                Event.external_id == event.external_id)
        ).scalar_one_or_none()
        if existing is None:
            db.add(Event(
                venue_id=venue.id, source=event.source,
                external_id=event.external_id, title=event.title,
                starts_at=event.starts_at, ends_at=event.ends_at,
                doors_at=event.doors_at, url=event.url,
                performers=event.performers, raw=event.raw, scraped_at=now,
            ))
            created += 1
        else:
            existing.title = event.title or existing.title
            existing.starts_at = event.starts_at or existing.starts_at
            existing.ends_at = event.ends_at or existing.ends_at
            existing.doors_at = event.doors_at or existing.doors_at
            existing.url = event.url or existing.url
            existing.performers = event.performers or existing.performers
            existing.raw = event.raw or existing.raw
            existing.scraped_at = now
            updated += 1
    db.commit()
    return {"created": created, "updated": updated}


def scrape_venue(db: Session, venue: Venue, url: str | None = None,
                 client=None) -> dict:
    """Walk the ladder for one venue. `client` is injectable for tests."""
    target = url or venue.events_url
    if not target:
        return {"venue": venue.slug, "skipped": "no events_url configured"}

    tz = venue_tz(venue)
    attempts: list[dict] = []
    owned = client is None
    client = client or make_client()
    try:
        for scraper in ladder(tz):
            try:
                events = scraper.fetch(client, target)
            except Exception as exc:
                attempts.append({"rung": scraper.name,
                                 "error": f"{type(exc).__name__}: {exc}"})
                log.info("rung %s failed for %s: %s", scraper.name, venue.slug, exc)
                continue

            attempts.append({"rung": scraper.name, "events": len(events)})
            if events:
                result = persist(db, venue, events)
                return {"venue": venue.slug, "rung": scraper.name,
                        "found": len(events), **result, "attempts": attempts}
    finally:
        if owned:
            client.close()

    return {"venue": venue.slug, "rung": None, "found": 0,
            "created": 0, "updated": 0, "attempts": attempts}


def scrape_all(db: Session) -> list[dict]:
    return [scrape_venue(db, venue)
            for venue in db.execute(select(Venue)).scalars()]
