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
