"""Rung 2: The Events Calendar REST API (spec 7).

This WordPress plugin runs on a large share of venue sites and exposes
/wp-json/tribe/events/v1/events, which is far more reliable than parsing the
theme's markup.
"""

from __future__ import annotations

import datetime as dt
import logging
from urllib.parse import urljoin, urlparse

import httpx

from .base import ScrapedEvent, as_utc, parse_datetime

log = logging.getLogger(__name__)

ENDPOINT = "/wp-json/tribe/events/v1/events"


def api_url(site_url: str) -> str:
    parts = urlparse(site_url)
    return urljoin(f"{parts.scheme}://{parts.netloc}", ENDPOINT)


def to_event(node: dict, tz) -> ScrapedEvent | None:
    # utc_start_date is authoritative when present; start_date is venue-local.
    starts = parse_datetime(node.get("utc_start_date"))
    starts = (as_utc(starts, dt.timezone.utc) if starts
              else as_utc(parse_datetime(node.get("start_date")), tz))
    if starts is None:
        return None

    ends = parse_datetime(node.get("utc_end_date"))
    ends = (as_utc(ends, dt.timezone.utc) if ends
            else as_utc(parse_datetime(node.get("end_date")), tz))

    performers = []
    for group in ("organizer", "categories", "tags"):
        for item in node.get(group) or []:
            if isinstance(item, dict) and item.get("name"):
                performers.append(item["name"])

    return ScrapedEvent(
        source="tribe",
        external_id=str(node.get("id") or node.get("global_id") or node.get("url")),
        title=(node.get("title") or "").strip() or None,
        starts_at=starts,
        ends_at=ends,
        url=node.get("url"),
        performers=performers,
        raw=node,
    )


class TribeScraper:
    name = "tribe"

    def __init__(self, tz=None, page_size: int = 50, max_pages: int = 10):
        self.tz = tz
        self.page_size = page_size
        self.max_pages = max_pages

    def fetch(self, client: httpx.Client, url: str) -> list[ScrapedEvent]:
        endpoint = api_url(url)
        events: list[ScrapedEvent] = []
        page = 1
        while page <= self.max_pages:
            response = client.get(endpoint, params={
                "page": page, "per_page": self.page_size,
                "start_date": dt.date.today().isoformat(),
            })
            if response.status_code == 404:
                return []          # plugin not installed; fall through the ladder
            response.raise_for_status()
            payload = response.json()
            batch = payload.get("events") or []
            if not batch:
                break
            for node in batch:
                event = to_event(node, self.tz)
                if event:
                    events.append(event)
            if not payload.get("next_rest_url"):
                break
            page += 1
        return events
