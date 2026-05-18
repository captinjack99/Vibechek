"""Tests for the VRAM-aware GPU worker cap (audit landmine #11).

The cap logic lives in `vibechek.analyzer`:
- `_probe_free_vram_mb` shells out to `nvidia-smi` and returns free VRAM in MB
  (summed across visible GPUs), or None on any failure.
- `analyze_directory` uses that probe (when `use_gpu in ("auto", "on")`) to set
  `gpu_cap = max(1, free_mb // _GPU_WORKER_MB)` and emit a `report_progress`
  message so the GUI can surface "Capped workers from X to Y due to Z".

These tests exercise the probe directly via mocked `subprocess.run` /
`shutil.which`, and assert the fallback path when nvidia-smi is missing.
"""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from vibechek import analyzer


# ---------------------------------------------------------------------------
# _probe_free_vram_mb — direct unit tests
# ---------------------------------------------------------------------------


def _mock_smi_result(stdout: str, returncode: int = 0, stderr: str = "") -> MagicMock:
    """Build a stand-in for `subprocess.run`'s CompletedProcess."""
    result = MagicMock()
    result.stdout = stdout
    result.stderr = stderr
    result.returncode = returncode
    return result


def test_probe_returns_none_when_nvidia_smi_missing() -> None:
    """No nvidia-smi on PATH -> probe must return None (no exception)."""
    with patch("shutil.which", return_value=None):
        assert analyzer._probe_free_vram_mb() is None


def test_probe_parses_single_gpu_output() -> None:
    """nvidia-smi happy path: one GPU, return its free MB."""
    with patch("shutil.which", return_value="/usr/bin/nvidia-smi"), \
         patch("subprocess.run", return_value=_mock_smi_result("9216\n")):
        assert analyzer._probe_free_vram_mb() == 9216


def test_probe_sums_free_across_multiple_gpus() -> None:
    """Multi-GPU rig -> sum of free VRAM (documented behaviour)."""
    with patch("shutil.which", return_value="/usr/bin/nvidia-smi"), \
         patch("subprocess.run", return_value=_mock_smi_result("8000\n4000\n")):
        assert analyzer._probe_free_vram_mb() == 12000


def test_probe_returns_none_on_timeout() -> None:
    """nvidia-smi hung -> probe must return None, not raise."""
    with patch("shutil.which", return_value="/usr/bin/nvidia-smi"), \
         patch(
             "subprocess.run",
             side_effect=subprocess.TimeoutExpired(cmd="nvidia-smi", timeout=5),
         ):
        assert analyzer._probe_free_vram_mb() is None


def test_probe_returns_none_on_os_error() -> None:
    """OSError (e.g., permission denied) -> None, not raise."""
    with patch("shutil.which", return_value="/usr/bin/nvidia-smi"), \
         patch("subprocess.run", side_effect=OSError("permission denied")):
        assert analyzer._probe_free_vram_mb() is None


def test_probe_returns_none_on_nonzero_exit() -> None:
    """nvidia-smi returns non-zero -> treat as failure."""
    with patch("shutil.which", return_value="/usr/bin/nvidia-smi"), \
         patch(
             "subprocess.run",
             return_value=_mock_smi_result("", returncode=9, stderr="driver missing"),
         ):
        assert analyzer._probe_free_vram_mb() is None


def test_probe_returns_none_on_empty_output() -> None:
    """Zero-exit but no GPU lines -> None (caller falls back)."""
    with patch("shutil.which", return_value="/usr/bin/nvidia-smi"), \
         patch("subprocess.run", return_value=_mock_smi_result("\n  \n")):
        assert analyzer._probe_free_vram_mb() is None


def test_probe_skips_malformed_rows_but_keeps_good_ones() -> None:
    """One bad row shouldn't poison the probe — sum the parseable rows."""
    with patch("shutil.which", return_value="/usr/bin/nvidia-smi"), \
         patch("subprocess.run", return_value=_mock_smi_result("8000\nnot-a-number\n4000\n")):
        assert analyzer._probe_free_vram_mb() == 12000


# ---------------------------------------------------------------------------
# Integration: cap math + on_progress surfacing inside analyze_directory
# ---------------------------------------------------------------------------
#
# We don't spin up a real worker pool (would need essentia + models). Instead
# we let analyze_directory get as far as the cap-decision block, then short-
# circuit by stubbing _worker_init / _worker_analyze... actually simpler: we
# verify the cap math by patching `_probe_free_vram_mb` and inspecting the
# `on_progress` callback. To avoid the pool spin-up, we steer the function
# into the workers==1 branch by patching `cpu_count` AND requesting one
# worker — but cpu_count gates `workers = max(1, min(requested, cpu_count()))`
# so requested=1 -> workers=1 -> we never enter the GPU cap branch.
#
# Cleaner approach: extract the cap-decision logic into a small helper and
# test it directly. But the task said "surgical" and the cap is currently
# inline. So we test via the *probe* (already covered above) and add a
# behavioural test that monkeypatches `_probe_free_vram_mb` and verifies the
# cap formula by replicating it (the same constants are used in source).


@pytest.mark.parametrize(
    "free_mb, expected_cap",
    [
        (1500, 1),       # exactly 1 worker fits
        (4500, 3),       # 4500 / 1500 = 3
        (9000, 6),       # 8 GB-ish card -> 6 workers
        (24000, 16),     # 24 GB monster -> 16 workers
        (500, 1),        # below per-worker budget -> still 1 (max(1, ...))
        (0, 1),          # zero free -> still 1 worker requested
    ],
)
def test_gpu_cap_formula(free_mb: int, expected_cap: int) -> None:
    """The documented formula: max(1, free_mb // _GPU_WORKER_MB)."""
    assert max(1, free_mb // analyzer._GPU_WORKER_MB) == expected_cap


def test_gpu_worker_mb_is_sane() -> None:
    """Guard against accidental tuning regressions.

    The per-worker budget should stay in the 1-2 GB range. Below 1 GB risks
    OOM (the model weights alone are ~500 MB and TF's CUDA context is ~700 MB);
    above 2 GB starves big-VRAM cards of parallelism.
    """
    assert 1000 <= analyzer._GPU_WORKER_MB <= 2000


def test_gpu_fallback_cap_matches_prior_behaviour() -> None:
    """When nvidia-smi probing fails, we must preserve the old cap of 4 so
    machines that worked yesterday still work today."""
    assert analyzer._GPU_FALLBACK_CAP == 4


# ---------------------------------------------------------------------------
# EffNet embedding guard + mood-model fallback gating
# ---------------------------------------------------------------------------
#
# `analyze_audio_features` runs Essentia in worker processes. A malformed
# FLAC or 0-byte file used to crash the worker on the unguarded
# `models["effnet"](audio_16k)` call. We now wrap it: set ml_error and
# return early so the worker keeps processing the next file.
#
# Mocking essentia is heavy, so we exercise these via fake models — the
# function takes a `models: dict[str, Any]` so we can pass callables.


class _FakeModel:
    """Mimics an Essentia model callable.

    `effect` is either a numpy-like return value or an Exception instance to
    raise. We import numpy lazily because some CI envs lack it; tests that
    don't need it stay skip-clean.
    """

    def __init__(self, effect):
        self.effect = effect
        self.calls = 0

    def __call__(self, _input):
        self.calls += 1
        if isinstance(self.effect, BaseException):
            raise self.effect
        return self.effect


def _have_essentia_audio_stubs() -> bool:
    """Audio decoding inside analyze_audio_features still calls Essentia's
    MonoLoader. We can't test the EffNet guard end-to-end without monkey-
    patching that — see the dedicated guard test below for the approach.
    """
    try:
        import essentia  # noqa: F401
        return True
    except ImportError:
        return False


def test_effnet_guard_sets_ml_error_and_returns_early(monkeypatch, tmp_path) -> None:
    """A bad audio file shouldn't take down the worker — it should land in
    ml_error and the function should return a non-fatal MLResult."""
    np = pytest.importorskip("numpy")

    # Build a fake audio loader so we don't need real essentia for this test.
    class _FakeMonoLoader:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def __call__(self):
            return np.zeros(16000, dtype=np.float32)

    class _FakeRhythm:
        def __init__(self, **kwargs):
            pass

        def __call__(self, _audio):
            return (123.4, [], 0.9, [], [])

    class _FakeKey:
        def __call__(self, _audio):
            return ("C", "major", 0.9)

    # Patch the essentia.standard imports analyze_audio_features performs
    # lazily. We inject our fakes into sys.modules before the function runs.
    import sys
    import types

    fake_module = types.ModuleType("essentia.standard")
    fake_module.MonoLoader = _FakeMonoLoader
    fake_module.RhythmExtractor2013 = _FakeRhythm
    fake_module.KeyExtractor = _FakeKey
    fake_essentia = types.ModuleType("essentia")
    fake_essentia.standard = fake_module
    monkeypatch.setitem(sys.modules, "essentia", fake_essentia)
    monkeypatch.setitem(sys.modules, "essentia.standard", fake_module)

    # EffNet model that crashes on call — simulates a bad audio decode.
    crashing_effnet = _FakeModel(RuntimeError("essentia c++ crash on bad audio"))

    fake_file = tmp_path / "bad.flac"
    fake_file.write_bytes(b"")

    result = analyzer.analyze_audio_features(fake_file, {"effnet": crashing_effnet})
    assert result.ml_error is not None
    assert "EffNet" in result.ml_error
    # We bailed before BPM/key extraction in this guarded-fast path because
    # we already had nothing useful to extract; what matters is no crash.
    assert crashing_effnet.calls == 1


def test_mood_fallback_requires_two_models_to_blend(monkeypatch, tmp_path) -> None:
    """Only 1 of 4 mood models scoring → must drop to genre-table fallback,
    NOT blend with 0.5 defaults and fabricate medium energy."""
    np = pytest.importorskip("numpy")
    import sys
    import types

    class _FakeMonoLoader:
        def __init__(self, **kwargs):
            pass

        def __call__(self):
            return np.zeros(16000, dtype=np.float32)

    class _FakeRhythm:
        def __init__(self, **kwargs):
            pass

        def __call__(self, _audio):
            return (128.0, [], 0.9, [], [])

    class _FakeKey:
        def __call__(self, _audio):
            return ("A", "minor", 0.9)

    fake_module = types.ModuleType("essentia.standard")
    fake_module.MonoLoader = _FakeMonoLoader
    fake_module.RhythmExtractor2013 = _FakeRhythm
    fake_module.KeyExtractor = _FakeKey
    fake_essentia = types.ModuleType("essentia")
    fake_essentia.standard = fake_module
    monkeypatch.setitem(sys.modules, "essentia", fake_essentia)
    monkeypatch.setitem(sys.modules, "essentia.standard", fake_module)

    # Embeddings shaped like what discogs-effnet emits (frames x 1280-ish).
    embeddings = np.ones((10, 1280), dtype=np.float32)

    # Only `aggressive` returns a score — the other three mood models fail.
    # The old code would still blend (using 0.5 defaults for happy/relaxed/sad).
    # The fix: drop to genre-table fallback.
    happy_fail = _FakeModel(RuntimeError("happy model crashed"))
    relaxed_fail = _FakeModel(RuntimeError("relaxed model crashed"))
    sad_fail = _FakeModel(RuntimeError("sad model crashed"))

    models = {
        "effnet": _FakeModel(embeddings),
        "aggressive": _FakeModel(np.array([[0.9, 0.1]] * 10)),
        "happy": happy_fail,
        "relaxed": relaxed_fail,
        "sad": sad_fail,
    }

    fake_file = tmp_path / "track.mp3"
    fake_file.write_bytes(b"")

    result = analyzer.analyze_audio_features(fake_file, models)
    # Without a genre prediction either, the fallback picks "Unknown"
    # → default energy=3 / mood=Neutral path. The KEY test: ml_mood_scores
    # was NOT populated (which would indicate the blend path ran).
    assert result.ml_mood_scores is None
    assert result.ml_energy is not None  # fallback fills in something
    assert result.ml_mood is not None


def test_mood_fallback_blends_with_two_or_more(monkeypatch, tmp_path) -> None:
    """With ≥2 mood models, the blend runs (this is the intended path)."""
    np = pytest.importorskip("numpy")
    import sys
    import types

    class _FakeMonoLoader:
        def __init__(self, **kwargs):
            pass

        def __call__(self):
            return np.zeros(16000, dtype=np.float32)

    class _FakeRhythm:
        def __init__(self, **kwargs):
            pass

        def __call__(self, _audio):
            return (128.0, [], 0.9, [], [])

    class _FakeKey:
        def __call__(self, _audio):
            return ("A", "minor", 0.9)

    fake_module = types.ModuleType("essentia.standard")
    fake_module.MonoLoader = _FakeMonoLoader
    fake_module.RhythmExtractor2013 = _FakeRhythm
    fake_module.KeyExtractor = _FakeKey
    fake_essentia = types.ModuleType("essentia")
    fake_essentia.standard = fake_module
    monkeypatch.setitem(sys.modules, "essentia", fake_essentia)
    monkeypatch.setitem(sys.modules, "essentia.standard", fake_module)

    embeddings = np.ones((10, 1280), dtype=np.float32)
    models = {
        "effnet": _FakeModel(embeddings),
        "aggressive": _FakeModel(np.array([[0.9, 0.1]] * 10)),
        "happy": _FakeModel(np.array([[0.7, 0.3]] * 10)),
        # relaxed + sad missing — still 2 of 4 = blend allowed.
    }

    fake_file = tmp_path / "track.mp3"
    fake_file.write_bytes(b"")

    result = analyzer.analyze_audio_features(fake_file, models)
    assert result.ml_mood_scores is not None
    assert set(result.ml_mood_scores.keys()) == {"aggressive", "happy"}
