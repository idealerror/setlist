"""Home Assistant webhook alerting (spec 8).

Alerting is best-effort by design: a webhook that is down must never take an
API request or a nightly job with it.
"""

from __future__ import annotations

import logging

import httpx

from ..config import get_settings

log = logging.getLogger(__name__)


def notify(kind: str, payload: dict) -> bool:
    """Fire an alert. Returns True if it was delivered."""
    url = get_settings().home_assistant_webhook_url
    if not url:
        log.info("alert %s (no webhook configured): %s", kind, payload)
        return False
    try:
        response = httpx.post(url, json={"kind": kind, **payload}, timeout=10.0)
        response.raise_for_status()
        return True
    except Exception as exc:
        log.warning("alert %s could not be delivered: %s", kind, exc)
        return False
