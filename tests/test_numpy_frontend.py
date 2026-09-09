"""Parity test for the pure-NumPy MusiCNN mel frontend.

`tests/data/musicnn_mel_fixture.npz` holds a deterministic synthetic signal plus
the mel that essentia's `TensorflowInputMusiCNN` produced for it (captured once,
in WSL, by scripts/native_frontend_parity.py's sibling generator). This test
recomputes the mel with `vibechek.numpy_frontend` — numpy only, NO essentia — and
asserts it reproduces essentia's output, so CI guards the reproduction on every
platform without needing essentia installed.

The live spike measured per-frame L1 = 0.0000 / max abs ≤ 0.001 on 5 real tracks;
the tolerances here are deliberately a few× looser to absorb cross-platform FFT
rounding while still catching any real regression (a wrong filterbank, window, or
compression would blow well past them).
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from vibechek.numpy_frontend import (
    N_MELS,
    _slaney_mel_filterbank,
    musicnn_mel,
)

FIXTURE = Path(__file__).parent / "data" / "musicnn_mel_fixture.npz"


@pytest.fixture(scope="module")
def fixture() -> dict:
    if not FIXTURE.exists():
        pytest.skip(f"parity fixture missing: {FIXTURE}")
    d = np.load(FIXTURE)
    return {"audio": d["audio"], "mel": d["mel"]}


def test_matches_essentia_mel(fixture: dict) -> None:
    mel_np = musicnn_mel(fixture["audio"])
    mel_es = fixture["mel"]
    assert mel_np.shape == mel_es.shape, (mel_np.shape, mel_es.shape)

    abs_err = np.abs(mel_np - mel_es)
    mean_l1 = float(abs_err.mean())
    max_abs = float(abs_err.max())
    # Bit-close, not approximate: essentia's mel range is ~0..6. A broken
    # filterbank/window/compression misses by >>0.05; matched, it's ~1e-3.
    assert mean_l1 < 0.005, f"mean per-frame L1 {mean_l1:.5f} too high"
    assert max_abs < 0.05, f"max abs error {max_abs:.5f} too high"


def test_output_contract(fixture: dict) -> None:
    mel = musicnn_mel(fixture["audio"])
    assert mel.dtype == np.float32
    assert mel.ndim == 2 and mel.shape[1] == N_MELS
    assert np.all(np.isfinite(mel))
    # log10(1 + 10000·power) with power >= 0 -> values are non-negative.
    assert mel.min() >= 0.0


def test_short_and_empty_audio_yield_no_frames() -> None:
    # Shorter than one 512-sample frame -> zero frames (the patcher zero-pads).
    assert musicnn_mel(np.zeros(100, dtype=np.float32)).shape == (0, N_MELS)
    assert musicnn_mel(np.zeros(0, dtype=np.float32)).shape == (0, N_MELS)


def test_filterbank_shape_and_normalization() -> None:
    fb = _slaney_mel_filterbank()
    assert fb.shape == (N_MELS, 512 // 2 + 1)
    # Every triangular filter has positive area (unit_tri normalization is finite).
    assert np.all(fb.sum(axis=1) > 0)
    assert np.all(fb >= 0)


# ---------------------------------------------------------------------------
# Peak memory must be a constant, not a multiple of the track length
# ---------------------------------------------------------------------------


def _whole_track_mel(audio: np.ndarray) -> np.ndarray:
    """The pre-fix implementation: every frame, its windowed copy and the full
    complex spectrum materialized at once, in float64."""
    from vibechek.numpy_frontend import FRAME_SIZE, HOP_SIZE, MEL_SCALE_K, _hann

    audio = np.asarray(audio, dtype=np.float64)
    n = audio.shape[0]
    if n < FRAME_SIZE:
        return np.zeros((0, N_MELS), dtype=np.float32)
    starts = range(0, n - FRAME_SIZE + 1, HOP_SIZE)
    frames = np.stack([audio[s:s + FRAME_SIZE] for s in starts])
    spec = np.fft.rfft(frames * _hann(FRAME_SIZE), n=FRAME_SIZE, axis=1)
    power = np.abs(spec) ** 2
    mel_power = power @ _slaney_mel_filterbank().T
    return np.log10(1.0 + 10000.0 * MEL_SCALE_K * mel_power).astype(np.float32)


@pytest.mark.parametrize("dtype", [np.float32, np.float64])
@pytest.mark.parametrize("n", [512, 513, 1000, 20_011])
def test_blocking_is_bit_identical_to_the_whole_track_maths(n: int, dtype) -> None:
    """Every frame's spectrum and mel projection depend on that frame alone, so
    processing them in blocks must not move a single bit."""
    rng = np.random.default_rng(1234)
    audio = rng.standard_normal(n).astype(dtype)
    assert np.array_equal(musicnn_mel(audio), _whole_track_mel(audio))


def test_block_size_does_not_change_the_result(monkeypatch) -> None:
    from vibechek import numpy_frontend

    rng = np.random.default_rng(7)
    audio = rng.standard_normal(40_000).astype(np.float32)
    reference = musicnn_mel(audio)
    for block in (1, 7, 64):
        monkeypatch.setattr(numpy_frontend, "_BLOCK_FRAMES", block)
        assert np.array_equal(numpy_frontend.musicnn_mel(audio), reference)


def _peak_bytes(audio: np.ndarray) -> tuple[int, int]:
    """(peak bytes allocated during the call, bytes of mel returned).

    numpy's allocations are traced by tracemalloc, so this measures the real
    scratch the frontend holds — the thing the flat 800 MB per-worker budget in
    `resources.per_worker_mb` assumes is independent of track length.
    """
    import tracemalloc

    tracemalloc.start()
    try:
        tracemalloc.reset_peak()
        base = tracemalloc.get_traced_memory()[0]
        mel = musicnn_mel(audio)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    return peak - base, mel.nbytes


def test_peak_memory_tracks_the_output_not_the_track_length() -> None:
    """Doubling the track must cost roughly the extra mel rows and nothing else.
    The whole-track version held ~56 bytes of float64/complex128 scratch per
    input SAMPLE — ~38x the mel it returns — which is how a 20-minute recorded
    set peaked near 1.4 GB inside an 800 MB worker budget.
    """
    rng = np.random.default_rng(99)
    short = rng.standard_normal(16000 * 30).astype(np.float32)   # 30 s
    long = rng.standard_normal(16000 * 120).astype(np.float32)   # 2 min

    peak_short, mel_short = _peak_bytes(short)
    peak_long, mel_long = _peak_bytes(long)

    growth = (peak_long - peak_short) / (mel_long - mel_short)
    assert growth < 3.0, f"peak memory grew {growth:.1f}x the mel it produced"
