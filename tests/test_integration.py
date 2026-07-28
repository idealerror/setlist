"""End-to-end: the real client sync worker against the real server app.

Run with:  py -3.12 tests/test_integration.py

The client talks to the server over an ASGI transport rather than a socket, so
this exercises the actual request/response path, auth, and idempotency without
needing a running uvicorn or a Postgres daemon.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
import sys
import tempfile
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "client"))
sys.path.insert(0, str(ROOT / "server"))

_TMP = Path(tempfile.mkdtemp(prefix="setlist_integration_"))
os.environ["SETLIST_DATABASE_URL"] = f"sqlite:///{_TMP / 'server.db'}"
os.environ["SETLIST_HOME_ASSISTANT_WEBHOOK_URL"] = ""

import httpx
from sqlalchemy import func, select

from app import jobs
from app.auth import generate_token, hash_token
from app.db import init_engine, session_scope
from app.main import app as server_app
from app.models import Base, Detection as ServerDetection
from app.models import Heartbeat, Track, Venue
from setlist import config, storage, sync

FAILURES: list[str] = []
TOKEN = generate_token()
VENUE = "demo-venue"


def check(label, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  ({detail})" if detail else ""))
    if not ok:
        FAILURES.append(label)


def section(title):
    print(f"\n=== {title} ===")


def reset_server():
    engine = init_engine()
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with session_scope() as db:
        db.add(Venue(slug=VENUE, name="Demo Venue", timezone="UTC",
                     token_hash=hash_token(TOKEN)))
        db.commit()


def client_config(db_path: Path, **server_overrides):
    cfg = config.load(None)
    cfg.venue.slug = VENUE
    cfg.storage.db_path = str(db_path)
    cfg.root = db_path.parent
    cfg.server.url = "http://server"
    cfg.server.token = TOKEN
    for key, value in server_overrides.items():
        setattr(cfg.server, key, value)
    return cfg


def make_http(transport=None):
    return httpx.AsyncClient(
        transport=transport or httpx.ASGITransport(app=server_app),
        base_url="http://server",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )


def seed(conn, count, start=None, key="k1"):
    start = start or dt.datetime(2026, 8, 14, 21, 0, tzinfo=dt.timezone.utc)
    for i in range(count):
        storage.insert_detection(conn, storage.Detection(
            id=storage.new_id(),
            captured_at=storage.iso(start + dt.timedelta(seconds=30 * i)),
            shazam_key=key, title="Track One", artist="Artist One",
            isrc="USUM71703861", level_dbfs=-22.0, method="shazam",
            client_version="0.2.0"))


def server_count():
    with session_scope() as db:
        return db.execute(select(func.count(ServerDetection.id))).scalar_one()


# ----------------------------------------------------------------------

def test_happy_path():
    section("client -> server sync (spec 2.3, spec 4)")
    reset_server()
    db_path = _TMP / "c1.db"
    conn = storage.connect(db_path)
    seed(conn, 12)

    async def go():
        async with make_http() as http:
            syncer = sync.build(client_config(db_path), conn, "0.2.0", client=http)
            report = await syncer.drain()
            await syncer.heartbeat()
            return report

    report = asyncio.run(go())
    check("all detections sent", report["sent"] == 12, str(report))
    check("server accepted them", report["accepted"] == 12, str(report))
    check("queue drained", storage.unsynced_count(conn) == 0)
    check("server holds the rows", server_count() == 12, str(server_count()))

    with session_scope() as db:
        check("one track row created",
              db.execute(select(func.count(Track.id))).scalar_one() == 1)
        beat = db.execute(select(Heartbeat)).scalars().one()
        check("heartbeat reports drained queue", beat.queue_depth == 0,
              str(beat.queue_depth))
    conn.close()


def test_idempotent_replay():
    section("idempotent replay (spec 4.2)")
    reset_server()
    db_path = _TMP / "c2.db"
    conn = storage.connect(db_path)
    seed(conn, 5)

    async def go():
        async with make_http() as http:
            syncer = sync.build(client_config(db_path), conn, "0.2.0", client=http)
            first = await syncer.drain()
            # Simulate the ack being lost: the server got them, we never knew.
            conn.execute("UPDATE detections SET synced_at = NULL")
            conn.commit()
            second = await syncer.drain()
            return first, second

    first, second = asyncio.run(go())
    check("first pass accepted", first["accepted"] == 5, str(first))
    check("replay recognised as duplicates", second["duplicates"] == 5, str(second))
    check("no rows duplicated server-side", server_count() == 5, str(server_count()))
    check("replay still clears the local queue",
          storage.unsynced_count(conn) == 0)
    conn.close()


def test_outage_and_recovery():
    section("server outage and recovery (spec 2.3)")
    reset_server()
    db_path = _TMP / "c3.db"
    conn = storage.connect(db_path)
    seed(conn, 8)

    def down(request):
        return httpx.Response(503, text="maintenance")

    async def go():
        async with make_http(httpx.MockTransport(down)) as http:
            syncer = sync.build(client_config(db_path), conn, "0.2.0", client=http)
            failed = False
            try:
                await syncer.drain()
            except Exception:
                failed = True
            return failed

    failed = asyncio.run(go())
    check("outage surfaced as an error", failed)
    check("nothing marked synced during the outage",
          storage.unsynced_count(conn) == 8, str(storage.unsynced_count(conn)))
    check("server received nothing", server_count() == 0)

    # Capture keeps running through the outage and the queue keeps growing.
    seed(conn, 4, start=dt.datetime(2026, 8, 14, 23, 0, tzinfo=dt.timezone.utc))
    check("capture continued into the queue",
          storage.unsynced_count(conn) == 12)

    async def recover():
        async with make_http() as http:
            syncer = sync.build(client_config(db_path), conn, "0.2.0", client=http)
            return await syncer.drain()

    report = asyncio.run(recover())
    check("full backlog drained on recovery", report["sent"] == 12, str(report))
    check("nothing lost", server_count() == 12, str(server_count()))
    check("queue empty", storage.unsynced_count(conn) == 0)
    conn.close()


def test_batching_and_order():
    section("batch cap and ordering (spec 6)")
    reset_server()
    db_path = _TMP / "c4.db"
    conn = storage.connect(db_path)
    # More than one batch, and more than the server's 500 cap in total.
    seed(conn, 12)

    async def go():
        async with make_http() as http:
            cfg = client_config(db_path, batch_size=5)
            syncer = sync.build(cfg, conn, "0.2.0", client=http)
            first = await syncer.push_once()
            return first

    first = asyncio.run(go())
    check("batch size respected", first["sent"] == 5, str(first))
    check("remainder still pending", first["pending"] == 7, str(first))

    with session_scope() as db:
        sent = db.execute(
            select(ServerDetection.captured_at)
            .order_by(ServerDetection.captured_at)).scalars().all()
        earliest_local = conn.execute(
            "SELECT captured_at FROM detections ORDER BY captured_at LIMIT 1"
        ).fetchone()[0]
    check("oldest sent first",
          sent[0].replace(tzinfo=dt.timezone.utc).isoformat(timespec="seconds")
          == earliest_local, f"{sent[0]} vs {earliest_local}")

    async def rest():
        async with make_http() as http:
            cfg = client_config(db_path, batch_size=5)
            return await sync.build(cfg, conn, "0.2.0", client=http).drain()

    report = asyncio.run(rest())
    check("drain finishes the rest", report["pending"] == 0, str(report))
    check("all twelve landed", server_count() == 12)

    over_cap = client_config(db_path, batch_size=501)
    check("client default batch respects the server cap",
          config.load(None).server.batch_size == 500)
    conn.close()


def test_bad_credentials():
    section("credential failure is loud but non-fatal")
    reset_server()
    db_path = _TMP / "c5.db"
    conn = storage.connect(db_path)
    seed(conn, 3)

    async def go():
        async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=server_app),
                base_url="http://server",
                headers={"Authorization": "Bearer wrong-token"}) as http:
            cfg = client_config(db_path)
            cfg.server.token = "wrong-token"
            syncer = sync.build(cfg, conn, "0.2.0", client=http)
            try:
                await syncer.drain()
                return None
            except Exception as exc:
                return str(exc)

    message = asyncio.run(go())
    check("rejected credentials raise", message is not None)
    check("message names the cause", "credentials" in (message or ""),
          str(message))
    check("queue preserved", storage.unsynced_count(conn) == 3)
    conn.close()


def test_ceiling_alert():
    section("API ceiling alert reaches the server (spec 8)")
    reset_server()
    db_path = _TMP / "c6.db"
    conn = storage.connect(db_path)

    fired: list = []
    original = jobs.alerts.notify

    def spy(kind, payload):
        fired.append((kind, payload))
        return True

    import app.api.heartbeat as hb
    hb.notify = spy
    try:
        async def go():
            async with make_http() as http:
                syncer = sync.build(client_config(db_path), conn, "0.2.0",
                                    client=http)
                await syncer.heartbeat()
                syncer.ceiling_tripped = True
                await syncer.heartbeat()

        asyncio.run(go())
    finally:
        hb.notify = original

    check("no alert while healthy then one on trip", len(fired) == 1, str(fired))
    check("alert names the venue",
          fired and fired[0][1]["venue"] == VENUE, str(fired))
    check("alert kind is specific",
          fired and fired[0][0] == "api_ceiling_tripped", str(fired))
    conn.close()


def test_sync_disabled():
    section("standalone operation")
    db_path = _TMP / "c7.db"
    conn = storage.connect(db_path)
    cfg = config.load(None)
    cfg.storage.db_path = str(db_path)
    check("no server configured means no syncer",
          sync.build(cfg, conn, "0.2.0") is None)
    conn.close()


def main():
    test_happy_path()
    test_idempotent_replay()
    test_outage_and_recovery()
    test_batching_and_order()
    test_bad_credentials()
    test_ceiling_alert()
    test_sync_disabled()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
