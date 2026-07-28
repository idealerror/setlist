# Venue setlist logger — server

FastAPI + Postgres 16, running under Docker Compose inside a Proxmox LXC.
Receives detections from venue clients, scrapes venue calendars, associates
plays with events, and backs Metabase.

## Deploy

```bash
cp .env.example .env          # set POSTGRES_PASSWORD, then BIND_ADDR
docker compose up -d
```

`migrate` runs `alembic upgrade head` and must complete before `api` and
`scheduler` start; Compose enforces that.

**Networking.** Nothing is port-forwarded. Set `BIND_ADDR` to the LXC's
Tailscale address so the API is reachable only on the tailnet — it defaults to
`127.0.0.1` so a missing `.env` can't accidentally expose it. Postgres isn't
published at all. Metabase stays on loopback and goes out through a Cloudflare
Tunnel for mobile.

## Add a venue

```bash
docker compose exec api python manage.py create-venue --slug demo-venue --name "Demo Venue" --timezone America/Los_Angeles --events-url https://venue.example/events
```

```bash
docker compose exec api python manage.py issue-token --slug demo-venue
```

The token is printed once. Only its sha256 is stored, so reissue to rotate.
Put it in the client's `config.toml` under `[server]`.

## API

| Endpoint | Purpose |
|---|---|
| `POST /api/v1/detections` | Batch upsert, idempotent on client UUID. Cap 500. |
| `POST /api/v1/heartbeat` | Liveness, queue depth, ceiling state. |
| `GET /api/v1/events` | `?venue=&from=&to=` |
| `GET /api/v1/plays` | `?venue=&event_id=&from=&to=` |
| `GET /api/v1/stats/top-tracks` | `?venue=&from=&to=&limit=` |
| `GET /api/v1/stats/summary` | Counts, cache hit rate, unassociated plays. |
| `GET /health` | Unauthenticated liveness probe. |

Bearer token per venue. A token may only read and write its own venue; the body's
`venue` field must match, or the request is rejected 403.

## Jobs

The `scheduler` service runs three things on fixed cadences — stdlib only, no
cron sidecar:

- **Scrape**, 04:00 and 16:00 UTC. Walks the fallback ladder per venue.
- **Nightly**, 05:00 UTC. Sessionizes detections into plays, then associates
  plays with events.
- **Health check**, every 5 minutes. Alerts when a venue is silent during a
  scheduled event.

All are runnable by hand:

```bash
docker compose exec api python manage.py nightly --since 2026-07-01
```

Both jobs are idempotent over any range, which is the point: retune
`SETLIST_PLAY_GAP_MINUTES` or the association margins in `.env`, re-run, and
history is rebuilt from the immutable detection log.

## The scraper ladder

Tried in order, stopping at the first rung that returns events:

1. **`application/ld+json`** — schema.org `Event`. Most modern venue sites emit
   it; clean start times, performers, URLs.
2. **The Events Calendar REST API** — `/wp-json/tribe/events/v1/events`.
3. **Ticketing embeds** — detects DICE, Eventbrite, Ticketmaster, See Tickets,
   Songkick, Bandsintown; follows the embed and re-runs the ld+json parser
   against it. When that fails it logs the platform and URL so a dedicated
   adapter can be written against a known target.
4. **HTML** — anchors on `<time datetime>`. Brittle; expect to revisit it.

Every rung stores the untouched payload in `events.raw`, so fixing a parser
never means re-scraping.

## Alerting

Set `SETLIST_HOME_ASSISTANT_WEBHOOK_URL` to receive:

- `venue_quiet` — no detections for `SETLIST_QUIET_ALERT_MINUTES` during a
  window where an event is scheduled.
- `api_ceiling_tripped` — a client reported hitting its daily API ceiling.

Without a webhook, alerts are logged. Delivery is best-effort by design: a dead
webhook must never take an API request or a nightly job down with it.

## Backups

```bash
./backup.sh
```

Custom-format `pg_dump`, verified with `pg_restore --list` before being trusted,
30-day retention. Run it from host cron:

```
15 6 * * *  cd /srv/venue-setlist/server && ./backup.sh >> backup.log 2>&1
```

## Tests

```bash
py -3.12 tests/test_server.py
```

Runs against SQLite so no database daemon is needed — the models use dialect
variants (`JSONB`/`JSON`, native/portable UUID) so the same code runs on both.
The Alembic migration is written the same way and is verified against SQLite
too. **Production is Postgres 16, and it has not been exercised here** — see
the note in the root README.
