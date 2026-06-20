"""Tests for vibechek.native_decode (essentia-free Windows decode path).

The soundfile/soxr decode paths use real wheels (skipped if absent). The ffmpeg
fallback is exercised with monkeypatching so no real ffmpeg binary is required.
"""

from __future__ import annotations

import numpy as np
import pytest

from vibechek import native_decode
from vibechek.native_decode import DecodeError, decode_mono

sf = pytest.importorskip("soundfile")
pytest.importorskip("soxr")


def _write_wav(path, data, sr) -> str:
    sf.write(str(path), data, sr)
    return str(path)


def test_decode_mono_wav(tmp_path) -> None:
    sr = 16000
    data = (0.5 * np.sin(2 * np.pi * 440 * np.arange(sr) / sr)).astype(np.float32)
    out = decode_mono(_write_wav(tmp_path / "m.wav", data, sr), sr)
    assert out.dtype == np.float32
    assert out.ndim == 1
    assert abs(len(out) - sr) <= 1
    np.testing.assert_allclose(out, data, atol=1e-4)


def test_resamples_to_target_rate(tmp_path) -> None:
    src_sr, dst_sr = 44100, 16000
    data = (0.3 * np.sin(2 * np.pi * 220 * np.arange(src_sr) / src_sr)).astype(np.float32)
    out = decode_mono(_write_wav(tmp_path / "r.wav", data, src_sr), dst_sr)
    # ~1 s of audio resampled to 16 kHz -> ~16000 samples (soxr edge handling ±tiny).
    assert abs(len(out) - dst_sr) <= 64


def test_stereo_is_downmixed_to_mono_mean(tmp_path) -> None:
    sr = 16000
    left = np.full(sr, 0.4, np.float32)
    right = np.full(sr, -0.2, np.float32)
    stereo = np.stack([left, right], axis=1)
    out = decode_mono(_write_wav(tmp_path / "s.wav", stereo, sr), sr)
    assert out.ndim == 1
    np.testing.assert_allclose(out, np.full(sr, 0.1, np.float32), atol=1e-4)


def test_ffmpeg_fallback_used_when_soundfile_fails(monkeypatch, tmp_path) -> None:
    sentinel = np.arange(10, dtype=np.float32)

    def boom(*a, **k):
        raise RuntimeError("libsndfile: Unspecified internal error")

    monkeypatch.setattr(native_decode, "_decode_soundfile", boom)
    monkeypatch.setattr(native_decode, "ffmpeg_available", lambda: True)
    monkeypatch.setattr(native_decode, "_decode_ffmpeg", lambda p, sr: sentinel)

    out = decode_mono(str(tmp_path / "broken.mp3"), 16000)
    np.testing.assert_array_equal(out, sentinel)


def test_decode_error_when_soundfile_fails_and_no_ffmpeg(monkeypatch, tmp_path) -> None:
    def boom(*a, **k):
        raise RuntimeError("libsndfile: format not recognised")

    monkeypatch.setattr(native_decode, "_decode_soundfile", boom)
    monkeypatch.setattr(native_decode, "ffmpeg_available", lambda: False)

    with pytest.raises(DecodeError):
        decode_mono(str(tmp_path / "weird.m4a"), 16000)
