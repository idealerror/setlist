#!/usr/bin/env python3
"""Operational CLI for the setlist server.

    python manage.py create-venue --slug demo-venue --name "Demo Venue" \
        --timezone America/Los_Angeles --events-url https://venue.example/events
    python manage.py issue-token --slug demo-venue
    python manage.py scrape
    python manage.py nightly            # sessionize + associate, all venues
    python manage.py health-check
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys

from sqlalchemy import select

from app.auth import generate_token, hash_token
from app.db import init_engine, session_scope
from app.jobs import associate, health, sessionize
from app.models import Base, Venue
from app.scrapers import runner


def _venues(db, slug: str | None):
    stmt = select(Venue)
    if slug:
        stmt = stmt.where(Venue.slug == slug)
    found = list(db.execute(stmt).scalars())
    if slug and not found:
        raise SystemExit(f"No venue with slug {slug!r}")
    return found


def _parse_when(value: str | None):
    if not value:
        return None
    moment = dt.datetime.fromisoformat(value)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.timezone.utc)
    return moment


def cmd_init_db(args):
    """Create tables directly. Alembic is the supported path for production;
    this exists for a throwaway or test database."""
    Base.metadata.create_all(init_engine())
    print("Tables created.")


def cmd_create_venue(args):
    with session_scope() as db:
        if db.execute(select(Venue).where(Venue.slug == args.slug)).scalar_one_or_none():
            raise SystemExit(f"Venue {args.slug!r} already exists")
        venue = Venue(slug=args.slug, name=args.name or args.slug,
                      timezone=args.timezone, events_url=args.events_url)
        db.add(venue)
        db.commit()
        print(f"Created venue {venue.slug} (id {venue.id})")
        print("Now run: python manage.py issue-token --slug " + venue.slug)


def cmd_issue_token(args):
    with session_scope() as db:
        venue = _venues(db, args.slug)[0]
        token = generate_token()
        venue.token_hash = hash_token(token)
        db.commit()
        print(f"Token for {venue.slug}:\n\n  {token}\n")
        print("Store it in the client's config now -- only the hash is kept,")
        print("so this cannot be shown again. Reissue to rotate.")


def cmd_scrape(args):
    with session_scope() as db:
        results = [runner.scrape_venue(db, v) for v in _venues(db, args.slug)]
    print(json.dumps(results, indent=2, default=str))


def cmd_sessionize(args):
    with session_scope() as db:
        for venue in _venues(db, args.slug):
            count = sessionize.rebuild(
                db, venue, since=_parse_when(args.since),
                until=_parse_when(args.until), gap_minutes=args.gap_minutes)
            print(f"{venue.slug}: {count} plays")


def cmd_associate(args):
    with session_scope() as db:
        for venue in _venues(db, args.slug):
            print(json.dumps(associate.run(
                db, venue, since=_parse_when(args.since),
                until=_parse_when(args.until)), default=str))


def cmd_nightly(args):
    """Sessionize then associate. Idempotent, so re-running over any range
    after retuning a threshold is always safe (spec 8)."""
    since, until = _parse_when(args.since), _parse_when(args.until)
    with session_scope() as db:
        for venue in _venues(db, args.slug):
            plays = sessionize.rebuild(db, venue, since=since, until=until)
            result = associate.run(db, venue, since=since, until=until)
            print(f"{venue.slug}: {plays} plays, {result['matched']} associated, "
                  f"{result['unmatched']} unassociated")


def cmd_health_check(args):
    with session_scope() as db:
        problems = health.check(db, fire=not args.dry_run)
    if not problems:
        print("All venues healthy.")
        return
    print(json.dumps(problems, indent=2, default=str))
    sys.exit(1)


def build_parser():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db").set_defaults(func=cmd_init_db)

    cv = sub.add_parser("create-venue")
    cv.add_argument("--slug", required=True)
    cv.add_argument("--name")
    cv.add_argument("--timezone", default="UTC")
    cv.add_argument("--events-url")
    cv.set_defaults(func=cmd_create_venue)

    it = sub.add_parser("issue-token")
    it.add_argument("--slug", required=True)
    it.set_defaults(func=cmd_issue_token)

    sc = sub.add_parser("scrape")
    sc.add_argument("--slug")
    sc.set_defaults(func=cmd_scrape)

    for name, func in (("sessionize", cmd_sessionize), ("associate", cmd_associate),
                       ("nightly", cmd_nightly)):
        cmd = sub.add_parser(name)
        cmd.add_argument("--slug")
        cmd.add_argument("--since")
        cmd.add_argument("--until")
        if name in ("sessionize",):
            cmd.add_argument("--gap-minutes", type=int)
        cmd.set_defaults(func=func)

    hc = sub.add_parser("health-check")
    hc.add_argument("--dry-run", action="store_true",
                    help="report without firing the webhook")
    hc.set_defaults(func=cmd_health_check)
    return p


def main():
    init_engine()
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
