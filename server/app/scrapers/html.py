"""Rung 4: HTML parsing (spec 7). Last resort. Brittle by nature.

Anchors on <time datetime="..."> because that is the one piece of machine
readable markup that survives most theme changes, then walks outward for the
nearest heading or link text to use as a title. Expect to revisit this whenever
the venue redesigns; that is why the raw payload is kept.
"""

from __future__ import annotations

import hashlib
import logging
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from .base import ScrapedEvent, as_utc, parse_datetime

log = logging.getLogger(__name__)

_HEADINGS = ("h1", "h2", "h3", "h4", "a")


def _title_near(node) -> str | None:
    """Nearest heading or link text, searching the element then its ancestors."""
    for ancestor in [node, *node.parents]:
        if ancestor is None or getattr(ancestor, "name", None) in (None, "body",
                                                                   "html"):
            break
        for tag in ancestor.find_all(_HEADINGS):
            text = tag.get_text(" ", strip=True)
            if text and len(text) > 2:
                return text[:255]
    return None


def parse(html: str, page_url: str, tz=None) -> list[ScrapedEvent]:
    soup = BeautifulSoup(html, "lxml")
    events: list[ScrapedEvent] = []
    seen: set[str] = set()

    for tag in soup.find_all("time"):
        starts = as_utc(parse_datetime(tag.get("datetime")
                                       or tag.get_text(strip=True)), tz)
        if starts is None:
            continue

        title = _title_near(tag)
        link = None
        for ancestor in [tag, *tag.parents][:6]:
            anchor = (ancestor if getattr(ancestor, "name", None) == "a"
                      else ancestor.find("a", href=True)
                      if hasattr(ancestor, "find") else None)
            if anchor is not None and anchor.get("href"):
                link = urljoin(page_url, anchor["href"])
                break

        # No stable id exists in raw HTML, so synthesise one from the fields
        # that identify the event. A retitled event becomes a new row rather
        # than silently mutating the old one.
        fingerprint = hashlib.sha1(
            f"{starts.isoformat()}|{title}|{link}".encode("utf-8")
        ).hexdigest()[:32]
        if fingerprint in seen:
            continue
        seen.add(fingerprint)

        events.append(ScrapedEvent(
            source="html",
            external_id=fingerprint,
            title=title,
            starts_at=starts,
            url=link or page_url,
            raw={"datetime": tag.get("datetime"), "title": title, "url": link},
        ))
    return events


class HtmlScraper:
    name = "html"

    def __init__(self, tz=None):
        self.tz = tz

    def fetch(self, client: httpx.Client, url: str) -> list[ScrapedEvent]:
        response = client.get(url)
        response.raise_for_status()
        return parse(response.text, url, self.tz)
