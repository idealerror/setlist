"""Two independent signal-processing jobs. Do not conflate them.

**Change detection (spec 5.1)** -- a 12-dimensional chroma vector, compared
against the previous chunk. Cheap, local, and answers only "is this still the
same music?". Twelve dimensions cannot uniquely identify a song, so this must
never be used as a cache key.

**Recognition cache (spec 5.3)** -- a constellation fingerprint in the
audfprint/Dejavu style: spectrogram, local peak picking, hash pairs of
(freq1, freq2, dt). This *can* identify a track, because a match requires many
hashes agreeing on a single time offset.
"""

from __future__ import annotations

import numpy as np

#: Bit layout for a peak-pair hash. n_fft=2048 gives 1025 bins, so 11 bits
#: covers the frequency indices; 14 bits covers the target-zone width.
_F_BITS = 11
_DT_BITS = 14
_F_MASK = (1 << _F_BITS) - 1
_DT_MASK = (1 << _DT_BITS) - 1

_librosa = None


def _lib():
    """Import librosa lazily -- it costs seconds, and `list-devices` and
    `export` have no use for it."""
    global _librosa
    if _librosa is None:
        import librosa
        _librosa = librosa
    return _librosa


def as_float(samples: np.ndarray) -> np.ndarray:
    """int16 PCM -> float32 in [-1, 1]."""
    if samples.dtype == np.float32:
        return samples
    return (samples.astype(np.float32) / 32768.0).reshape(-1)


# ----------------------------------------------------------------------
# change detection (spec 5.1)
# ----------------------------------------------------------------------

def chroma_vector(samples: np.ndarray, samplerate: int) -> np.ndarray:
    """Unit-norm 12-dim chroma summary of a chunk."""
    mono = as_float(samples)
    chroma = _lib().feature.chroma_cqt(y=mono, sr=samplerate)
    vector = chroma.mean(axis=1).astype(np.float64)
    norm = np.linalg.norm(vector)
    return vector / norm if norm > 0 else vector


def similarity(a: np.ndarray | None, b: np.ndarray | None) -> float:
    """Cosine similarity of two unit-norm chroma vectors, 0..1."""
    if a is None or b is None:
        return 0.0
    return float(np.clip(np.dot(a, b), 0.0, 1.0))


# ----------------------------------------------------------------------
# constellation fingerprint (spec 5.3)
# ----------------------------------------------------------------------

def _spectrogram(mono: np.ndarray, cfg) -> np.ndarray:
    librosa = _lib()
    stft = np.abs(librosa.stft(mono, n_fft=cfg.n_fft, hop_length=cfg.hop_length))
    return librosa.amplitude_to_db(stft, ref=np.max)


def _peaks(spec_db: np.ndarray, cfg) -> tuple[np.ndarray, np.ndarray]:
    """Local maxima of the spectrogram, returned sorted by time."""
    from scipy.ndimage import maximum_filter

    size = 2 * cfg.peak_neighborhood + 1
    local_max = maximum_filter(spec_db, size=size, mode="constant", cval=-np.inf)
    mask = (spec_db == local_max) & (spec_db > cfg.peak_floor_db)
    freq_idx, time_idx = np.nonzero(mask)
    order = np.argsort(time_idx, kind="stable")
    return freq_idx[order], time_idx[order]


def _pack(f1: int, f2: int, dt: int) -> int:
    return (((int(f1) & _F_MASK) << (_F_BITS + _DT_BITS))
            | ((int(f2) & _F_MASK) << _DT_BITS)
            | (int(dt) & _DT_MASK))


def fingerprint(samples: np.ndarray, samplerate: int, cfg) -> list[tuple[int, float]]:
    """
    Return [(hash, offset_seconds), ...] for a chunk.

    Each anchor peak is paired with up to `fan_value` later peaks inside the
    target zone. The offset is the anchor's time, which is what lets matching
    recover a constant delta between query and stored copy.
    """
    mono = as_float(samples)
    if mono.size < cfg.n_fft:
        return []

    freq_idx, time_idx = _peaks(_spectrogram(mono, cfg), cfg)
    count = time_idx.size
    if count < 2:
        return []

    seconds_per_frame = cfg.hop_length / float(samplerate)
    out: list[tuple[int, float]] = []
    for i in range(count):
        f1 = freq_idx[i]
        t1 = time_idx[i]
        paired = 0
        for j in range(i + 1, count):
            delta = int(time_idx[j] - t1)
            if delta < cfg.target_zone_min_dt:
                continue
            if delta > cfg.target_zone_max_dt:
                break  # time_idx is sorted, so nothing later can qualify
            out.append((_pack(f1, freq_idx[j], delta), float(t1) * seconds_per_frame))
            paired += 1
            if paired >= cfg.fan_value:
                break
    return out
