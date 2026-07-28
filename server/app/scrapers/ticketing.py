"""Rung 3: ticketing embeds (spec 7).

Many venue "calendars" are an iframe from DICE, Eventbrite, Ticketmaster or
See Tickets. Scraping the wrapper gets you nothing; the listings live on the
platform.

This rung detects the platform and follows the embed, then re-runs the ld+json
parser against it -- those platforms all emit schema.org Event markup, so the
generic path usually works without a per-platform API client. When it does not,
`detect` still names the platform and the embed URL so a dedicated adapter can
be written against a known target rather than guessed at.
"""

from __future__ import annotations

import logging
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from .base import ScrapedEvent
from .ldjson import extract, to_event

log = logging.getLogger(__name__)

PLATFORMS = {
    "dice.fm": "dice",
    "eventbrite.": "eventbrite",
    "ticketmaster.": "ticketmaster",
    "seetickets.": "seetickets",
    "wl.seetickets.": "seetickets",
    "songkick.": "songkick",
    "bandsintown.": "bandsintown",
}


def detect(html: str, page_url: str) -> list[tuple[str, str]]:
    """Return [(platform, embed_url)] found in iframes, links and scripts."""
    soup = BeautifulSoup(html, "lxml")
    found: list[tuple[str, str]] = []
    seen: set[str] = set()

    candidates: list[str] = []
    for tag in soup.find_all(["iframe", "script"]):
        src = tag.get("src")
        if src:
            candidates.append(src)
    for tag in soup.find_all("a", href=True):
        candidates.append(tag["href"])

    for candidate in candidates:
        absolute = urljoin(page_url, candidate)
        for needle, platform in PLATFORMS.items():
            if needle in absolute.lower() and absolute not in seen:
                seen.add(absolute)
                found.append((platform, absolute))
                break
    return found


class TicketingScraper:
    name = "ticketing"

    def __init__(self, tz=None, max_embeds: int = 3):
        self.tz = tz
        self.max_embeds = max_embeds
        #: Populated by fetch() so the caller can report what was seen even
        #: when nothing parsed.
        self.detected: list[tuple[str, str]] = []

    def fetch(self, client: httpx.Client, url: str) -> list[ScrapedEvent]:
        response = client.get(url)
        response.raise_for_status()
        self.detected = detect(response.text, url)
        if not self.detected:
            return []

        events: list[ScrapedEvent] = []
        for platform, embed_url in self.detected[:self.max_embeds]:
            try:
                embed = client.get(embed_url)
                embed.raise_for_status()
            except Exception as exc:
                log.info("embed %s unreachable: %s", embed_url, exc)
                continue
            for node in extract(embed.text):
                event = to_event(node, self.tz)
                if event:
                    event.source = platform
                    events.append(event)
        if self.detected and not events:
            log.warning(
                "ticketing embeds found but none parsed: %s -- a dedicated "
                "adapter is needed for: %s",
                [u for _, u in self.detected],
                sorted({p for p, _ in self.detected}))
        return events
