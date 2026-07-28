# Venue setlist logger — capture client

Passively identifies music playing in a room and logs it to local SQLite.
Phases 1 and 2 of the build spec.

**Audio is never retained.** A chunk reaches the filesystem only as a temp WAV,
deleted in a `finally` block, and only when a recognizer call is actually being
made. There is deliberately no option to keep or relocate that file.

## Requirements

Python **3.10–3.12**. Not 3.13+: `shazamio-core` publishes Windows wheels for
cp39–cp312 only, so pip falls back to a Rust build that fails on the MSVC
linker, and `shazamio` pulls in `pydub`, which imports the `audioop` module
removed from the stdlib in 3.13.

```bash
py -3.12 -m pip install numpy librosa scipy sounddevice soundcard shazamio
```

## Setup

```bash
py -3.12 -m setlist list-devices
```

WASAPI entries come first and are the ones to pick — MME truncates device names
to 31 characters and DirectSound adds latency. Then copy `config.example.toml`
to `config.toml` and set `audio.device` to an index or a name substring
(a substring survives Windows renumbering devices).

```bash
py -3.12 -m setlist run
```

## Commands

| Command | Purpose |
|---|---|
| `run` | Listen and log. `--max-runtime SECONDS` exits cleanly for a scheduled restart. |
| `list-devices` | Show capture sources. |
| `sessionize` | Rebuild `plays` from `detections`. `--since/--until/--gap-minutes`. |
| `export plays\|detections FILE.csv` | Write a CSV. |
| `stats` | Cache hit rate, API calls today, pending sync depth. |

`--config` and `--db` are global.

## How a poll is decided

    record → level gate → chroma changed? → local cache → recognizer API

Each stage exists to avoid the next. A quiet room costs nothing; unchanged
music costs nothing; a track already in the fingerprint cache costs nothing.
Capture is never gated on the network or the recognizer — if everything
downstream is broken the loop keeps running, because uncaptured audio is gone
forever while wrong metadata can be re-derived.

## Data model

`detections` is append-only and immutable: one row per identification attempt
that produced an answer, keyed by a client-generated UUID so the eventual
server sync is idempotent under retry.

`plays` is **derived** — sessionize.py collapses consecutive detections of the
same track. It can be deleted and rebuilt at any time, which is the point:
`play_gap_minutes` will need retuning, and only the raw log makes that possible.

## Two capture backends

`sounddevice` handles real input devices — the production path, a desk line
feed into the Scarlett. It exposes `default_samplerate`, which is what lets the
client honour the device's native rate (WASAPI shared mode requires it: 48000
on the Scarlett, not 44100).

`soundcard` handles loopback, for development against whatever this PC is
playing. sounddevice cannot do it — its `WasapiSettings` takes only
`exclusive`/`auto_convert`/`explicit_sample_format`, and the PortAudio it
bundles (V19.7.0-devel) exposes no loopback devices.

## Tests

```bash
py -3.12 tests/test_client.py
```

No pytest dependency, so it runs on a minimal venue install when something
needs diagnosing in place.

## Not yet built

Phases 3–7 — the FastAPI/Postgres server, sync worker, event scraper,
association job, and ops — are separate deliverables. The client already writes
`detections.synced_at` and reports queue depth via `stats`, so the sync worker
has somewhere to attach.
