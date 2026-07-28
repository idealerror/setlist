"""The capture loop.

Order of operations follows the spec 3 diagram:

    record -> level gate -> chroma change? -> local cache -> recognizer API

Each stage exists to avoid the next one. Capture itself is never gated on the
network, the recognizer, or event data (spec 2.1, spec 2.3): if everything
downstream is broken the loop still records levels and keeps running, because
uncaptured audio is gone forever while wrong metadata can be re-derived.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import random
import time

from . import capture, fingerprint, storage
from .recognize import RateLimited, RecognizerError, TrackMatch


class Runner:
    def __init__(self, cfg, conn, source, recognizer, client_version: str,
                 syncer=None):
        self.cfg = cfg
        self.conn = conn
        self.source = source
        self.recognizer = recognizer
        self.client_version = client_version
        #: Optional; capture never depends on it being present or working.
        self.syncer = syncer
        #: Monotonic time since the input went absolutely silent (spec 9.2).
        self._silence_since: float | None = None

        self._prev_chroma = None
        #: Chroma of the chunk that produced the current identification. Unlike
        #: _prev_chroma this does not move on stable polls, so a crossfade
        #: cannot walk the reference along with it.
        self._anchor_chroma = None
        self._stable_polls = 0
        self._current_key: str | None = None
        #: True when the last identification attempt came back as a no-match,
        #: used only by change_detection.skip_repeat_nomatch.
        self._last_was_nomatch = False
        self._backoff_index = 0
        self._consecutive_failures = 0
        #: monotonic deadline while the backend is throttling us (spec 5.4)
        self._rate_limited_until = 0.0
        self._rate_limit_step = 0
        self._ceiling_announced = False

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _sleep_for(self, base: float) -> float:
        """Apply +/- jitter so calls never land on exact multiples (spec 5.2)."""
        jitter = self.cfg.poll.jitter_pct
        return max(0.0, base * (1.0 + random.uniform(-jitter, jitter)))

    def _interval(self) -> float:
        steps = self.cfg.poll.backoff_steps_s
        return steps[min(self._backoff_index, len(steps) - 1)]

    def _api_available(self) -> tuple[bool, str]:
        if time.monotonic() < self._rate_limited_until:
            wait = int(self._rate_limited_until - time.monotonic())
            return False, f"rate-limited for another {wait}s"
        used = storage.api_calls_today(self.conn)
        if used >= self.cfg.api.daily_ceiling:
            return False, f"daily ceiling reached ({used}/{self.cfg.api.daily_ceiling})"
        return True, ""

    def _trip_rate_limit(self) -> None:
        steps = self.cfg.api.rate_limit_backoff_s
        delay = (steps[self._rate_limit_step] if self._rate_limit_step < len(steps)
                 else self.cfg.api.rate_limit_cap_s)
        delay = min(delay, self.cfg.api.rate_limit_cap_s)
        self._rate_limited_until = time.monotonic() + delay
        self._rate_limit_step += 1
        print(f"  ! rate limited; pausing API calls for {int(delay)}s "
              "(capture and cache continue)")

    def _check_watchdog(self, level: float) -> None:
        """Spec 9.2.

        A dead USB interface does not necessarily raise: the stream stays open
        and returns perfect digital silence forever, which the capture-failure
        counter never sees. A live input always has a noise floor, so a level
        this far down for this long means the device is gone, not the room.
        """
        minutes = self.cfg.audio.watchdog_silence_minutes
        if minutes <= 0:
            return

        if level > self.cfg.audio.watchdog_floor_dbfs:
            self._silence_since = None
            return

        now = time.monotonic()
        if self._silence_since is None:
            self._silence_since = now
            return
        if now - self._silence_since >= minutes * 60:
            raise SystemExit(
                f"No signal at all for {minutes:g} minutes "
                f"(below {self.cfg.audio.watchdog_floor_dbfs} dBFS). The input "
                "device has almost certainly dropped. Exiting so the "
                "supervisor restarts us.")

    def _log(self, captured_at, level, method, match: TrackMatch | None) -> None:
        storage.insert_detection(self.conn, storage.Detection(
            id=storage.new_id(),
            captured_at=storage.iso(captured_at),
            shazam_key=match.key if match else None,
            title=match.title if match else None,
            artist=match.artist if match else None,
            isrc=match.isrc if match else None,
            level_dbfs=level,
            method=method,
            client_version=self.client_version,
        ))

    # ------------------------------------------------------------------
    # identification
    # ------------------------------------------------------------------

    def _try_cache(self, samples, hashes) -> TrackMatch | None:
        """Local recognition cache (spec 5.3). No network involved."""
        if not self.cfg.cache.enabled or not hashes:
            return None
        hit = storage.match_fingerprints(
            self.conn, hashes, self.cfg.cache.align_bin_s)
        if hit is None:
            return None
        key, score = hit
        if score < self.cfg.cache.min_aligned_hashes:
            return None
        row = storage.get_cache_track(self.conn, key)
        if row is None:
            return None
        return TrackMatch(key=key, title=row["title"] or "?",
                          artist=row["artist"] or "?", isrc=row["isrc"])

    async def _identify(self, samples, level: float, captured_at) -> None:
        cfg = self.cfg
        hashes = []
        if cfg.cache.enabled:
            hashes = fingerprint.fingerprint(samples, self.source.samplerate,
                                             cfg.cache)

        cached = self._try_cache(samples, hashes)
        if cached is not None:
            storage.bump_hit_count(self.conn, cached.key)
            self._log(captured_at, level, "cache", cached)
            self._current_key = cached.key
            self._last_was_nomatch = False
            print(f"  cache: {cached.artist} - {cached.title}")
            return

        allowed, reason = self._api_available()
        if not allowed:
            if not self._ceiling_announced:
                print(f"  (skipping API: {reason}; cache-only recognition)")
                self._ceiling_announced = True
            if self.syncer is not None and "ceiling" in reason:
                # Surfaced on the next heartbeat so the server can alert
                # (spec 8); the server cannot infer client-side state.
                self.syncer.ceiling_tripped = True
            self._current_key = None
            return
        self._ceiling_announced = False
        if self.syncer is not None:
            self.syncer.ceiling_tripped = False

        wav_path = capture.write_temp_wav(samples, self.source.samplerate)
        match: TrackMatch | None = None
        try:
            match = await self.recognizer.recognize(wav_path)
        except RateLimited:
            storage.record_api_call(self.conn, "ratelimit")
            self._trip_rate_limit()
            return
        except RecognizerError as exc:
            storage.record_api_call(self.conn, "error")
            print(f"  ! recognition error: {exc}")
            return
        finally:
            # spec 11: audio never outlives the attempt.
            capture.remove_temp(wav_path)

        if match is None:
            storage.record_api_call(self.conn, "nomatch")
            self._log(captured_at, level, "nomatch", None)
            self._current_key = None
            self._last_was_nomatch = True
            print(f"  no match ({level:.0f} dBFS)")
            return

        storage.record_api_call(self.conn, "match")
        self._rate_limit_step = 0
        self._log(captured_at, level, "shazam", match)
        self._current_key = match.key
        self._last_was_nomatch = False

        # Teach the cache so the next play of this track costs nothing.
        storage.upsert_cache_track(self.conn, match.key, match.title,
                                   match.artist, match.isrc)
        stored = storage.store_fingerprints(
            self.conn, match.key, hashes, self.cfg.cache.max_hashes_per_track)
        extra = f", +{stored} hashes" if stored else ""
        print(f"  MATCH: {match.artist} - {match.title}{extra}")

    # ------------------------------------------------------------------
    # main loop
    # ------------------------------------------------------------------

    async def run(self, max_runtime: float | None = None) -> None:
        cfg = self.cfg
        started = time.monotonic()

        print(f"Listening. chunk {cfg.audio.chunk_seconds}s, "
              f"base interval {cfg.poll.base_interval_s}s, "
              f"gate {cfg.audio.gate_dbfs} dBFS, "
              f"chroma {'on' if cfg.change_detection.enabled else 'off'}, "
              f"cache {'on' if cfg.cache.enabled else 'off'}. Ctrl-C to stop.\n")

        while True:
            if max_runtime and time.monotonic() - started >= max_runtime:
                print("Max runtime reached; exiting for a clean restart.")
                return

            captured_at = storage.utc_now()
            local = captured_at.astimezone()

            try:
                samples = self.source.record(cfg.audio.chunk_seconds)
            except capture.CaptureError as exc:
                self._consecutive_failures += 1
                print(f"[{local:%H:%M:%S}] capture failed: {exc}")
                if self._consecutive_failures >= cfg.audio.max_consecutive_failures:
                    raise SystemExit(
                        f"{self._consecutive_failures} captures failed in a row. "
                        "The device has probably gone away (USB interfaces "
                        "drop, spec 9.2). Exiting so the supervisor restarts us.")
                await asyncio.sleep(min(cfg.poll.base_interval_s, 5))
                continue
            self._consecutive_failures = 0

            level = capture.dbfs(samples)
            self._check_watchdog(level)

            # The audio level is the event detector (spec 2.1): a quiet room
            # means nothing is happening, so spend nothing on it.
            if level < cfg.audio.gate_dbfs:
                self._prev_chroma = None
                self._anchor_chroma = None
                self._stable_polls = 0
                self._current_key = None
                self._last_was_nomatch = False
                self._backoff_index = 0
                print(f"[{local:%H:%M:%S}] quiet ({level:.0f} dBFS)")
                await asyncio.sleep(self._sleep_for(cfg.poll.base_interval_s))
                continue

            changed = True
            score = None
            chroma = None
            if cfg.change_detection.enabled:
                chroma = fingerprint.chroma_vector(samples, self.source.samplerate)
                if (cfg.change_detection.compare_to == "identified"
                        and self._anchor_chroma is not None):
                    reference = self._anchor_chroma
                else:
                    reference = self._prev_chroma
                score = fingerprint.similarity(chroma, reference)
                changed = (reference is None
                           or score < cfg.change_detection.similarity_threshold)
                self._prev_chroma = chroma

            known = self._current_key is not None
            settled_nomatch = (self._last_was_nomatch
                               and cfg.change_detection.skip_repeat_nomatch)
            ceiling = cfg.change_detection.max_stable_polls
            overdue = ceiling > 0 and self._stable_polls >= ceiling

            if not changed and not overdue and (known or settled_nomatch):
                # Same music still playing: log nothing, call nothing, and
                # lengthen the interval while it stays that way (spec 5.1/5.2).
                self._stable_polls += 1
                steps = cfg.poll.backoff_steps_s
                self._backoff_index = min(self._backoff_index + 1, len(steps) - 1)
                delay = self._interval()
                what = "stable" if known else "stable, still unidentified"
                print(f"[{local:%H:%M:%S}] {what} (chroma {score:.3f}), "
                      f"next in {int(delay)}s")
                await asyncio.sleep(self._sleep_for(delay))
                continue

            # A change, a forced re-check, or nothing currently identified.
            self._backoff_index = 0
            self._stable_polls = 0
            # Re-anchor on the chunk we are about to identify, so the next
            # comparison is against audio we actually recognised.
            if chroma is not None:
                self._anchor_chroma = chroma

            detail = f", chroma {score:.3f}" if score is not None else ""
            if overdue and not changed:
                detail += f", {ceiling} stable polls"
            print(f"[{local:%H:%M:%S}] checking ({level:.0f} dBFS{detail})")
            await self._identify(samples, level, captured_at)
            await asyncio.sleep(self._sleep_for(cfg.poll.base_interval_s))
