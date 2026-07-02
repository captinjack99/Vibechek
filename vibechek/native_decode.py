"""Essentia-free audio decode — a VALIDATED SPIKE, currently unwired.

STATUS (honest, 2026-07): nothing in the production analyze path imports this
module. The shipped native Windows engine decodes through the bundled DSP-only
essentia wheel's `MonoLoader` (FFmpeg/libav folded into the installer), which
made this replacement unnecessary for v0.6.x. It is kept because the
validation was real and it remains the candidate FALLBACK for files the
bundled decoder rejects:

  * `soundfile` (libsndfile) decodes WAV/FLAC/OGG and MP3 (libsndfile >= 1.1) with
    a native `win_amd64` wheel;
  * `soxr` does high-quality band-limited resampling (what librosa uses);
  * `ffmpeg` (if present) covers files libsndfile can't open — some MP3s with
    damaged frames, and AAC/M4A/WMA that libsndfile lacks.

The live spike measured this path at embedding cosine >= 0.998 + genre top-1
match vs essentia's MonoLoader through the ONNX backbone; the one MP3 libsndfile
rejected ("Illegal Audio-MPEG-Header") is exactly the ffmpeg-fallback case.

Imports of `soundfile`/`soxr` are lazy so the module stays import-clean where
they aren't installed; the `[native]` extra installs them. If you are
debugging a Windows decode failure: this fallback does NOT run today — wire it
into the native engine's load path first (or check the bundled essentia).
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)


class DecodeError(RuntimeError):
    """Raised when neither soundfile nor the ffmpeg fallback can decode a file."""


def decode_mono(path: str | Path, sample_rate: int, *, allow_ffmpeg: bool = True) -> np.ndarray:
    """Decode `path` to a mono float32 array at `sample_rate`.

    Tries soundfile+soxr first; on any failure (unsupported codec, damaged
    stream) falls back to ffmpeg when it is on PATH and `allow_ffmpeg`. Raises
    `DecodeError` if every path fails. Mirrors essentia MonoLoader's contract:
    downmix to mono, resample to the requested rate.
    """
    path = str(path)
    try:
        return _decode_soundfile(path, sample_rate)
    except Exception as e:  # noqa: BLE001 — any libsndfile/codec failure is a fallback trigger
        if allow_ffmpeg and ffmpeg_available():
            log.info("soundfile decode failed for %s (%s); falling back to ffmpeg", path, e)
            try:
                return _decode_ffmpeg(path, sample_rate)
            except Exception as e2:  # noqa: BLE001
                raise DecodeError(f"soundfile AND ffmpeg failed for {path}: {e2}") from e2
        raise DecodeError(
            f"could not decode {path}: {e}"
            + ("" if ffmpeg_available() else " (ffmpeg not on PATH for fallback)")
        ) from e


def _decode_soundfile(path: str, sample_rate: int) -> np.ndarray:
    import soundfile as sf  # noqa: PLC0415

    wav, srate = sf.read(path, dtype="float32", always_2d=True)
    mono = wav.mean(axis=1).astype(np.float32) if wav.shape[1] > 1 else wav[:, 0]
    if srate != sample_rate:
        import soxr  # noqa: PLC0415

        mono = soxr.resample(mono, srate, sample_rate, quality="VHQ").astype(np.float32)
    return np.ascontiguousarray(mono, dtype=np.float32)


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def _decode_ffmpeg(path: str, sample_rate: int) -> np.ndarray:
    """Decode via ffmpeg to raw mono float32 little-endian at `sample_rate`."""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise DecodeError("ffmpeg not found on PATH")
    cmd = [ffmpeg, "-v", "error", "-i", path, "-ac", "1", "-ar", str(sample_rate), "-f", "f32le", "-"]
    proc = subprocess.run(cmd, capture_output=True)  # noqa: S603
    if proc.returncode != 0:
        raise DecodeError(f"ffmpeg exit {proc.returncode}: {proc.stderr.decode(errors='replace')[:200]}")
    return np.frombuffer(proc.stdout, dtype="<f4").copy()
