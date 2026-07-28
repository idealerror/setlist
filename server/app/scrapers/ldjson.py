"""Rung 1: application/ld+json (spec 7).

Always check this first. Most modern venue sites emit schema.org Event objects,
and they give clean start times, performers and URLs with no HTML parsing.
"""

from __future__ import annotations

import json
import logging

import httpx
from bs4 import BeautifulSoup

from .base import ScrapedEvent, as_utc, names_of, parse_datetime

log = logging.getLogger(__name__)


def _walk(node, found: list) -> None:
    """ld+json blocks nest events under @graph, itemListElement, or bare lists."""
    if isinstance(node, list):
        for item in node:
            _walk(item, found)
        return
    if not isinstance(node, dict):
        return

    node_type = node.get("@type")
    types = node_type if isinstance(node_type, list) else [node_type]
    if any(isinstance(t, str) and t.endswith("Event") for t in types):
        found.append(node)

    for key in ("@graph", "itemListElement", "subEvent", "events"):
        if key in node:
            _walk(node[key], found)
    # itemListElement entries often wrap the event in {"item": {...}}
    if "item" in node:
        _walk(node["item"], found)


def extract(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    found: list[dict] = []
    for tag in soup.find_all("script", type="application/ld+json"):
        text = tag.string or tag.get_text() or ""
        if not text.strip():
            continue
        try:
            _walk(json.loads(text), found)
        except json.JSONDecodeError:
            # Some sites emit trailing commas or concatenated objects; skip
            # rather than fail the whole rung.
            log.debug("skipping unparsable ld+json block")
    return found


def to_event(node: dict, tz) -> ScrapedEvent | None:
    starts = as_utc(parse_datetime(node.get("startDate")), tz)
    if starts is None:
        return None
    external = (node.get("@id") or node.get("identifier")
                or node.get("url") or node.get("name"))
    return ScrapedEvent(
        source="ldjson",
        external_id=str(external)[:255],
        title=node.get("name"),
        starts_at=starts,
        ends_at=as_utc(parse_datetime(node.get("endDate")), tz),
        doors_at=as_utc(parse_datetime(node.get("doorTime")), tz),
        url=node.get("url") if isinstance(node.get("url"), str) else None,
        performers=names_of(node.get("performer")),
        raw=node,
    )


class LdJsonScraper:
    name = "ldjson"

    def __init__(self, tz=None):
        self.tz = tz

    def fetch(self, client: httpx.Client, url: str) -> list[ScrapedEvent]:
        response = client.get(url)
        response.raise_for_status()
        events = []
        for node in extract(response.text):
            event = to_event(node, self.tz)
            if event:
                events.append(event)
        return events
