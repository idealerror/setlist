"""Recognition, behind an interface (spec 2.4).

shazamio drives a private endpoint that Shazam does not document and does not
support. Treat it as something that will break. Everything above this module
talks to the ``Recognizer`` protocol, so adding an AudD or ACRCloud adapter
means writing a new class here and changing nothing else.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class TrackMatch:
    key: str
    title: str
    artist: str
    isrc: str | None = None
    url: str | None = None


class RecognizerError(RuntimeError):
    """Recognition failed in a way that consumed an API call."""


class RateLimited(RecognizerError):
    """The backend asked us to slow down (spec 5.4)."""

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


@runtime_checkable
class Recognizer(Protocol):
    #: Recorded on detections so a later backend swap stays auditable.
    name: str

    async def recognize(self, wav_path: str) -> TrackMatch | None:
        """Identify a WAV file.

        Returns None for a confident no-match. Raises RateLimited when
        throttled, RecognizerError for any other failure. Both outcomes mean an
        API call was spent.
        """
        ...


def _status_of(exc: BaseException) -> int | None:
    for attr in ("status", "status_code", "code"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    return None


def _looks_rate_limited(exc: BaseException) -> bool:
    if _status_of(exc) == 429:
        return True
    text = str(exc).lower()
    return "429" in text or "too many requests" in text or "rate limit" in text


def _first_str(mapping: dict, *keys) -> str | None:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _extract_isrc(track: dict) -> str | None:
    """ISRC moves around between Shazam response shapes, so try the known spots."""
    direct = _first_str(track, "isrc")
    if direct:
        return direct
    for section in track.get("sections") or []:
        if isinstance(section, dict):
            found = _first_str(section, "isrc")
            if found:
                return found
    hub = track.get("hub")
    if isinstance(hub, dict):
        found = _first_str(hub, "isrc")
        if found:
            return found
    return None


class ShazamRecognizer:
    """Adapter over shazamio."""

    name = "shazam"

    def __init__(self):
        # shazamio imports pydub, which warns that ffmpeg is missing. We only
        # ever hand it a WAV, decoded by shazamio-core, so ffmpeg is not used
        # (spec 9.4). Filter before the import so the warning never fires.
        warnings.filterwarnings("ignore", message=".*ffmpeg or avconv.*")
        try:
            from shazamio import Shazam
        except ImportError as exc:  # pragma: no cover - environment problem
            raise RecognizerError(
                f"cannot import shazamio ({exc}); this client requires "
                "Python 3.10-3.12, see spec 9.1"
            ) from exc
        self._shazam = Shazam()

    async def recognize(self, wav_path: str) -> TrackMatch | None:
        call = getattr(self._shazam, "recognize", None)
        if call is None:  # older shazamio
            call = self._shazam.recognize_song
        try:
            result = await call(wav_path)
        except Exception as exc:
            if _looks_rate_limited(exc):
                raise RateLimited(str(exc)) from exc
            raise RecognizerError(f"{type(exc).__name__}: {exc}") from exc

        track = (result or {}).get("track")
        if not track:
            return None

        key = _first_str(track, "key")
        if not key:
            # Without a stable key we cannot dedupe or cache it; treat as miss.
            return None
        return TrackMatch(
            key=key,
            title=_first_str(track, "title") or "?",
            artist=_first_str(track, "subtitle") or "?",
            isrc=_extract_isrc(track),
            url=_first_str(track, "url"),
        )
