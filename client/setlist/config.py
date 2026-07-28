"""Configuration.

Every threshold the spec calls out -- chroma similarity, backoff steps, cache
hash minimum, play gap -- lives here and is overridable from a TOML file
(spec 11). No tuning constant may be a literal at its use site.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path


@dataclass
class VenueConfig:
    #: Identifies this venue to the server (spec 6). Unused until phase 3.
    slug: str = "venue"


@dataclass
class AudioConfig:
    #: "input" for a real capture device (the Scarlett line feed, spec 9.5),
    #: "loopback" to capture whatever the PC itself is playing.
    backend: str = "input"
    #: Device index or case-insensitive name substring. Empty = system default.
    device: str = ""
    #: 0 = pick automatically (1 for input, 2 for loopback).
    channels: int = 0
    #: 0 = use the device's native rate. WASAPI shared mode requires it
    #: (spec 9.3): 48000 on the Scarlett, not 44100.
    samplerate: int = 0
    chunk_seconds: float = 12.0
    #: Below this level the room is considered quiet and nothing is polled.
    #: The audio level is the event detector (spec 2.1).
    gate_dbfs: float = -50.0
    #: Consecutive capture failures before the process exits so the supervisor
    #: can restart it. USB interfaces drop (spec 9.2).
    max_consecutive_failures: int = 5

    #: Watchdog (spec 9.2). A device can stay open and keep handing back
    #: perfect digital silence after it dies, which no capture-error counter
    #: catches. A real input always has a noise floor, so sustained silence
    #: *this* absolute means the interface is gone rather than the room quiet.
    #: Exit after this many minutes so the supervisor restarts us. 0 disables.
    watchdog_silence_minutes: float = 30.0
    watchdog_floor_dbfs: float = -90.0


@dataclass
class PollConfig:
    base_interval_s: float = 30.0
    #: Adaptive backoff while chroma stays stable (spec 5.2).
    backoff_steps_s: list[float] = field(default_factory=lambda: [30.0, 60.0, 120.0])
    #: +/- fraction applied to every sleep so calls never land on exact
    #: multiples of a minute (spec 5.2).
    jitter_pct: float = 0.20


@dataclass
class ChangeDetectionConfig:
    """Spec 5.1. Chroma is for change detection only -- 12 dimensions cannot
    uniquely identify a song, so it must never be used as a cache key."""

    enabled: bool = True
    similarity_threshold: float = 0.95

    #: What each new chunk is compared against.
    #:
    #: "previous" is spec 5.1 literally: compare to the immediately preceding
    #: chunk. That reference drifts. During a crossfade the blended chunk
    #: becomes the new baseline, so the next chunk -- now entirely the new
    #: track -- is compared against the blend rather than the old track and
    #: scores high. No individual step crosses the threshold while the audio
    #: changes completely, and the incoming track is never logged. Observed in
    #: the field: a transition read 0.961 then 0.989 and was missed outright.
    #:
    #: "identified" (default) compares against the chunk that produced the
    #: current identification, so drift cannot accumulate: the comparison is
    #: always against audio we actually recognised. This deviates from spec 5.1
    #: deliberately -- a DJ set is a continuous crossfade, which is precisely
    #: the case the spec's wording loses. Set to "previous" to restore it.
    compare_to: str = "identified"

    #: Hard ceiling on how long the loop may coast without re-identifying,
    #: regardless of similarity. Backstop for the fact that a 12-dimension
    #: chroma vector is only marginally discriminative on real music: measured
    #: 0.900 between two different tracks versus 0.960 between two chunks of
    #: one track, which no threshold separates. Bounds how long a missed
    #: transition can hide. Repeat tracks come back as free cache hits, so the
    #: cost of a forced re-check falls as the cache fills. 0 disables.
    max_stable_polls: int = 4

    #: Spec 5.2 resets the interval on a no-match, so unmatched-but-loud audio
    #: is re-asked at base rate forever -- and spec 9.5 expects exactly that
    #: during DJ sets. At a 30s base that is 2880 calls/day against a ceiling
    #: of 500, tripped in about four hours, which is the metronomic pattern
    #: spec 5.4 warns about.
    #:
    #: False (default) is spec-faithful. True also backs off when chroma says
    #: the audio has not changed since a no-match, on the grounds that the
    #: recognizer has already answered for this audio. Re-identification still
    #: happens the moment chroma detects a change.
    skip_repeat_nomatch: bool = False


@dataclass
class CacheConfig:
    """Spec 5.3, constellation fingerprint cache."""

    enabled: bool = True
    #: Aligned hash count needed to declare a local hit.
    min_aligned_hashes: int = 8
    n_fft: int = 2048
    hop_length: int = 512
    #: Half-size of the local-maximum window used for peak picking, in bins.
    peak_neighborhood: int = 20
    #: Peaks quieter than this (relative to the chunk's max) are discarded.
    peak_floor_db: float = -60.0
    #: How many later peaks each anchor pairs with.
    fan_value: int = 15
    #: Target-zone width in frames.
    target_zone_min_dt: int = 1
    target_zone_max_dt: int = 100
    #: Offset-alignment bin width, seconds.
    align_bin_s: float = 0.10
    #: Bounds cache growth per track. One 12s chunk yields roughly 9000 hashes
    #: at these settings, so this budget is really "how many separate windows
    #: of a track may be cached". It must exceed one chunk's worth by a healthy
    #: margin: a track is only a cache hit if a stored window overlaps the
    #: window the next poll happens to land on, and jittered polling spreads
    #: those windows across the song over repeated plays.
    max_hashes_per_track: int = 40000


@dataclass
class ApiConfig:
    """Spec 5.4, rate-limit hygiene."""

    #: On trip, capture continues with cache-only recognition.
    daily_ceiling: int = 500
    rate_limit_backoff_s: list[float] = field(
        default_factory=lambda: [60.0, 300.0, 900.0]
    )
    rate_limit_cap_s: float = 3600.0


@dataclass
class StorageConfig:
    #: Resolved relative to the config file's directory when relative.
    db_path: str = "setlist.db"


@dataclass
class ServerConfig:
    """Store-and-forward sync (spec 2.3, spec 4).

    Empty `url` disables sync entirely and the client runs standalone. Capture
    must never depend on any of this being reachable.
    """

    url: str = ""
    token: str = ""
    #: Spec 6 caps a batch at 500.
    batch_size: int = 500
    sync_interval_s: float = 60.0
    heartbeat_interval_s: float = 300.0
    request_timeout_s: float = 30.0
    #: Escalating pause after a failed push, then held at the last value. Days
    #: of server downtime must cost nothing but a growing local queue.
    retry_backoff_s: list[float] = field(
        default_factory=lambda: [30.0, 120.0, 300.0, 900.0]
    )
    verify_tls: bool = True


@dataclass
class SessionizeConfig:
    """Spec 8. The same track reappearing after a longer gap is a new play."""

    play_gap_minutes: float = 15.0


@dataclass
class Config:
    venue: VenueConfig = field(default_factory=VenueConfig)
    audio: AudioConfig = field(default_factory=AudioConfig)
    poll: PollConfig = field(default_factory=PollConfig)
    change_detection: ChangeDetectionConfig = field(
        default_factory=ChangeDetectionConfig
    )
    cache: CacheConfig = field(default_factory=CacheConfig)
    api: ApiConfig = field(default_factory=ApiConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    sessionize: SessionizeConfig = field(default_factory=SessionizeConfig)
    #: Directory the config was loaded from; relative paths resolve against it.
    root: Path = field(default_factory=Path.cwd)

    @property
    def db_path(self) -> Path:
        p = Path(self.storage.db_path).expanduser()
        return p if p.is_absolute() else (self.root / p)


class ConfigError(RuntimeError):
    pass


def _apply(section, data: dict, where: str):
    """Overlay a TOML table onto a dataclass instance, type-checking as we go."""
    known = {f.name: f for f in fields(section)}
    for key, value in data.items():
        if key not in known:
            raise ConfigError(
                f"{where}.{key} is not a recognised setting. "
                f"Valid keys: {', '.join(sorted(known))}"
            )
        current = getattr(section, key)
        # bool is a subclass of int, so check it first to keep them distinct.
        if isinstance(current, bool) and not isinstance(value, bool):
            raise ConfigError(f"{where}.{key} must be true or false")
        if isinstance(current, float) and isinstance(value, int):
            value = float(value)
        if isinstance(current, list) and isinstance(value, list):
            value = [float(v) for v in value]
        elif not isinstance(value, type(current)):
            raise ConfigError(
                f"{where}.{key} must be {type(current).__name__}, "
                f"got {type(value).__name__}"
            )
        setattr(section, key, value)


def load(path: str | Path | None) -> Config:
    """Load config from TOML. With no path, every default applies."""
    cfg = Config()
    if path is None:
        return cfg

    p = Path(path).expanduser()
    if not p.is_file():
        raise ConfigError(f"Config file not found: {p}")
    try:
        # utf-8-sig, not utf-8: Notepad and PowerShell's Out-File both write a
        # BOM by default on Windows, and a leading ﻿ makes tomllib fail on
        # line 1 with an error that says nothing about encoding. Reading as
        # utf-8-sig strips a BOM if present and is identical when absent.
        data = tomllib.loads(p.read_text(encoding="utf-8-sig"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{p} is not valid TOML: {exc}") from exc

    cfg.root = p.parent.resolve()
    for name, value in data.items():
        target = getattr(cfg, name, None)
        if not is_dataclass(target):
            raise ConfigError(f"[{name}] is not a recognised config section")
        if not isinstance(value, dict):
            raise ConfigError(f"[{name}] must be a table")
        _apply(target, value, name)

    validate(cfg)
    return cfg


def validate(cfg: Config) -> None:
    if cfg.audio.backend not in ("input", "loopback"):
        raise ConfigError("audio.backend must be \"input\" or \"loopback\"")
    if cfg.audio.chunk_seconds < 3:
        raise ConfigError("audio.chunk_seconds below 3 will not fingerprint reliably")
    if not 0.0 <= cfg.change_detection.similarity_threshold <= 1.0:
        raise ConfigError("change_detection.similarity_threshold must be in 0..1")
    if cfg.change_detection.compare_to not in ("identified", "previous"):
        raise ConfigError(
            "change_detection.compare_to must be \"identified\" or \"previous\"")
    if cfg.change_detection.max_stable_polls < 0:
        raise ConfigError("change_detection.max_stable_polls must not be negative")
    if not cfg.poll.backoff_steps_s:
        raise ConfigError("poll.backoff_steps_s must list at least one interval")
    if any(s <= 0 for s in cfg.poll.backoff_steps_s):
        raise ConfigError("poll.backoff_steps_s entries must be positive")
    if not 0.0 <= cfg.poll.jitter_pct < 1.0:
        raise ConfigError("poll.jitter_pct must be in 0..1")
    if cfg.cache.min_aligned_hashes < 1:
        raise ConfigError("cache.min_aligned_hashes must be at least 1")
    if cfg.api.daily_ceiling < 0:
        raise ConfigError("api.daily_ceiling must not be negative")
    if cfg.sessionize.play_gap_minutes <= 0:
        raise ConfigError("sessionize.play_gap_minutes must be positive")
