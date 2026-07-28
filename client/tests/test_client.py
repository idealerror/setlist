"""Self-contained checks for the capture client.

Run with:  py -3.12 tests/test_client.py

No pytest dependency on purpose -- the venue PC gets a minimal install, and
these need to be runnable there to diagnose a misbehaving deployment.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import glob
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np

from setlist import capture, config, fingerprint, sessionize, storage
from setlist.loop import Runner
from setlist.recognize import RateLimited, TrackMatch

SR = 48000
FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  ({detail})" if detail else ""))
    if not ok:
        FAILURES.append(label)


def section(title: str) -> None:
    print(f"\n=== {title} ===")


# ----------------------------------------------------------------------
# synthetic audio
# ----------------------------------------------------------------------

def tone_bed(freqs, seconds=20.0, seed=0):
    """A deterministic, spectrally busy signal: a chord plus harmonics, with
    transients so the spectrogram has distinct local peaks to latch onto."""
    rng = np.random.default_rng(seed)
    t = np.arange(int(seconds * SR)) / SR
    sig = np.zeros_like(t)
    for f in freqs:
        for harmonic in (1, 2, 3, 5):
            sig += (1.0 / harmonic) * np.sin(2 * np.pi * f * harmonic * t
                                             + rng.uniform(0, 2 * np.pi))
    # Percussive clicks every 500ms give the peak picker sharp time structure.
    for onset in range(0, int(seconds * 2)):
        start = int(onset * 0.5 * SR)
        end = min(start + 1200, sig.size)
        sig[start:end] += 3.0 * rng.standard_normal(end - start) * np.linspace(1, 0, end - start)
    sig += 0.01 * rng.standard_normal(sig.size)
    sig /= np.abs(sig).max()
    return (sig * 20000).astype(np.int16)


MUSIC_A = tone_bed([220.0, 277.2, 329.6], seed=1)      # A major-ish
MUSIC_B = tone_bed([311.1, 392.0, 466.2], seed=2)      # D# minor-ish


def excerpt(signal, start_s, length_s=12.0):
    a = int(start_s * SR)
    return signal[a:a + int(length_s * SR)]


# ----------------------------------------------------------------------

def test_config():
    section("config")
    cfg = config.load(None)
    check("defaults load", cfg.cache.min_aligned_hashes == 8)

    example = Path(__file__).resolve().parent.parent / "config.example.toml"
    loaded = config.load(example)
    check("config.example.toml parses", loaded.audio.chunk_seconds == 12.0)
    check("db_path resolves against config dir",
          loaded.db_path.parent == example.parent)

    with tempfile.TemporaryDirectory() as tmp:
        bad = Path(tmp) / "bad.toml"
        bad.write_text("[audio]\nbackend = \"telepathy\"\n", encoding="utf-8")
        try:
            config.load(bad)
            check("rejects invalid backend", False)
        except config.ConfigError as exc:
            check("rejects invalid backend", "backend" in str(exc))

        typo = Path(tmp) / "typo.toml"
        typo.write_text("[cache]\nmin_aligned_hash = 4\n", encoding="utf-8")
        try:
            config.load(typo)
            check("rejects unknown key", False)
        except config.ConfigError as exc:
            check("rejects unknown key", "not a recognised setting" in str(exc))

        # Notepad and PowerShell's Out-File write a BOM by default on Windows.
        bom = Path(tmp) / "bom.toml"
        bom.write_text("[audio]\nchunk_seconds = 15.0\n", encoding="utf-8-sig")
        try:
            check("BOM-prefixed config still parses",
                  config.load(bom).audio.chunk_seconds == 15.0)
        except config.ConfigError as exc:
            check("BOM-prefixed config still parses", False, str(exc)[:60])


def test_storage_migration():
    section("storage")
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "s.db"

        # Build a v1-format database, as the phase-1 prototype left behind.
        import sqlite3
        legacy = sqlite3.connect(db)
        legacy.execute("CREATE TABLE plays (id INTEGER PRIMARY KEY, "
                       "first_heard TEXT, last_heard TEXT, title TEXT, "
                       "artist TEXT, shazam_key TEXT, url TEXT)")
        legacy.execute("INSERT INTO plays (first_heard, last_heard, title, artist)"
                       " VALUES ('2026-01-01T00:00:00','2026-01-01T00:05:00',"
                       "'Old','Band')")
        legacy.commit()
        legacy.close()

        conn = storage.connect(db)
        rows = conn.execute("SELECT COUNT(*) FROM plays_v1_legacy").fetchone()[0]
        check("legacy plays preserved", rows == 1)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(plays)")}
        check("new plays schema installed", "detection_count" in cols)

        det = storage.Detection(
            id=storage.new_id(), captured_at=storage.iso(storage.utc_now()),
            shazam_key="k1", title="T", artist="A", isrc=None,
            level_dbfs=-20.0, method="shazam", client_version="test")
        storage.insert_detection(conn, det)
        check("detection stored with uuid", storage.detection_count(conn) == 1)
        check("unsynced queue depth", storage.unsynced_count(conn) == 1)

        try:
            storage.insert_detection(conn, storage.Detection(
                id=storage.new_id(), captured_at="x", shazam_key=None,
                title=None, artist=None, isrc=None, level_dbfs=0.0,
                method="telepathy", client_version="t"))
            check("rejects unknown method", False)
        except ValueError:
            check("rejects unknown method", True)

        # Reopening must be a no-op, not a re-migration.
        conn.close()
        conn = storage.connect(db)
        check("reopen is idempotent", storage.detection_count(conn) == 1)
        conn.close()


def test_fingerprint():
    section("fingerprint cache (spec 5.3)")
    cfg = config.load(None).cache

    stored = fingerprint.fingerprint(excerpt(MUSIC_A, 0.0), SR, cfg)
    check("produces hashes", len(stored) > 100, f"{len(stored)} hashes")

    with tempfile.TemporaryDirectory() as tmp:
        conn = storage.connect(Path(tmp) / "fp.db")
        storage.upsert_cache_track(conn, "keyA", "Track A", "Artist A", None)
        written = storage.store_fingerprints(conn, "keyA", stored,
                                             cfg.max_hashes_per_track)
        check("hashes stored", written == min(len(stored), cfg.max_hashes_per_track),
              f"{written} of {len(stored)}")
        # One chunk must not exhaust a track's whole budget, or no later part
        # of the song could ever be cached.
        check("one chunk leaves room for more windows",
              len(stored) * 2 < cfg.max_hashes_per_track,
              f"{len(stored)} per chunk vs cap {cfg.max_hashes_per_track}")

        # An overlapping excerpt of the same audio must match on a consistent
        # offset -- that alignment is what distinguishes a real hit from noise.
        query = fingerprint.fingerprint(excerpt(MUSIC_A, 3.0), SR, cfg)
        hit = storage.match_fingerprints(conn, query, cfg.align_bin_s)
        check("same track matches", hit is not None and hit[0] == "keyA",
              f"score {hit[1] if hit else 0}")
        check("score clears threshold",
              hit is not None and hit[1] >= cfg.min_aligned_hashes,
              f"{hit[1] if hit else 0} >= {cfg.min_aligned_hashes}")

        # Different audio must not.
        other = fingerprint.fingerprint(excerpt(MUSIC_B, 0.0), SR, cfg)
        miss = storage.match_fingerprints(conn, other, cfg.align_bin_s)
        miss_score = miss[1] if miss else 0
        check("different track does not match",
              miss_score < cfg.min_aligned_hashes, f"score {miss_score}")

        check("hash cap respected",
              storage.store_fingerprints(conn, "keyA", stored, 10) == 0)
        conn.close()


def test_chroma():
    section("chroma change detection (spec 5.1)")
    a1 = fingerprint.chroma_vector(excerpt(MUSIC_A, 0.0), SR)
    a2 = fingerprint.chroma_vector(excerpt(MUSIC_A, 3.0), SR)
    b1 = fingerprint.chroma_vector(excerpt(MUSIC_B, 0.0), SR)

    same = fingerprint.similarity(a1, a2)
    diff = fingerprint.similarity(a1, b1)
    check("12 dimensions", a1.shape == (12,))
    check("same music scores high", same > 0.95, f"{same:.3f}")
    check("different music scores lower", diff < same, f"{diff:.3f} < {same:.3f}")
    check("no previous vector reads as change",
          fingerprint.similarity(a1, None) == 0.0)


def test_sessionize():
    section("sessionization (spec 8)")
    base = dt.datetime(2026, 7, 1, 20, 0, tzinfo=dt.timezone.utc)

    with tempfile.TemporaryDirectory() as tmp:
        conn = storage.connect(Path(tmp) / "sess.db")

        def add(minutes, key, title):
            storage.insert_detection(conn, storage.Detection(
                id=storage.new_id(),
                captured_at=storage.iso(base + dt.timedelta(minutes=minutes)),
                shazam_key=key, title=title, artist="A", isrc=None,
                level_dbfs=-20.0, method="shazam", client_version="t"))

        add(0, "k1", "One")
        add(1, "k1", "One")
        add(2, "k1", "One")
        add(5, "k2", "Two")
        add(40, "k1", "One")          # same track, well past the gap
        add(41, "k1", "One")
        storage.insert_detection(conn, storage.Detection(
            id=storage.new_id(), captured_at=storage.iso(base),
            shazam_key=None, title=None, artist=None, isrc=None,
            level_dbfs=-20.0, method="nomatch", client_version="t"))

        count = sessionize.rebuild(conn, 15.0)
        check("no-match rows excluded from plays", count == 3, f"{count} plays")

        k1 = conn.execute(
            "SELECT * FROM plays WHERE shazam_key='k1' ORDER BY started_at"
        ).fetchall()
        check("gap splits into two plays", len(k1) == 2)
        check("first play counts 3 detections", k1[0]["detection_count"] == 3)

        again = sessionize.rebuild(conn, 15.0)
        total = conn.execute("SELECT COUNT(*) FROM plays").fetchone()[0]
        check("rerun is idempotent", again == 3 and total == 3, f"{total} rows")

        merged = sessionize.rebuild(conn, 60.0)
        check("wider gap merges plays", merged == 2, f"{merged} plays")
        conn.close()


# ----------------------------------------------------------------------
# loop
# ----------------------------------------------------------------------

class _Done(Exception):
    """Not a CaptureError, so it escapes the loop's retry handling."""


class FakeSource:
    backend = "fake"
    name = "fake"

    def __init__(self, chunks):
        self.samplerate = SR
        self.channels = 1
        self.info = capture.SourceInfo(0, "input", "fake", "test", 1, SR, True)
        self._chunks = list(chunks)

    def record(self, seconds):
        if not self._chunks:
            raise _Done()
        return self._chunks.pop(0)


class StubRecognizer:
    name = "stub"

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    async def recognize(self, wav_path):
        assert os.path.exists(wav_path), "recognizer got a missing file"
        self.calls += 1
        action = self.script.pop(0) if self.script else None
        if isinstance(action, Exception):
            raise action
        return action


def fast_config(tmp):
    cfg = config.load(None)
    cfg.storage.db_path = str(Path(tmp) / "loop.db")
    cfg.root = Path(tmp)
    cfg.poll.base_interval_s = 0.01
    cfg.poll.backoff_steps_s = [0.01, 0.02, 0.03]
    cfg.poll.jitter_pct = 0.0
    cfg.audio.chunk_seconds = 12.0
    return cfg


async def run_loop(cfg, chunks, script):
    conn = storage.connect(cfg.db_path)
    source = FakeSource(chunks)
    rec = StubRecognizer(script)
    runner = Runner(cfg, conn, source, rec, "test")
    try:
        await runner.run()
    except _Done:
        pass
    return conn, rec


def test_loop_decision_tree():
    section("loop decision tree (spec 3)")
    match_a = TrackMatch(key="keyA", title="Track A", artist="Artist A")

    with tempfile.TemporaryDirectory() as tmp:
        cfg = fast_config(tmp)
        chunks = [
            excerpt(MUSIC_A, 0.0),    # 1: API -> match, caches hashes
            excerpt(MUSIC_A, 0.2),    # 2: chroma stable -> no call, no row
            excerpt(MUSIC_B, 0.0),    # 3: changed, cache miss -> API no-match
            excerpt(MUSIC_A, 6.0),    # 4: changed, cache HIT -> no API
            np.zeros(SR * 2, dtype=np.int16),   # 5: silent -> gated
        ]
        conn, rec = asyncio.run(run_loop(cfg, chunks, [match_a, None]))

        methods = [r[0] for r in conn.execute(
            "SELECT method FROM detections ORDER BY captured_at")]
        check("API called exactly twice", rec.calls == 2, f"{rec.calls} calls")
        check("stable chunk logged nothing",
              methods == ["shazam", "nomatch", "cache"], str(methods))

        row = conn.execute(
            "SELECT * FROM detections WHERE method='nomatch'").fetchone()
        check("no-match row has null key", row["shazam_key"] is None)

        cached = conn.execute(
            "SELECT * FROM detections WHERE method='cache'").fetchone()
        check("cache hit carries track metadata", cached["title"] == "Track A")

        track = storage.get_cache_track(conn, "keyA")
        check("cache hit counted", track["hit_count"] == 1)
        check("silent chunk logged nothing",
              storage.detection_count(conn) == 3)

        ids = [r[0] for r in conn.execute("SELECT id FROM detections")]
        check("ids are distinct uuids", len(set(ids)) == 3 and len(ids[0]) == 36)
        conn.close()


def test_repeat_nomatch_policy():
    section("unmatched-audio backoff (spec 5.2 vs 5.4)")
    # Same audio four times over: chroma sees no change, but nothing matches.
    chunks = [excerpt(MUSIC_A, 0.0), excerpt(MUSIC_A, 0.1),
              excerpt(MUSIC_A, 0.2), excerpt(MUSIC_A, 0.3)]

    with tempfile.TemporaryDirectory() as tmp:
        cfg = fast_config(tmp)
        cfg.cache.enabled = False
        conn, rec = asyncio.run(run_loop(cfg, list(chunks), [None] * 4))
        check("spec default re-asks every poll", rec.calls == 4,
              f"{rec.calls} calls")
        conn.close()

    with tempfile.TemporaryDirectory() as tmp:
        cfg = fast_config(tmp)
        cfg.cache.enabled = False
        cfg.change_detection.skip_repeat_nomatch = True
        conn, rec = asyncio.run(run_loop(cfg, list(chunks), [None] * 4))
        check("opt-in backs off on unchanged unmatched audio", rec.calls == 1,
              f"{rec.calls} calls")
        check("only the real attempt was logged",
              storage.detection_count(conn) == 1)
        conn.close()

    # A genuine change must still re-identify immediately under the opt-in.
    with tempfile.TemporaryDirectory() as tmp:
        cfg = fast_config(tmp)
        cfg.cache.enabled = False
        cfg.change_detection.skip_repeat_nomatch = True
        conn, rec = asyncio.run(run_loop(
            cfg, [excerpt(MUSIC_A, 0.0), excerpt(MUSIC_A, 0.1),
                  excerpt(MUSIC_B, 0.0)], [None] * 3))
        check("chroma change still triggers a call", rec.calls == 2,
              f"{rec.calls} calls")
        conn.close()


def test_crossfade_drift():
    section("crossfade drift (field regression)")
    # Reproduces an observed miss: the song changed mid-chunk, so that chunk
    # was a blend. Comparing each chunk to the *previous* one made the blend
    # the new reference, and the following chunk -- entirely the new track --
    # scored high against the blend instead of against the old track. Every
    # step stayed above threshold while the audio changed completely.
    a = excerpt(MUSIC_A, 0.0)
    b = excerpt(MUSIC_B, 0.0)
    blend = (0.5 * a.astype(np.float32) + 0.5 * b.astype(np.float32)).astype(np.int16)

    ca = fingerprint.chroma_vector(a, SR)
    cb = fingerprint.chroma_vector(b, SR)
    cblend = fingerprint.chroma_vector(blend, SR)
    step1 = fingerprint.similarity(cblend, ca)     # blend vs old track
    step2 = fingerprint.similarity(cb, cblend)     # new track vs blend
    direct = fingerprint.similarity(cb, ca)        # new track vs old track

    # Pick a threshold where each individual step reads as "no change" but the
    # end-to-end difference does not -- exactly the field conditions.
    threshold = min(step1, step2) - 0.01
    check("each crossfade step looks stable",
          step1 > threshold and step2 > threshold,
          f"{step1:.3f}, {step2:.3f} vs {threshold:.3f}")
    check("but old vs new is a real change", direct < threshold,
          f"{direct:.3f} < {threshold:.3f}")

    chunks = [a, blend, b, b]
    match_a = TrackMatch(key="keyA", title="A", artist="A")
    match_b = TrackMatch(key="keyB", title="B", artist="B")

    def run(compare_to):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = fast_config(tmp)
            cfg.cache.enabled = False
            cfg.change_detection.similarity_threshold = threshold
            cfg.change_detection.compare_to = compare_to
            cfg.change_detection.max_stable_polls = 0   # isolate the drift
            conn, rec = asyncio.run(run_loop(cfg, list(chunks),
                                             [match_a, match_b]))
            keys = [r[0] for r in conn.execute(
                "SELECT shazam_key FROM detections ORDER BY captured_at")]
            conn.close()
            return rec.calls, keys

    calls, keys = run("previous")
    check("spec 5.1 wording loses the incoming track",
          calls == 1 and keys == ["keyA"], f"{calls} calls, {keys}")

    calls, keys = run("identified")
    check("anchoring catches it", calls == 2 and keys == ["keyA", "keyB"],
          f"{calls} calls, {keys}")


def test_stable_ceiling():
    section("forced re-check ceiling")
    steady = [excerpt(MUSIC_A, 0.0)] * 7
    match_a = TrackMatch(key="keyA", title="A", artist="A")

    def run(ceiling):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = fast_config(tmp)
            cfg.cache.enabled = False
            cfg.change_detection.max_stable_polls = ceiling
            conn, rec = asyncio.run(run_loop(cfg, list(steady), [match_a] * 5))
            conn.close()
            return rec.calls

    check("disabled means coast forever", run(0) == 1, f"{run(0)} calls")
    check("ceiling forces periodic re-identification", run(2) == 3,
          f"{run(2)} calls")
    check("tighter ceiling checks more often", run(1) == 4, f"{run(1)} calls")


def test_rate_limit_and_ceiling():
    section("rate limiting and daily ceiling (spec 5.4)")
    with tempfile.TemporaryDirectory() as tmp:
        cfg = fast_config(tmp)
        cfg.cache.enabled = False          # force every eligible chunk to the API
        cfg.change_detection.enabled = False
        chunks = [excerpt(MUSIC_A, 0.0), excerpt(MUSIC_B, 0.0)]
        conn, rec = asyncio.run(run_loop(
            cfg, chunks, [RateLimited("429 Too Many Requests"), None]))

        outcomes = [r[0] for r in conn.execute("SELECT outcome FROM api_calls")]
        check("429 recorded", "ratelimit" in outcomes, str(outcomes))
        check("second chunk skipped the API", rec.calls == 1,
              f"{rec.calls} calls")
        check("no detection row for a throttled call",
              storage.detection_count(conn) == 0)
        conn.close()

    with tempfile.TemporaryDirectory() as tmp:
        cfg = fast_config(tmp)
        cfg.cache.enabled = False
        cfg.change_detection.enabled = False
        cfg.api.daily_ceiling = 1
        chunks = [excerpt(MUSIC_A, 0.0), excerpt(MUSIC_B, 0.0)]
        match_a = TrackMatch(key="keyA", title="A", artist="A")
        conn, rec = asyncio.run(run_loop(cfg, chunks, [match_a, match_a]))
        check("ceiling stops further API calls", rec.calls == 1,
              f"{rec.calls} calls")
        check("capture continued past the ceiling",
              storage.api_calls_today(conn) == 1)
        conn.close()


def run_until_exit(cfg, chunks, script):
    """Run the loop expecting it to exit itself; returns the exit message.

    Closes the connection whatever happens -- an open SQLite handle stops
    Windows removing the temp directory.
    """
    conn = storage.connect(cfg.db_path)
    try:
        runner = Runner(cfg, conn, FakeSource(chunks), StubRecognizer(script),
                        "test")
        asyncio.run(runner.run())
        return ""
    except SystemExit as exc:
        return str(exc)
    except _Done:
        return ""
    finally:
        conn.close()


def test_watchdog():
    section("silence watchdog (spec 9.2)")
    silence = np.zeros(SR * 2, dtype=np.int16)

    with tempfile.TemporaryDirectory() as tmp:
        cfg = fast_config(tmp)
        cfg.audio.watchdog_silence_minutes = 0.002       # ~0.12s
        cfg.audio.watchdog_floor_dbfs = -90.0
        exited = run_until_exit(cfg, [silence] * 200, [])
        check("digital silence eventually forces a restart",
              "dropped" in exited, exited[:60] or "no exit")

    # A quiet room is not a dead device: ambient noise sits well above the
    # watchdog floor and must never trigger it.
    with tempfile.TemporaryDirectory() as tmp:
        cfg = fast_config(tmp)
        cfg.audio.watchdog_silence_minutes = 0.002
        rng = np.random.default_rng(7)
        room = (rng.standard_normal(SR) * 30).astype(np.int16)   # ~ -60 dBFS
        check("quiet room sits above the watchdog floor",
              capture.dbfs(room) > cfg.audio.watchdog_floor_dbfs,
              f"{capture.dbfs(room):.0f} dBFS")
        exited = run_until_exit(cfg, [room] * 60, [None] * 60)
        check("quiet room does not trigger the watchdog", exited == "",
              exited[:60])

    # Disabling it must actually disable it.
    with tempfile.TemporaryDirectory() as tmp:
        cfg = fast_config(tmp)
        cfg.audio.watchdog_silence_minutes = 0
        exited = run_until_exit(cfg, [silence] * 200, [])
        check("watchdog can be turned off", exited == "", exited[:60])


def test_no_audio_retained():
    section("audio retention (spec 2, spec 11)")
    before = set(glob.glob(os.path.join(tempfile.gettempdir(), "setlist_*.wav")))
    with tempfile.TemporaryDirectory() as tmp:
        cfg = fast_config(tmp)
        cfg.cache.enabled = False
        cfg.change_detection.enabled = False
        conn, _ = asyncio.run(run_loop(
            cfg, [excerpt(MUSIC_A, 0.0), excerpt(MUSIC_B, 0.0)],
            [TrackMatch(key="k", title="t", artist="a"), None]))
        conn.close()
        leftovers = list(Path(tmp).rglob("*.wav"))
        check("no wav beside the database", not leftovers, str(leftovers))
    after = set(glob.glob(os.path.join(tempfile.gettempdir(), "setlist_*.wav")))
    check("no temp wav survives the run", not (after - before),
          str(after - before))


def main():
    capture.quiet_warnings()
    test_config()
    test_storage_migration()
    test_fingerprint()
    test_chroma()
    test_sessionize()
    test_loop_decision_tree()
    test_repeat_nomatch_policy()
    test_crossfade_drift()
    test_stable_ceiling()
    test_rate_limit_and_ceiling()
    test_watchdog()
    test_no_audio_retained()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
