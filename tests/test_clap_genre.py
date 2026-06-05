"""Tests for the pure-audio CLAP genre classifier (vibechek/clap_genre.py).

The kNN half is pure numpy and fully testable on `[dev]`; the CLAP embedding half
(laion_clap + torch) is lazy and NOT exercised here — we only assert it doesn't
get imported at module load, so the package stays import-clean for CI.
"""

from __future__ import annotations

import sys

import numpy as np
import pytest

from vibechek import clap_genre


def test_module_is_import_clean() -> None:
    """Importing clap_genre must NOT pull in the heavy opt-in deps."""
    for heavy in ("laion_clap", "torch", "torchvision"):
        assert heavy not in sys.modules, f"{heavy} leaked into import"


def test_knn_predict_picks_nearest_label() -> None:
    ref = clap_genre.ClapReference(
        emb=np.eye(4, dtype=np.float32),
        labels=np.array(["A", "B", "C", "D"], dtype=object),
        meta={},
    )
    g, conf = clap_genre.knn_predict(np.array([0.95, 0.05, 0.0, 0.0], dtype=np.float32), ref, k=2)
    assert g == "A"
    assert 0.0 < conf <= 1.0


def test_knn_predict_weighted_vote_majority() -> None:
    # 3 of 4 neighbours are "House": a query near all of them should vote House.
    emb = np.array([
        [1.0, 0.0, 0.0],
        [0.98, 0.1, 0.0],
        [0.96, 0.0, 0.1],
        [0.0, 1.0, 0.0],   # the odd one out
    ], dtype=np.float32)
    ref = clap_genre.ClapReference(
        emb=emb / np.linalg.norm(emb, axis=1, keepdims=True),
        labels=np.array(["House", "House", "House", "Trance"], dtype=object),
        meta={},
    )
    g, _ = clap_genre.knn_predict(np.array([1.0, 0.02, 0.0], dtype=np.float32), ref, k=4)
    assert g == "House"


def test_knn_predict_empty_or_zero_is_unknown() -> None:
    ref = clap_genre.ClapReference(emb=np.zeros((0, 4), dtype=np.float32),
                                   labels=np.array([], dtype=object), meta={})
    assert clap_genre.knn_predict(np.ones(4, dtype=np.float32), ref)[0] == "Unknown"
    ref2 = clap_genre.ClapReference(emb=np.eye(3, dtype=np.float32),
                                    labels=np.array(["X", "Y", "Z"], dtype=object), meta={})
    assert clap_genre.knn_predict(np.zeros(3, dtype=np.float32), ref2)[0] == "Unknown"


def test_bundled_reference_loads_and_classifies() -> None:
    """The shipped reference library loads, is normalized, and self-predicts."""
    assert clap_genre.bundled_clap_assets_dir() is not None
    ref = clap_genre.load_reference()
    n, dim = ref.emb.shape
    assert n > 100 and dim == 512
    # rows are unit-normalized
    norms = np.linalg.norm(ref.emb, axis=1)
    assert np.allclose(norms, 1.0, atol=1e-3)
    # a reference vector classifies to a real (non-empty, known) genre
    g, conf = clap_genre.knn_predict(ref.emb[0], ref)
    assert g and g != "Unknown"
    assert 0.0 <= conf <= 1.0


def test_segments_handles_short_audio() -> None:
    """_segments must not crash on audio shorter than one segment."""
    short = np.ones(1000, dtype=np.float32)
    arr = clap_genre._segments(short)
    assert arr.ndim == 2 and arr.shape[0] >= 1


def test_load_clap_model_errors_clearly_without_checkpoint(tmp_path) -> None:
    with pytest.raises(RuntimeError, match="checkpoint not found|CLAP"):
        clap_genre.load_clap_model(checkpoint=tmp_path / "nope.pt")
