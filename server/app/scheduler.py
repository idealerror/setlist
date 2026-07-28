"""In-container scheduler (spec 7, spec 8).

Deliberately stdlib-only rather than APScheduler or a cron sidecar: three jobs
on fixed cadences do not justify a dependency, and a plain loop is far easier
to reason about at 2am when a scrape has silently stopped.

Every job is wrapped so a failure logs and the loop continues. A scrape failure
must never affect anything else (spec 7), and it structurally cannot affect
capture, which runs on a different machine.
"""

from __future__ import annotations

import datetime as dt
import logging
import time

from sqlalchemy import select

from .config import get_settings
from .db import init_engine, session_scope
from .jobs import associate, health, sessionize
from .models import Venue
from .scrapers import runner

log = logging.getLogger("scheduler")

#: UTC hours at which to scrape. Twice daily per spec 7.
SCRAPE_HOURS = (4, 16)
#: UTC hour for the nightly sessionize + associate pass (spec 8).
NIGHTLY_HOUR = 5
#: Health check cadence. The client heartbeats every 5 minutes (spec 8).
HEALTH_INTERVAL_S = 300


def _safe(name: str, func) -> None:
    try:
        result = func()
        log.info("%s: %s", name, result)
    except Exception as exc:
        log.exception("%s failed: %s", name, exc)


def do_scrape() -> list:
    with session_scope() as db:
        return runner.scrape_all(db)


def do_nightly() -> list:
    """Sessionize then associate, for every venue. Idempotent by construction,
    so a re-run after a scraper backfill simply corrects the associations."""
    out = []
    with session_scope() as db:
        for venue in db.execute(select(Venue)).scalars():
            plays = sessionize.rebuild(db, venue)
            result = associate.run(db, venue)
            out.append({"venue": venue.slug, "plays": plays, **result})
    return out


def do_health() -> list:
    with session_scope() as db:
        return health.check(db)


def next_at(now: dt.datetime, hours: tuple[int, ...]) -> dt.datetime:
    today = [now.replace(hour=h, minute=0, second=0, microsecond=0)
             for h in sorted(hours)]
    for moment in today:
        if moment > now:
            return moment
    return today[0] + dt.timedelta(days=1)


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    init_engine()
    settings = get_settings()
    log.info("scheduler up; scrape at %s UTC, nightly at %02d:00 UTC, "
             "health every %ds", SCRAPE_HOURS, NIGHTLY_HOUR, HEALTH_INTERVAL_S)
    if not settings.home_assistant_webhook_url:
        log.warning("no Home Assistant webhook configured; alerts will only "
                    "be logged")

    now = dt.datetime.now(dt.timezone.utc)
    next_scrape = next_at(now, SCRAPE_HOURS)
    next_nightly = next_at(now, (NIGHTLY_HOUR,))
    next_health = now

    while True:
        now = dt.datetime.now(dt.timezone.utc)
        if now >= next_scrape:
            _safe("scrape", do_scrape)
            next_scrape = next_at(now, SCRAPE_HOURS)
        if now >= next_nightly:
            _safe("nightly", do_nightly)
            next_nightly = next_at(now, (NIGHTLY_HOUR,))
        if now >= next_health:
            _safe("health", do_health)
            next_health = now + dt.timedelta(seconds=HEALTH_INTERVAL_S)
        time.sleep(15)


if __name__ == "__main__":
    main()
