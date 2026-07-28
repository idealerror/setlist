"""Shared types for the spec 7 fallback ladder."""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from typing import Protocol

import httpx

from ..config import get_settings

log = logging.getLogger(__name__)


@dataclass
class ScrapedEvent:
    source: str
    external_id: str
    title: str | None = None
    starts_at: dt.datetime | None = None
    ends_at: dt.datetime | None = None
    doors_at: dt.datetime | None = None
    url: str | None = None
    performers: list = field(default_factory=list)
    #: The untouched payload this was parsed from, persisted so that a parser
    #: fix never requires re-scraping the site (spec 7).
    raw: dict | None = None


class Scraper(Protocol):
    #: Rung name, recorded on events.source.
    name: str

    def fetch(self, client: httpx.Client, url: str) -> list[ScrapedEvent]:
        ...


def make_client() -> httpx.Client:
    settings = get_settings()
    return httpx.Client(
        timeout=settings.scrape_timeout_s,
        follow_redirects=True,
        headers={"User-Agent": settings.scrape_user_agent},
    )


def parse_datetime(value) -> dt.datetime | None:
    """Parse the date shapes venue sites actually emit."""
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None

    # fromisoformat handles 'Z' and offsets from 3.11 onward.
    try:
        return dt.datetime.fromisoformat(text)
    except ValueError:
        pass

    # The Events Calendar emits "2026-08-14 20:00:00" (venue-local, no zone).
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
                "%Y/%m/%d %H:%M:%S", "%d/%m/%Y %H:%M"):
        try:
            return dt.datetime.strptime(text, fmt)
        except ValueError:
            continue
    log.debug("unparsed datetime %r", text)
    return None


def as_utc(moment: dt.datetime | None, fallback_tz=dt.timezone.utc):
    """Naive timestamps are venue-local; callers pass the venue's zone."""
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=fallback_tz)
    return moment.astimezone(dt.timezone.utc)


def names_of(performers) -> list:
    """schema.org `performer` may be a string, an object, or a list of either."""
    if performers is None:
        return []
    if isinstance(performers, str):
        return [performers]
    if isinstance(performers, dict):
        name = performers.get("name")
        return [name] if name else []
    out = []
    for item in performers if isinstance(performers, list) else []:
        out.extend(names_of(item))
    return out
