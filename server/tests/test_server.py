"""Self-contained checks for the setlist server.

Run with:  py -3.12 tests/test_server.py

Backed by SQLite so no database daemon is needed. Production is Postgres 16;
the models are declared with dialect variants so the same code runs on both.
"""

from __future__ import annotations

import datetime as dt
import os
import sys
import tempfile
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TMP = tempfile.mkdtemp(prefix="setlist_server_test_")
os.environ["SETLIST_DATABASE_URL"] = f"sqlite:///{Path(_TMP) / 'test.db'}"
os.environ["SETLIST_HOME_ASSISTANT_WEBHOOK_URL"] = ""

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.auth import generate_token, hash_token
from app.db import init_engine, session_scope
from app.jobs import associate, health, sessionize
from app.main import app
from app.models import Base, Detection, Event, Heartbeat, Play, Track, Venue
from app.scrapers import html as html_scraper
from app.scrapers import runner
from app.scrapers.ldjson import LdJsonScraper, extract, to_event
from app.scrapers.ticketing import TicketingScraper, detect
from app.scrapers.tribe import TribeScraper, api_url

FAILURES: list[str] = []
TOKEN = generate_token()


def check(label, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  ({detail})" if detail else ""))
    if not ok:
        FAILURES.append(label)


def section(title):
    print(f"\n=== {title} ===")


def fresh_db():
    engine = init_engine()
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with session_scope() as db:
        venue = Venue(slug="demo-venue", name="Demo Venue",
                      timezone="America/Los_Angeles",
                      token_hash=hash_token(TOKEN),
                      events_url="https://venue.example/events")
        db.add(venue)
        db.add(Venue(slug="other", name="Other", token_hash=hash_token("other-token")))
        db.commit()
        return venue.id


def auth(token=None):
    return {"Authorization": f"Bearer {token or TOKEN}"}


def detection_payload(n, start, key="k1", method="shazam"):
    return [{
        "id": str(uuid.uuid4()),
        "captured_at": (start + dt.timedelta(minutes=i)).isoformat(),
        "shazam_key": key,
        "title": "Track One",
        "artist": "Artist One",
        "isrc": "USUM71703861",
        "level_dbfs": -20.0,
        "method": method,
        "client_version": "0.2.0",
    } for i in range(n)]


# ----------------------------------------------------------------------

def test_auth():
    section("auth (spec 6)")
    fresh_db()
    client = TestClient(app)

    body = {"venue": "demo-venue", "detections": []}
    check("no token rejected",
          client.post("/api/v1/detections", json=body).status_code == 401)
    check("bad token rejected",
          client.post("/api/v1/detections", json=body,
                      headers=auth("nope")).status_code == 401)
    check("valid token accepted",
          client.post("/api/v1/detections", json=body,
                      headers=auth()).status_code == 200)

    wrong = client.post("/api/v1/detections",
                        json={"venue": "other", "detections": []}, headers=auth())
    check("token cannot write another venue", wrong.status_code == 403,
          str(wrong.status_code))
    check("health probe is open", client.get("/health").status_code == 200)


def test_idempotency():
    section("idempotent ingest (spec 4.2)")
    fresh_db()
    client = TestClient(app)
    start = dt.datetime(2026, 8, 14, 21, 0, tzinfo=dt.timezone.utc)
    payload = detection_payload(5, start)

    first = client.post("/api/v1/detections",
                        json={"venue": "demo-venue", "detections": payload},
                        headers=auth()).json()
    check("first batch accepted", first == {"accepted": 5, "duplicates": 0},
          str(first))

    replay = client.post("/api/v1/detections",
                         json={"venue": "demo-venue", "detections": payload},
                         headers=auth()).json()
    check("replay is all duplicates", replay == {"accepted": 0, "duplicates": 5},
          str(replay))

    overlap = payload[3:] + detection_payload(2, start + dt.timedelta(hours=1))
    partial = client.post("/api/v1/detections",
                          json={"venue": "demo-venue", "detections": overlap},
                          headers=auth()).json()
    check("partial overlap counted correctly",
          partial == {"accepted": 2, "duplicates": 2}, str(partial))

    dupe_id = payload[0]
    within = client.post("/api/v1/detections",
                         json={"venue": "demo-venue", "detections": [dupe_id, dupe_id]},
                         headers=auth()).json()
    check("duplicate id inside one batch collapses",
          within["accepted"] == 0, str(within))

    with session_scope() as db:
        check("one track row per shazam_key",
              db.execute(select(Track)).scalars().all().__len__() == 1)
        check("detections linked to track",
              db.execute(select(Detection).where(
                  Detection.track_id.is_(None))).scalars().first() is None)

    too_big = detection_payload(501, start)
    resp = client.post("/api/v1/detections",
                       json={"venue": "demo-venue", "detections": too_big},
                       headers=auth())
    check("batch cap enforced", resp.status_code == 413, str(resp.status_code))


def test_nomatch_rows():
    section("no-match rows")
    fresh_db()
    client = TestClient(app)
    start = dt.datetime(2026, 8, 14, 21, 0, tzinfo=dt.timezone.utc)
    payload = [{
        "id": str(uuid.uuid4()), "captured_at": start.isoformat(),
        "shazam_key": None, "title": None, "artist": None, "isrc": None,
        "level_dbfs": -30.0, "method": "nomatch", "client_version": "0.2.0",
    }]
    resp = client.post("/api/v1/detections",
                       json={"venue": "demo-venue", "detections": payload},
                       headers=auth()).json()
    check("no-match accepted", resp["accepted"] == 1)
    with session_scope() as db:
        det = db.execute(select(Detection)).scalars().one()
        check("no-match has null track", det.track_id is None)
        check("no track row invented",
              db.execute(select(Track)).scalars().first() is None)


def test_heartbeat():
    section("heartbeat (spec 6)")
    fresh_db()
    client = TestClient(app)
    body = {"venue": "demo-venue", "client_version": "0.2.0",
            "queue_depth": 17, "uptime_s": 8123}
    resp = client.post("/api/v1/heartbeat", json=body, headers=auth())
    check("heartbeat accepted", resp.status_code == 200)
    check("server time returned", "server_time" in resp.json())
    with session_scope() as db:
        row = db.execute(select(Heartbeat)).scalars().one()
        check("queue depth stored", row.queue_depth == 17)
        check("uptime stored", row.uptime_s == 8123)


def test_sessionize_and_associate():
    section("sessionize + associate (spec 8)")
    fresh_db()
    client = TestClient(app)
    doors = dt.datetime(2026, 8, 14, 20, 0, tzinfo=dt.timezone.utc)

    with session_scope() as db:
        venue = db.execute(select(Venue).where(Venue.slug == "demo-venue")).scalar_one()
        db.add(Event(venue_id=venue.id, source="ldjson", external_id="e1",
                     title="Friday Night", doors_at=doors,
                     starts_at=doors + dt.timedelta(minutes=30),
                     ends_at=doors + dt.timedelta(hours=4)))
        db.commit()

    inside = detection_payload(3, doors + dt.timedelta(hours=1), key="k1")
    later = detection_payload(2, doors + dt.timedelta(hours=1, minutes=40), key="k1")
    outside = detection_payload(2, doors - dt.timedelta(days=2), key="k2")
    client.post("/api/v1/detections",
                json={"venue": "demo-venue", "detections": inside + later + outside},
                headers=auth())

    with session_scope() as db:
        venue = db.execute(select(Venue).where(Venue.slug == "demo-venue")).scalar_one()
        count = sessionize.rebuild(db, venue)
        check("gap splits repeat of same track", count == 3, f"{count} plays")

        again = sessionize.rebuild(db, venue)
        check("sessionize idempotent",
              again == 3 and db.execute(select(Play)).scalars().all().__len__() == 3)

        result = associate.run(db, venue)
        check("in-window plays associated", result["matched"] == 2, str(result))
        check("out-of-window play left unassociated",
              result["unmatched"] == 1, str(result))

        rerun = associate.run(db, venue)
        check("associate idempotent", rerun["changed"] == 0, str(rerun))

    # Margins are configurable and re-running must pick up the change.
    from app.config import get_settings
    get_settings.cache_clear()
    os.environ["SETLIST_ASSOCIATION_BEFORE_MINUTES"] = "5000"
    try:
        with session_scope() as db:
            venue = db.execute(select(Venue).where(Venue.slug == "demo-venue")).scalar_one()
            widened = associate.run(db, venue)
            check("widened margin re-associates", widened["matched"] == 3,
                  str(widened))
    finally:
        del os.environ["SETLIST_ASSOCIATION_BEFORE_MINUTES"]
        get_settings.cache_clear()


def test_read_endpoints():
    section("read endpoints (spec 6)")
    fresh_db()
    client = TestClient(app)
    doors = dt.datetime(2026, 8, 14, 20, 0, tzinfo=dt.timezone.utc)
    with session_scope() as db:
        venue = db.execute(select(Venue).where(Venue.slug == "demo-venue")).scalar_one()
        db.add(Event(venue_id=venue.id, source="ldjson", external_id="e1",
                     title="Friday Night", doors_at=doors,
                     starts_at=doors, ends_at=doors + dt.timedelta(hours=4)))
        db.commit()
    client.post("/api/v1/detections",
                json={"venue": "demo-venue",
                      "detections": detection_payload(3, doors + dt.timedelta(hours=1))},
                headers=auth())
    with session_scope() as db:
        venue = db.execute(select(Venue).where(Venue.slug == "demo-venue")).scalar_one()
        sessionize.rebuild(db, venue)
        associate.run(db, venue)

    events = client.get("/api/v1/events", params={"venue": "demo-venue"},
                        headers=auth()).json()
    check("events listed", len(events) == 1 and events[0]["title"] == "Friday Night")

    plays = client.get("/api/v1/plays", params={"venue": "demo-venue"},
                       headers=auth()).json()
    check("plays listed with track", len(plays) == 1
          and plays[0]["track"]["artist"] == "Artist One")
    check("play carries event id", plays[0]["event_id"] == events[0]["id"])

    by_event = client.get("/api/v1/plays",
                          params={"venue": "demo-venue", "event_id": events[0]["id"]},
                          headers=auth()).json()
    check("plays filter by event", len(by_event) == 1)

    top = client.get("/api/v1/stats/top-tracks", params={"venue": "demo-venue"},
                     headers=auth()).json()
    check("top tracks aggregated",
          top["items"][0]["play_count"] == 1
          and top["items"][0]["detection_count"] == 3, str(top))

    summary = client.get("/api/v1/stats/summary", params={"venue": "demo-venue"},
                         headers=auth()).json()
    check("summary reports counts",
          summary["detections"] == 3 and summary["plays"] == 1, str(summary))

    forbidden = client.get("/api/v1/plays", params={"venue": "other"},
                           headers=auth())
    check("cannot read another venue", forbidden.status_code == 403)


# ----------------------------------------------------------------------
# scrapers
# ----------------------------------------------------------------------

LDJSON_PAGE = """
<html><head>
<script type="application/ld+json">
{"@context":"https://schema.org","@graph":[
 {"@type":"MusicEvent","@id":"evt-1","name":"Bass Night",
  "startDate":"2026-08-14T21:00:00-07:00","endDate":"2026-08-15T02:00:00-07:00",
  "doorTime":"2026-08-14T20:00:00-07:00","url":"https://venue.example/e/1",
  "performer":[{"@type":"MusicGroup","name":"DJ One"},{"name":"DJ Two"}]},
 {"@type":"WebSite","name":"not an event"}]}
</script>
<script type="application/ld+json">{ this is not json </script>
</head><body></body></html>
"""

TRIBE_JSON = {
    "events": [{
        "id": 4242, "title": "Tribe Night",
        "start_date": "2026-08-20 21:00:00",
        "end_date": "2026-08-21 02:00:00",
        "utc_start_date": "2026-08-21 04:00:00",
        "utc_end_date": "2026-08-21 09:00:00",
        "url": "https://venue.example/event/tribe-night",
        "organizer": [{"name": "Promoter X"}],
    }]
}

TICKETING_PAGE = """
<html><body>
<iframe src="https://dice.fm/partner/venue/embed"></iframe>
</body></html>
"""

DICE_EMBED = """
<html><head><script type="application/ld+json">
{"@type":"Event","@id":"dice-9","name":"Dice Show",
 "startDate":"2026-09-01T20:00:00Z","url":"https://dice.fm/event/9"}
</script></head><body></body></html>
"""

HTML_PAGE = """
<html><body>
<div class="event"><h3>Raw HTML Night</h3>
  <a href="/shows/77"><time datetime="2026-10-05T21:30:00-07:00">Oct 5</time></a>
</div>
<div class="event"><h3>Second Show</h3>
  <time datetime="2026-10-06T20:00:00-07:00">Oct 6</time>
</div>
</body></html>
"""


def mock_client(routes):
    def handler(request: httpx.Request) -> httpx.Response:
        for needle, response in routes:
            if needle in str(request.url):
                return response
        return httpx.Response(404, text="not found")
    return httpx.Client(transport=httpx.MockTransport(handler),
                        follow_redirects=True)


def test_scraper_ldjson():
    section("scraper rung 1: ld+json (spec 7)")
    nodes = extract(LDJSON_PAGE)
    check("event nodes found, non-events skipped", len(nodes) == 1,
          f"{len(nodes)} nodes")
    check("malformed block did not abort the parse", nodes[0]["@id"] == "evt-1")

    event = to_event(nodes[0], dt.timezone.utc)
    check("start parsed to UTC",
          event.starts_at == dt.datetime(2026, 8, 15, 4, 0, tzinfo=dt.timezone.utc),
          str(event.starts_at))
    check("doors parsed", event.doors_at is not None)
    check("performers flattened", event.performers == ["DJ One", "DJ Two"],
          str(event.performers))
    check("raw payload retained", event.raw["@id"] == "evt-1")

    with mock_client([("venue.example", httpx.Response(200, text=LDJSON_PAGE))]) as c:
        found = LdJsonScraper(dt.timezone.utc).fetch(c, "https://venue.example/events")
    check("scraper returns events", len(found) == 1)


def test_scraper_tribe():
    section("scraper rung 2: The Events Calendar (spec 7)")
    check("api url derived",
          api_url("https://venue.example/events/list/")
          == "https://venue.example/wp-json/tribe/events/v1/events")

    with mock_client([("wp-json", httpx.Response(200, json=TRIBE_JSON))]) as c:
        found = TribeScraper(dt.timezone.utc).fetch(c, "https://venue.example/events")
    check("tribe event parsed", len(found) == 1 and found[0].title == "Tribe Night")
    check("utc_start_date preferred over local",
          found[0].starts_at == dt.datetime(2026, 8, 21, 4, 0, tzinfo=dt.timezone.utc),
          str(found[0].starts_at))

    with mock_client([("wp-json", httpx.Response(404))]) as c:
        missing = TribeScraper().fetch(c, "https://venue.example/events")
    check("plugin absent falls through cleanly", missing == [])


def test_scraper_ticketing():
    section("scraper rung 3: ticketing embeds (spec 7)")
    found = detect(TICKETING_PAGE, "https://venue.example/events")
    check("dice embed detected", found == [("dice", "https://dice.fm/partner/venue/embed")],
          str(found))

    with mock_client([
        ("venue.example", httpx.Response(200, text=TICKETING_PAGE)),
        ("dice.fm", httpx.Response(200, text=DICE_EMBED)),
    ]) as c:
        scraper = TicketingScraper(dt.timezone.utc)
        events = scraper.fetch(c, "https://venue.example/events")
    check("embed followed and parsed", len(events) == 1, str(events))
    check("source records the platform", events[0].source == "dice")


def test_scraper_html():
    section("scraper rung 4: HTML (spec 7)")
    events = html_scraper.parse(HTML_PAGE, "https://venue.example/events",
                                dt.timezone.utc)
    check("both events found", len(events) == 2, f"{len(events)}")
    check("title taken from nearby heading",
          events[0].title == "Raw HTML Night", str(events[0].title))
    check("link resolved absolute",
          events[0].url == "https://venue.example/shows/77", str(events[0].url))
    check("synthetic ids are stable",
          html_scraper.parse(HTML_PAGE, "https://venue.example/events",
                             dt.timezone.utc)[0].external_id == events[0].external_id)


def test_scraper_ladder():
    section("scraper ladder order and persistence (spec 7)")
    fresh_db()
    # ld+json absent, Tribe present -> must land on rung 2.
    with session_scope() as db:
        venue = db.execute(select(Venue).where(Venue.slug == "demo-venue")).scalar_one()
        with mock_client([
            ("wp-json", httpx.Response(200, json=TRIBE_JSON)),
            ("venue.example", httpx.Response(200, text="<html><body>nothing</body></html>")),
        ]) as c:
            result = runner.scrape_venue(db, venue, client=c)
    check("fell through to tribe", result["rung"] == "tribe", str(result["rung"]))
    check("event persisted", result["created"] == 1, str(result))

    # Re-scraping the same event updates rather than duplicating.
    with session_scope() as db:
        venue = db.execute(select(Venue).where(Venue.slug == "demo-venue")).scalar_one()
        with mock_client([
            ("wp-json", httpx.Response(200, json=TRIBE_JSON)),
            ("venue.example", httpx.Response(200, text="<html></html>")),
        ]) as c:
            again = runner.scrape_venue(db, venue, client=c)
        rows = db.execute(select(Event)).scalars().all()
    check("re-scrape updates in place",
          again["updated"] == 1 and again["created"] == 0, str(again))
    check("no duplicate event rows", len(rows) == 1)
    check("raw payload stored", rows[0].raw["id"] == 4242)

    # ld+json wins when both are available.
    fresh_db()
    with session_scope() as db:
        venue = db.execute(select(Venue).where(Venue.slug == "demo-venue")).scalar_one()
        with mock_client([
            ("wp-json", httpx.Response(200, json=TRIBE_JSON)),
            ("venue.example", httpx.Response(200, text=LDJSON_PAGE)),
        ]) as c:
            first = runner.scrape_venue(db, venue, client=c)
    check("ld+json takes precedence", first["rung"] == "ldjson", str(first["rung"]))

    # Total failure must be reported, not raised.
    fresh_db()
    with session_scope() as db:
        venue = db.execute(select(Venue).where(Venue.slug == "demo-venue")).scalar_one()
        with mock_client([("nothing", httpx.Response(200))]) as c:
            dead = runner.scrape_venue(db, venue, client=c)
    check("all rungs failing returns a report", dead["found"] == 0
          and dead["rung"] is None, str(dead["rung"]))
    check("every rung was attempted", len(dead["attempts"]) == 4,
          str(len(dead["attempts"])))


def test_health_check():
    section("health alerting (spec 8)")
    fresh_db()
    now = dt.datetime(2026, 8, 14, 22, 0, tzinfo=dt.timezone.utc)

    with session_scope() as db:
        venue = db.execute(select(Venue).where(Venue.slug == "demo-venue")).scalar_one()
        db.add(Event(venue_id=venue.id, source="ldjson", external_id="e1",
                     title="Tonight", starts_at=now - dt.timedelta(hours=1),
                     ends_at=now + dt.timedelta(hours=2)))
        db.commit()

        problems = health.check(db, now=now, fire=False)
        check("silence during a scheduled event alerts", len(problems) == 1,
              str(problems))

        db.add(Detection(id=uuid.uuid4(), venue_id=venue.id, track_id=None,
                         captured_at=now - dt.timedelta(minutes=2),
                         level_dbfs=-20.0, method="nomatch",
                         client_version="0.2.0"))
        db.commit()
        check("recent detection clears the alert",
              health.check(db, now=now, fire=False) == [])

        quiet_night = now + dt.timedelta(days=3)
        check("silence outside any event does not alert",
              health.check(db, now=quiet_night, fire=False) == [])


def main():
    test_auth()
    test_idempotency()
    test_nomatch_rows()
    test_heartbeat()
    test_sessionize_and_associate()
    test_read_endpoints()
    test_scraper_ldjson()
    test_scraper_tribe()
    test_scraper_ticketing()
    test_scraper_html()
    test_scraper_ladder()
    test_health_check()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
