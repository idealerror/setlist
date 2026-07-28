"""Store-and-forward sync (spec 2.3, spec 4).

Local SQLite is the source of truth. This worker pushes rows the server has not
acknowledged and marks them synced; it never deletes, never blocks capture, and
never treats an unreachable server as an error worth stopping for. Venue
internet drops mid-set and the home server can be down for days -- both must
cost nothing except a growing local queue.

Idempotency comes from the client-generated UUID on every detection: the server
upserts with ON CONFLICT DO NOTHING, so a batch that was actually received but
whose response we never saw is free to send again.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import time

import httpx

from . import storage


class SyncDisabled(Exception):
    """No server configured; the client runs standalone."""


class Syncer:
    def __init__(self, cfg, conn, client_version: str, client=None):
        self.cfg = cfg
        self.conn = conn
        self.client_version = client_version
        self._failures = 0
        self._started = time.monotonic()
        #: Set by the capture loop so the heartbeat can carry it (spec 8).
        self.ceiling_tripped = False

        if not cfg.server.url or not cfg.server.token:
            raise SyncDisabled()

        self._base = cfg.server.url.rstrip("/")
        # `client` is injectable so tests can drive the real server app over an
        # ASGI transport instead of a socket.
        self._client = client or httpx.AsyncClient(
            timeout=cfg.server.request_timeout_s,
            verify=cfg.server.verify_tls,
            headers={"Authorization": f"Bearer {cfg.server.token}"},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------

    def _backoff(self) -> float:
        steps = self.cfg.server.retry_backoff_s
        if self._failures == 0:
            return self.cfg.server.sync_interval_s
        return steps[min(self._failures - 1, len(steps) - 1)]

    def _payload(self, rows) -> list[dict]:
        out = []
        for row in rows:
            out.append({
                "id": row["id"],
                "captured_at": row["captured_at"],
                "shazam_key": row["shazam_key"],
                "title": row["title"],
                "artist": row["artist"],
                "isrc": row["isrc"],
                "level_dbfs": row["level_dbfs"],
                "method": row["method"],
                "client_version": row["client_version"],
            })
        return out

    async def push_once(self) -> dict:
        """Push one batch. Returns a small report; raises on transport error."""
        rows = storage.unsynced_batch(self.conn, self.cfg.server.batch_size)
        if not rows:
            return {"sent": 0, "accepted": 0, "duplicates": 0, "pending": 0}

        response = await self._client.post(
            f"{self._base}/api/v1/detections",
            json={"venue": self.cfg.venue.slug,
                  "detections": self._payload(rows)},
        )
        if response.status_code in (401, 403):
            # A wrong token will never fix itself by retrying; make it loud but
            # keep capturing.
            raise RuntimeError(
                f"server rejected our credentials ({response.status_code}): "
                f"{response.text[:200]}")
        response.raise_for_status()
        result = response.json()

        # Mark synced only after the server has confirmed. Duplicates count as
        # confirmed: the server already holds them.
        storage.mark_synced(self.conn, [row["id"] for row in rows])
        return {
            "sent": len(rows),
            "accepted": result.get("accepted", 0),
            "duplicates": result.get("duplicates", 0),
            "pending": storage.unsynced_count(self.conn),
        }

    async def drain(self, max_batches: int = 100) -> dict:
        """Push until the queue is empty or the cap is hit."""
        totals = {"sent": 0, "accepted": 0, "duplicates": 0, "pending": 0}
        for _ in range(max_batches):
            report = await self.push_once()
            for key in ("sent", "accepted", "duplicates"):
                totals[key] += report[key]
            totals["pending"] = report["pending"]
            if report["sent"] == 0 or report["pending"] == 0:
                break
        return totals

    async def heartbeat(self) -> bool:
        response = await self._client.post(
            f"{self._base}/api/v1/heartbeat",
            json={
                "venue": self.cfg.venue.slug,
                "client_version": self.client_version,
                "queue_depth": storage.unsynced_count(self.conn),
                "uptime_s": int(time.monotonic() - self._started),
                "ceiling_tripped": self.ceiling_tripped,
            },
        )
        response.raise_for_status()
        return True

    # ------------------------------------------------------------------

    async def run_forever(self) -> None:
        """Background task. Must never raise into the capture loop."""
        next_heartbeat = 0.0
        while True:
            try:
                report = await self.drain()
                if report["sent"]:
                    print(f"  sync: {report['accepted']} accepted, "
                          f"{report['duplicates']} dup, "
                          f"{report['pending']} pending")

                if time.monotonic() >= next_heartbeat:
                    await self.heartbeat()
                    next_heartbeat = (time.monotonic()
                                      + self.cfg.server.heartbeat_interval_s)

                self._failures = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._failures += 1
                pending = storage.unsynced_count(self.conn)
                # Only shout on the first failure and then occasionally; a
                # week-long outage should not fill the log.
                if self._failures == 1 or self._failures % 20 == 0:
                    print(f"  ! sync unavailable ({type(exc).__name__}: "
                          f"{str(exc)[:120]}); {pending} queued locally")

            await asyncio.sleep(self._backoff())


def build(cfg, conn, client_version: str, client=None) -> Syncer | None:
    """Returns None when no server is configured."""
    try:
        return Syncer(cfg, conn, client_version, client=client)
    except SyncDisabled:
        return None
