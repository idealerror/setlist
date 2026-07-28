# Venue setlist logger

A PC in a music venue listens to the room, identifies the songs playing by audio
fingerprinting, and logs them with timestamps. Detections sync to a self-hosted
API, which associates each one with the event it happened during — scraped from
the venue's public calendar — and exposes the result as analytics: what got
played, when, by which DJ, and how often.

It answers questions a venue can't otherwise answer. What actually got played
last Friday. Which tracks show up across every DJ. Whether the weekday
background playlist has gone stale. When a track first appeared and how its
rotation changed.

## No audio is retained. Ever.

This is a hard architectural constraint, not a setting.

A captured chunk reaches the filesystem **only** as a temporary WAV, **only**
when a recognizer call is actually being made, and is deleted in a `finally`
block. There is deliberately no option to keep it, relocate it, or increase how
long it lives. The system is designed so that it *cannot* produce a recording of
the room, and there's a test asserting no `.wav` survives a run.

Everything downstream stores metadata: a track title, an artist, a timestamp, a
level in dBFS.

## How it works

```
VENUE PC (Windows)
  record 12s ──▶ level gate ──▶ chroma changed? ──▶ local cache ──▶ Shazam API
                     │ quiet        │ no               │ hit           │
                     ▼              ▼                  ▼               ▼
                  (nothing)     (nothing)        (no network)     (one call)
                                                        │               │
                                                        └───────┬───────┘
                                                                ▼
                                              SQLite  ← source of truth
                                                                │
                                                     sync worker │ Tailscale
════════════════════════════════════════════════════════════════ │ ═══════════
HOME SERVER (Proxmox LXC)                                        ▼
                                                    FastAPI ──▶ PostgreSQL 16
                                                       ▲            ▲
                                              event scraper   nightly jobs
                                                       │            │
                                              venue website     Metabase
```

Each stage exists to avoid the next one, because the recognition API is the
scarce resource.

**The level gate.** A quiet room means nothing is happening, so nothing is
spent on it. The audio level *is* the event detector — no schedule required.

**Chroma change detection.** A 12-dimensional chroma vector per chunk, compared
against the chunk that produced the current identification. If the music hasn't
changed, nothing is logged and nothing is called. A five-minute track drops from
~10 API calls to ~1.

**Local fingerprint cache.** When the API returns a match, the chunk is
fingerprinted properly — spectrogram, local peak picking, hash pairs of
`(freq1, freq2, Δt)` — and stored against the track. Later chunks query local
hashes first, and a confident match skips the network entirely. For a venue's
weekday playlist of a few hundred rotating tracks, this converges to almost
entirely cache hits within a couple of weeks while still logging every play.

**Adaptive backoff with jitter.** After a confirmed match the poll interval
stretches 30 → 60 → 120s while the music holds steady, resetting immediately on
a change. ±20% jitter keeps calls off exact multiples of a minute, which is the
pattern a rate limiter notices.

Together these cut API usage by roughly an order of magnitude versus naive
30-second polling, which would be 2,880 calls/day from a single IP.

## Repository layout

| Path | Contents |
|---|---|
| [`client/`](client/) | Windows capture client. Python 3.12. |
| [`server/`](server/) | FastAPI + PostgreSQL, Docker Compose, scrapers, jobs. |
| [`ops/`](ops/) | Scheduled Task installer, supervisor loop, updater. |
| [`tests/`](tests/) | Client↔server integration tests. |

## Quick start

### Client

```bash
py -3.12 -m pip install -e client
```

```bash
py -3.12 -m setlist list-devices
```

Copy `client/config.example.toml` to `client/config.toml`, set `audio.device`,
then:

```bash
py -3.12 -m setlist run
```

Output looks like this — the middle line is change detection saving a call:

```
[23:26:59] checking (-24 dBFS, chroma 0.000)
  MATCH: JISOO - earthquake, +7156 hashes
[23:27:48] checking (-25 dBFS, chroma 0.900)
  MATCH: WENGIE & i-dle - EMPIRE, +6457 hashes
[23:28:26] stable (chroma 0.989), next in 60s
```

Other commands: `stats`, `sessionize`, `export plays|detections FILE.csv`,
`sync`.

### Server

```bash
cd server && cp .env.example .env && docker compose up -d
```

Then create a venue and issue it a token. See
[`server/README.md`](server/README.md).

## Data model

`detections` is **append-only and immutable** — one row per identification
attempt that produced an answer, keyed by a client-generated UUIDv4.

`plays` is **derived** — consecutive detections of the same track collapsed into
a single play, split when the gap exceeds a threshold.

That split matters. The gap threshold is a guess until there's real data to look
at, so the collapsed form has to be reproducible from the raw log rather than
being the only thing stored. `sessionize` can be re-run over any date range
after retuning, and it's idempotent.

The client UUID is what makes syncing safe: the server upserts with
`ON CONFLICT (id) DO NOTHING`, so a batch that was received but whose response
never made it back is free to send again.

## Design decisions

**Always listen; associate events afterwards.** Capture is never gated on the
scraper, the network, or the server. A broken scraper, an unlisted show, a set
that overruns, or an unexpected soundcheck all still produce data. Associating a
detection with an event is a server-side join at query time. Wrong metadata can
be re-derived; uncaptured audio is gone forever.

**Local SQLite is the source of truth.** The client writes locally first,
always. A separate worker pushes unsynced rows oldest-first. Venue internet
drops mid-set and the home server can be down for days; neither loses a row, and
the integration tests cover exactly that.

**The recognizer is an interface, not a dependency.** `shazamio` drives an
endpoint Shazam doesn't document or support, and it may break without notice.
Everything above it talks to a `Recognizer` protocol returning
`TrackMatch | None`, so adding an AudD or ACRCloud adapter is one new class and
no changes anywhere else.

**Every threshold is config, not a literal.** Chroma similarity, backoff steps,
cache hash minimum, play gap, association margins, alert windows.

## Known constraints

**Python 3.10–3.12 on the client. Not 3.13+.** `shazamio-core` publishes Windows
wheels for cp39–cp312 only; above that, pip falls back to a source build that
fails on the MSVC linker. `shazamio` also depends on `pydub`, which imports the
`audioop` module removed from the standard library in 3.13.

**Not a Windows Service.** Session 0 has no audio device access, so a service
registered via NSSM or `sc create` fails inside PortAudio in ways that are
miserable to diagnose. The supported arrangement is auto-login plus a
logon-triggered Scheduled Task running in the interactive session.

**Two capture backends.** `sounddevice` handles real input devices — the
production path, a line feed from the desk — and exposes `default_samplerate`,
which is what lets the client honour the device's native rate as WASAPI shared
mode requires. `soundcard` handles loopback for development, because
`sounddevice` cannot: its `WasapiSettings` exposes only
`exclusive`/`auto_convert`/`explicit_sample_format`, and the PortAudio it bundles
(V19.7.0-devel) surfaces no loopback devices.

**Recognition accuracy in a live room.** Beatmatched and pitch-shifted tracks
defeat fingerprinting and crowd noise hurts. A desk feed dramatically
outperforms an open mic. Expect materially lower hit rates on DJ sets than on a
background playlist — that's the technology, not a bug.

**Chroma is only marginally discriminative.** Measured on real music: 0.900
between two *different* tracks, 0.960 between two chunks of the *same* track. No
threshold separates those cleanly. Two mitigations ship as a result — the
comparison anchors to the last identified chunk rather than the previous chunk
(otherwise a crossfade walks the reference along with the music and the incoming
track is never logged), and `max_stable_polls` forces a periodic re-check
regardless of similarity.

**PostgreSQL is not yet exercised.** The server's tests and its Alembic
migration both run against SQLite via dialect variants, and the migration was
verified to produce a schema matching the models exactly — but the first
`docker compose up` will be the first contact with a real PostgreSQL 16.

## Tests

No pytest dependency, so they run on a minimal venue install when something
needs diagnosing in place.

```bash
py -3.12 client/tests/test_client.py
```

```bash
py -3.12 server/tests/test_server.py
```

```bash
py -3.12 tests/test_integration.py
```

The integration suite drives the real client sync worker against the real server
app over an ASGI transport, covering idempotent replay, a server outage with
zero data loss, and full recovery.

## Status

Client and server are built and tested. Deployment automation exists for the
venue PC. Not yet done: exercising PostgreSQL in anger, and running the scraper
against a real venue calendar — the fallback ladder is tested against fixtures.

## Legal note

Recognition uses [`shazamio`](https://github.com/shazamio/ShazamIO), a
reverse-engineered client for a private Shazam endpoint. That is against
Shazam's terms of service. This is a personal, non-commercial project; if you
need something durable or commercial, swap in a licensed provider — the
`Recognizer` protocol exists precisely so that's a one-class change.

Recording or analysing audio in a space may carry obligations depending on your
jurisdiction, even when no audio is retained. Check before deploying somewhere
you don't control.

## License

MIT — see [LICENSE](LICENSE).

That grant covers the code in this repository only. Dependencies carry their own
terms, and `shazamio` in particular reaches a Shazam endpoint in a way that
breaches Shazam's terms of service. An MIT licence on this repository does not
license that behaviour, and cannot — see the legal note above.
