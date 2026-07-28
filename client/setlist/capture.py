"""Audio capture.

Two backends, because neither covers both cases on Windows:

* **sounddevice** for real capture devices -- the production path, a line feed
  from the desk into the Scarlett (spec 9.5). It exposes ``default_samplerate``,
  which is what spec 9.3 requires us to honour: WASAPI shared mode wants the
  device's native rate (48000 on the Scarlett), not a hardcoded 44100.
* **soundcard** for loopback. sounddevice cannot do it: its ``WasapiSettings``
  takes only ``exclusive``/``auto_convert``/``explicit_sample_format``, and the
  bundled PortAudio (V19.7.0-devel) exposes no loopback-tagged devices.

Loopback is a development convenience for testing against whatever the PC is
playing. The venue runs the ``input`` backend.
"""

from __future__ import annotations

import contextlib
import warnings
from dataclasses import dataclass

import numpy as np

#: Used only when a device reports no usable native rate.
FALLBACK_RATE = 48000


class CaptureError(RuntimeError):
    """A recoverable failure while recording one chunk."""


@dataclass(frozen=True)
class SourceInfo:
    index: int          # position in the unified list shown by --list-devices
    kind: str           # 'input' | 'loopback'
    name: str
    hostapi: str
    channels: int
    samplerate: int     # device native rate (spec 9.3)
    is_default: bool


def _same_device(a: str, b: str) -> bool:
    """Windows exposes one device once per host API, and MME truncates the
    name to 31 characters, so an exact comparison misses the WASAPI twin."""
    a, b = a.strip(), b.strip()
    if a == b:
        return True
    shorter, longer = sorted((a, b), key=len)
    return len(shorter) >= 20 and longer.startswith(shorter)


def quiet_warnings() -> None:
    warnings.filterwarnings("ignore", module="soundcard")
    with contextlib.suppress(Exception):
        from soundcard import SoundcardRuntimeWarning
        warnings.filterwarnings("ignore", category=SoundcardRuntimeWarning)


# ----------------------------------------------------------------------
# enumeration
# ----------------------------------------------------------------------

def _wasapi_outputs() -> dict[str, int]:
    """Native rate per WASAPI output device name, for the loopback backend."""
    import sounddevice as sd

    rates: dict[str, int] = {}
    try:
        apis = sd.query_hostapis()
        for dev in sd.query_devices():
            api = apis[dev["hostapi"]]["name"]
            if "wasapi" in api.lower() and dev["max_output_channels"] > 0:
                rates[dev["name"]] = int(dev["default_samplerate"] or FALLBACK_RATE)
    except Exception:
        pass
    return rates


def list_sources() -> list[SourceInfo]:
    """Every capture source, inputs first, in one index space."""
    import sounddevice as sd
    import soundcard as sc

    sources: list[SourceInfo] = []
    index = 0

    apis = sd.query_hostapis()
    default_input_name = ""
    with contextlib.suppress(Exception):
        default_input_name = sd.query_devices(sd.default.device[0])["name"]

    # Inputs, WASAPI first: MME truncates names to 31 characters and
    # DirectSound adds latency, so WASAPI is the one worth defaulting to.
    devices = list(enumerate(sd.query_devices()))
    wasapi_first = sorted(
        (d for d in devices if d[1]["max_input_channels"] > 0),
        key=lambda d: "wasapi" not in apis[d[1]["hostapi"]]["name"].lower(),
    )
    # The system default is reported against one host API, but we want the
    # marker on the entry we would actually open -- the WASAPI one, which
    # sorts first. Claim it for the first matching name only.
    default_claimed = False
    for _, dev in wasapi_first:
        is_default = False
        if (not default_claimed and default_input_name
                and _same_device(dev["name"], default_input_name)):
            is_default = default_claimed = True
        sources.append(SourceInfo(
            index=index,
            kind="input",
            name=dev["name"],
            hostapi=apis[dev["hostapi"]]["name"].replace("Windows ", ""),
            channels=int(dev["max_input_channels"]),
            samplerate=int(dev["default_samplerate"] or FALLBACK_RATE),
            is_default=is_default,
        ))
        index += 1

    default_speaker = ""
    with contextlib.suppress(Exception):
        default_speaker = sc.default_speaker().name

    rates = _wasapi_outputs()
    for mic in sc.all_microphones(include_loopback=True):
        if not getattr(mic, "isloopback", False):
            continue
        sources.append(SourceInfo(
            index=index,
            kind="loopback",
            name=mic.name,
            hostapi="WASAPI",
            channels=int(mic.channels or 2),
            samplerate=rates.get(mic.name, FALLBACK_RATE),
            is_default=bool(default_speaker
                            and _same_device(mic.name, default_speaker)),
        ))
        index += 1

    return sources


def format_sources(sources: list[SourceInfo]) -> str:
    lines = [f"{'idx':>4}  {'kind':<8} {'hostapi':<12} {'ch':>3}  {'rate':>6}  name",
             "-" * 88]
    for s in sources:
        tag = "  [default]" if s.is_default else ""
        lines.append(f"{s.index:>4}  {s.kind:<8} {s.hostapi:<12} {s.channels:>3}  "
                     f"{s.samplerate:>6}  {s.name}{tag}")
    lines.append("")
    lines.append("input    = a real capture device. The venue uses this "
                 "(line feed -> Scarlett).")
    lines.append("loopback = whatever the PC is playing. Development only.")
    lines.append("")
    lines.append("Windows lists each device once per host API. WASAPI entries "
                 "come first and are")
    lines.append("the ones to pick: MME truncates names to 31 characters and "
                 "DirectSound adds latency.")
    lines.append("Set audio.device to an index or a name substring "
                 "(a substring survives renumbering).")
    return "\n".join(lines)


# ----------------------------------------------------------------------
# sources
# ----------------------------------------------------------------------

class _SoundDeviceSource:
    """Real capture device via sounddevice/PortAudio."""

    backend = "sounddevice"

    def __init__(self, info: SourceInfo, sd_index: int, samplerate: int,
                 channels: int):
        self.info = info
        self.name = info.name
        self._index = sd_index
        self.samplerate = samplerate
        self.channels = channels

    def record(self, seconds: float) -> np.ndarray:
        import sounddevice as sd

        try:
            frames = sd.rec(
                int(seconds * self.samplerate),
                samplerate=self.samplerate,
                channels=self.channels,
                dtype="int16",
                device=self._index,
            )
            sd.wait()
        except KeyboardInterrupt:
            with contextlib.suppress(Exception):
                sd.stop()
            raise
        except Exception as exc:
            with contextlib.suppress(Exception):
                sd.stop()
            raise CaptureError(f"{type(exc).__name__}: {exc}") from exc

        if frames is None or frames.size == 0:
            raise CaptureError("device returned no frames")
        if frames.ndim > 1 and frames.shape[1] > 1:
            frames = frames.mean(axis=1).astype(np.int16)
        return frames.reshape(-1)


class _SoundCardSource:
    """WASAPI loopback via soundcard."""

    backend = "soundcard"

    def __init__(self, info: SourceInfo, mic, samplerate: int, channels: int):
        self.info = info
        self.name = info.name
        self._mic = mic
        self.samplerate = samplerate
        self.channels = channels

    def record(self, seconds: float) -> np.ndarray:
        # The recorder is opened per chunk rather than held open: with a 30s+
        # poll interval a persistent recorder hands back buffered audio from
        # minutes ago instead of what is playing now.
        try:
            with self._mic.recorder(samplerate=self.samplerate,
                                    channels=self.channels) as rec:
                data = rec.record(numframes=int(seconds * self.samplerate))
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            raise CaptureError(f"{type(exc).__name__}: {exc}") from exc

        if data is None or len(data) == 0:
            raise CaptureError("device returned no frames")

        data = np.asarray(data, dtype=np.float32)
        if data.ndim > 1 and data.shape[1] > 1:
            data = data.mean(axis=1)
        data = data.reshape(-1)
        # Clip before scaling so inter-sample overshoot saturates instead of
        # wrapping to the opposite sign.
        np.clip(data, -1.0, 1.0, out=data)
        return (data * 32767.0).astype(np.int16)


def _select(audio_cfg, sources: list[SourceInfo]) -> SourceInfo:
    candidates = [s for s in sources if s.kind == audio_cfg.backend]
    if not candidates:
        raise SystemExit(
            f"No {audio_cfg.backend} devices found. Run `list-devices`.")

    wanted = (audio_cfg.device or "").strip()
    if not wanted:
        for source in candidates:
            if source.is_default:
                return source
        return candidates[0]

    if wanted.lstrip("-").isdigit():
        index = int(wanted)
        match = next((s for s in sources if s.index == index), None)
        if match is None:
            raise SystemExit(f"Device index {index} is out of range "
                             f"(0-{len(sources) - 1}). Run `list-devices`.")
        if match.kind != audio_cfg.backend:
            raise SystemExit(
                f"Device {index} ('{match.name}') is a {match.kind} source but "
                f"audio.backend is \"{audio_cfg.backend}\". Set "
                f"audio.backend = \"{match.kind}\", or choose another index.")
        return match

    needle = wanted.lower()
    matches = [s for s in candidates if needle in s.name.lower()]
    if not matches:
        raise SystemExit(f"No {audio_cfg.backend} device matches {wanted!r}. "
                         "Run `list-devices`.")
    if len(matches) > 1:
        print(f"  ! {wanted!r} matches {len(matches)} devices; using the first:")
        for s in matches:
            print(f"      {s.name}")
    return matches[0]


def open_source(audio_cfg):
    """Resolve config into a ready-to-record source."""
    quiet_warnings()
    sources = list_sources()
    info = _select(audio_cfg, sources)

    # spec 9.3: default to the device's native rate, never a hardcoded one.
    samplerate = audio_cfg.samplerate or info.samplerate
    channels = audio_cfg.channels or (2 if info.kind == "loopback" else 1)
    channels = max(1, min(channels, info.channels or channels))

    if info.kind == "input":
        import sounddevice as sd

        sd_index = next(
            (i for i, d in enumerate(sd.query_devices())
             if d["name"] == info.name and d["max_input_channels"] >= channels),
            None,
        )
        if sd_index is None:
            raise SystemExit(f"Input device '{info.name}' vanished between "
                             "enumeration and open. Re-run `list-devices`.")
        return _SoundDeviceSource(info, sd_index, samplerate, channels)

    import soundcard as sc

    try:
        mic = sc.get_microphone(str(info.name), include_loopback=True)
    except Exception as exc:
        raise SystemExit(f"Cannot open loopback device '{info.name}': {exc}")
    return _SoundCardSource(info, mic, samplerate, channels)


def dbfs(samples: np.ndarray) -> float:
    if samples.size == 0:
        return float("-inf")
    rms = np.sqrt(np.mean((samples.astype(np.float32) / 32768.0) ** 2))
    return float(20 * np.log10(rms)) if rms > 0 else float("-inf")


# ----------------------------------------------------------------------
# temp WAV
# ----------------------------------------------------------------------
# The only place audio ever reaches a filesystem. Callers must delete in a
# finally block; there is deliberately no option to keep or relocate the file,
# because the system must never be capable of producing a recording of the
# room (spec 2, spec 11).

def write_temp_wav(samples: np.ndarray, samplerate: int) -> str:
    import os
    import tempfile
    import wave

    fd, path = tempfile.mkstemp(suffix=".wav", prefix="setlist_")
    os.close(fd)
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(samplerate)
        wf.writeframes(samples.tobytes())
    return path


def remove_temp(path: str, attempts: int = 6) -> None:
    """Windows refuses to unlink a file while any handle is open; antivirus
    and the search indexer both grab freshly written files for a moment."""
    import os
    import time

    for attempt in range(attempts):
        try:
            os.remove(path)
            return
        except FileNotFoundError:
            return
        except OSError:
            if attempt == attempts - 1:
                print(f"  ! could not delete temp file {path}")
                return
            time.sleep(0.2)
