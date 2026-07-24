"""Tests for the VRAM-aware GPU worker cap (audit landmine #11).

The cap logic lives in `vibechek.analyzer`:
- `_probe_free_vram_mb` shells out to `nvidia-smi` and returns free VRAM in MB
  for the SINGLE GPU our workers pin (device 0, or the honored
  CUDA_VISIBLE_DEVICES index) — NOT summed across all cards — or None on any
  failure. (Audit fix: summing over-provisioned workers that all landed on
  GPU 0 → OOM.)
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


def test_probe_returns_device0_on_multi_gpu_by_default() -> None:
    """Multi-GPU rig -> free VRAM of device 0 ONLY (workers all pin GPU 0).

    Audit fix: the old probe summed (8000+4000=12000) which over-provisioned
    workers that then all piled onto GPU 0 and OOM'd. We now report only the
    device the workers actually use.
    """
    with patch("shutil.which", return_value="/usr/bin/nvidia-smi"), \
         patch("subprocess.run", return_value=_mock_smi_result("8000\n4000\n")), \
         patch.dict("os.environ", {}, clear=False):
        # Ensure no CUDA_VISIBLE_DEVICES override leaks from the host env.
        with patch.dict("os.environ", {"CUDA_VISIBLE_DEVICES": ""}, clear=False):
            assert analyzer._probe_free_vram_mb() == 8000


def test_probe_honors_cuda_visible_devices_index() -> None:
    """CUDA_VISIBLE_DEVICES=1 -> report the SECOND nvidia-smi row's free VRAM."""
    with patch("shutil.which", return_value="/usr/bin/nvidia-smi"), \
         patch("subprocess.run", return_value=_mock_smi_result("8000\n4000\n")), \
         patch.dict("os.environ", {"CUDA_VISIBLE_DEVICES": "1"}, clear=False):
        assert analyzer._probe_free_vram_mb() == 4000


def test_probe_falls_back_to_first_row_when_index_out_of_range() -> None:
    """CUDA_VISIBLE_DEVICES points past the visible cards -> first row."""
    with patch("shutil.which", return_value="/usr/bin/nvidia-smi"), \
         patch("subprocess.run", return_value=_mock_smi_result("8000\n4000\n")), \
         patch.dict("os.environ", {"CUDA_VISIBLE_DEVICES": "5"}, clear=False):
        assert analyzer._probe_free_vram_mb() == 8000


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
    """A bad leading row shouldn't poison the probe — device 0 = first parseable.

    With "not-a-number\n8000\n4000\n" the malformed row is skipped, leaving
    [8000, 4000]; device 0 reports 8000.
    """
    with patch("shutil.which", return_value="/usr/bin/nvidia-smi"), \
         patch("subprocess.run", return_value=_mock_smi_result("not-a-number\n8000\n4000\n")), \
         patch.dict("os.environ", {"CUDA_VISIBLE_DEVICES": ""}, clear=False):
        assert analyzer._probe_free_vram_mb() == 8000


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


# NOTE: the GPU-cap DECISION (and its floor-to-0 fix — a near-empty card now
# yields 0 GPU workers + a reason, not a doomed 1-worker pool that __init_fail__s
# the whole run) moved into the pure vibechek.resources.compute_worker_budget.
# It's exercised table-driven in tests/test_worker_budget.py. The probe below is
# unchanged, so its tests stay here.


def test_gpu_worker_mb_is_sane() -> None:
    """Guard against accidental tuning regressions.

    The per-worker budget should stay in the 1.5-3 GB range. Below 1.5 GB
    risks OOM cascades (model weights ~500 MB + CUDA context ~800 MB +
    activation buffers ~400 MB + growth-allocator fragmentation ~700 MB =
    ~2.4 GB at steady state under contention); above 3 GB starves big-VRAM
    cards of useful parallelism for diminishing reliability gains.
    """
    assert 1500 <= analyzer._GPU_WORKER_MB <= 3000


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


# ---------------------------------------------------------------------------
# Vocal classification calibration (bug: instrumental dance mislabelled Vocal)
# ---------------------------------------------------------------------------


def test_classify_vocal_instrumental_dance_not_vocal() -> None:
    """Robert Miles 'Children' and Eric Prydz 'Pjanoo' are instrumental but
    the model rates their melodic leads high. Measured on the user's library:
    Children scored 0.703, Pjanoo 0.642. The 0.72 instrumental cutoff is set
    specifically so Children (0.70-0.71) lands as Instrumental, not Vocal."""
    assert analyzer._classify_vocal(0.703) == "Instrumental"  # Children (measured)
    assert analyzer._classify_vocal(0.71) == "Instrumental"   # Children upper bound
    assert analyzer._classify_vocal(0.642) == "Instrumental"  # Pjanoo (measured)
    assert analyzer._classify_vocal(0.06) == "Instrumental"   # pure instrumental


def test_classify_vocal_true_vocals_still_vocal() -> None:
    assert analyzer._classify_vocal(0.97) == "Vocal"   # Adele
    assert analyzer._classify_vocal(0.98) == "Vocal"   # Aerosmith
    assert analyzer._classify_vocal(0.89) == "Vocal"   # vocal dance


def test_classify_vocal_light_band() -> None:
    # Between the two cutoffs → the hedged "Light Vocal".
    assert analyzer._classify_vocal(0.78) == "Light Vocal"


def test_classify_vocal_thresholds_are_configurable() -> None:
    # A caller can override the cutoffs (e.g. a stricter or looser profile).
    assert analyzer._classify_vocal(0.65, instrumental_max=0.5) == "Light Vocal"
    assert analyzer._classify_vocal(0.65, instrumental_max=0.5, full_min=0.6) == "Vocal"


# --- _reconcile_record_vocal: feat-credit metadata prior ---
# The voice model means its per-frame score over the whole track, so a vocal
# track with long instrumental intros/breaks reads low and mislabels as
# Instrumental. A "feat." credit is a near-certain vocal signal with no false
# positives on instrumentals, so we upgrade ONLY the Instrumental case.


def _rec(score, title="", artist=""):
    return {"ml_analysis": {"ml_vocal_score": score}, "filename_title": title,
            "filename_artist": artist, "existing_tags": {}}


def test_vocal_feat_credit_upgrades_diluted_instrumental() -> None:
    # 0.43 = ODESZA feat. Zyra "It's Only" (measured) — diluted mean, real vocals.
    r = _rec(0.43, "It's Only (feat. Zyra)")
    analyzer._reconcile_record_vocal(r)
    assert r["ml_analysis"]["ml_vocal"] == "Vocal"
    assert r["ml_analysis"]["ml_vocal_source"] == "feat_credit"
    assert r["ml_analysis"]["ml_vocal_audio"] == "Instrumental"  # the un-upgraded read


def test_vocal_feat_credit_matches_ft_and_artist_field() -> None:
    r = _rec(0.3, "Chemicals (Feat. Nat Slater)")
    analyzer._reconcile_record_vocal(r)
    assert r["ml_analysis"]["ml_vocal"] == "Vocal"
    r2 = _rec(0.3, "Some Title", artist="Artist ft. Vocalist")
    analyzer._reconcile_record_vocal(r2)
    assert r2["ml_analysis"]["ml_vocal"] == "Vocal"


def test_vocal_no_feat_instrumental_stays_instrumental() -> None:
    # Robert Miles "Children" (0.703) — instrumental, no feat credit. The
    # deliberately-tuned 0.72 cutoff must hold; the prior must NOT touch it.
    r = _rec(0.703, "Children")
    analyzer._reconcile_record_vocal(r)
    assert r["ml_analysis"]["ml_vocal"] == "Instrumental"
    assert r["ml_analysis"]["ml_vocal_source"] == "audio"


def test_vocal_feat_does_not_match_substrings() -> None:
    # "soft"/"feature"/"drift"/"with" must not trigger the feat prior.
    for title in ("Soft Touch", "Feature Presentation", "Drifting", "Dancing With Myself"):
        r = _rec(0.2, title)
        analyzer._reconcile_record_vocal(r)
        assert r["ml_analysis"]["ml_vocal"] == "Instrumental", title


def test_vocal_feat_credit_does_not_change_confident_vocal() -> None:
    # A confident audio vocal read on a feat track stays Vocal via the audio path.
    r = _rec(0.95, "Track (feat. Singer)")
    analyzer._reconcile_record_vocal(r)
    assert r["ml_analysis"]["ml_vocal"] == "Vocal"
    assert r["ml_analysis"]["ml_vocal_source"] == "audio"  # audio already said Vocal


def test_vocal_reconcile_idempotent_and_skips_missing_score() -> None:
    r = _rec(0.43, "It's Only (feat. Zyra)")
    analyzer._reconcile_record_vocal(r)
    analyzer._reconcile_record_vocal(r)  # second pass
    assert r["ml_analysis"]["ml_vocal"] == "Vocal"
    assert r["ml_analysis"]["ml_vocal_source"] == "feat_credit"
    # No score → untouched (model didn't run).
    r2 = {"ml_analysis": {"ml_vocal": "Unknown"}, "filename_title": "X (feat. Y)"}
    analyzer._reconcile_record_vocal(r2)
    assert r2["ml_analysis"]["ml_vocal"] == "Unknown"


# --- _vote_key: 3-segment majority vote (parallel-key confusion suppressor) ---


class _SeqKey:
    """Fake KeyExtractor returning queued (key, scale) reads in call order and
    recording the sample-length of each chunk it was handed."""

    def __init__(self, reads: list[tuple[str, str]]) -> None:
        self._reads = list(reads)
        self.call_lengths: list[int] = []

    def __call__(self, audio):  # noqa: ANN001
        self.call_lengths.append(len(audio))
        key, scale = self._reads.pop(0)
        return (key, scale, 0.9)


def test_vote_key_majority_overrides_a_flipped_segment() -> None:
    """The real win: two segments hear D major, one flips to the parallel D
    minor → the major reading wins (it would have lost on a single full read)."""
    np = pytest.importorskip("numpy")
    audio = np.zeros(3 * analyzer._MIN_KEY_SEGMENT_SAMPLES, dtype=np.float32)
    ke = _SeqKey([("D", "major"), ("D", "major"), ("D", "minor")])
    assert analyzer._vote_key(ke, audio) == "10B"  # D major
    assert len(ke.call_lengths) == 3  # segmented, never read the whole track


def test_vote_key_tie_resolves_to_first_segment() -> None:
    """All three segments disagree → deterministic fall-back to segment 0."""
    np = pytest.importorskip("numpy")
    audio = np.zeros(3 * analyzer._MIN_KEY_SEGMENT_SAMPLES, dtype=np.float32)
    ke = _SeqKey([("A", "minor"), ("C", "major"), ("E", "minor")])
    assert analyzer._vote_key(ke, audio) == "8A"  # A minor, the first read


def test_vote_key_short_audio_falls_back_to_one_full_read() -> None:
    """Below KEY_VOTE_SEGMENTS × 1 s there isn't enough audio to segment, so a
    single full-track read is used instead of voting."""
    np = pytest.importorskip("numpy")
    audio = np.zeros(analyzer._MIN_KEY_SEGMENT_SAMPLES, dtype=np.float32)  # 1 s
    ke = _SeqKey([("F", "minor")])
    assert analyzer._vote_key(ke, audio) == "4A"  # F minor
    assert ke.call_lengths == [analyzer._MIN_KEY_SEGMENT_SAMPLES]  # one full read


def test_vote_key_one_bad_segment_does_not_sink_the_vote() -> None:
    np = pytest.importorskip("numpy")
    audio = np.zeros(3 * analyzer._MIN_KEY_SEGMENT_SAMPLES, dtype=np.float32)

    class _Flaky:
        def __init__(self) -> None:
            self.n = 0

        def __call__(self, _audio):  # noqa: ANN001
            self.n += 1
            if self.n == 2:
                raise RuntimeError("essentia hiccup on segment 2")
            return ("G", "major", 0.9)

    assert analyzer._vote_key(_Flaky(), audio) == "9B"  # G major from 2 good reads


def test_vote_key_unparseable_reads_return_none() -> None:
    np = pytest.importorskip("numpy")
    audio = np.zeros(3 * analyzer._MIN_KEY_SEGMENT_SAMPLES, dtype=np.float32)
    ke = _SeqKey([("?", "?"), ("?", "?"), ("?", "?")])
    assert analyzer._vote_key(ke, audio) is None


def test_classify_vocal_sparse_hook_branch() -> None:
    """Calibrated 2026-07-24 against 30 human-labeled tracks: a low MEAN with
    strong-but-sparse vocal patches is a vocal track (chopped hook), while the
    same peak spread across most of the track is a melodic instrumental."""
    # Measured profiles from the labeled set (mean, p90, frac>0.5):
    # "ghost (feat. HUMAN)" — human label Vocal, shipped label was Instrumental.
    assert analyzer._classify_vocal(0.297, peak=0.997, frac=0.284) == "Vocal"
    # "Night Vision" — human label Vocal.
    assert analyzer._classify_vocal(0.226, peak=0.926, frac=0.217) == "Vocal"
    # "Children" — melodic-lead INSTRUMENTAL anchor: p90 0.99 but voice-like
    # for 70% of its patches → the frac cap must keep it Instrumental.
    assert analyzer._classify_vocal(0.681, peak=0.987, frac=0.703) == "Instrumental"
    # "Pjanoo" — same anchor class.
    assert analyzer._classify_vocal(0.631, peak=0.984, frac=0.632) == "Instrumental"
    # "Adagio For Strings" — true instrumental, low everything.
    assert analyzer._classify_vocal(0.069, peak=0.207, frac=0.015) == "Instrumental"
    # Weak peak (sampled vocals the model can't see) stays Instrumental —
    # the feat-credit prior is the safety net for those.
    assert analyzer._classify_vocal(0.221, peak=0.511, frac=0.116) == "Instrumental"


def test_classify_vocal_mean_bands_unchanged_by_patch_shape() -> None:
    """The proven mean bands outrank the sparse branch: a sustained vocal is
    Vocal regardless of frac, and the Light Vocal band is untouched."""
    assert analyzer._classify_vocal(0.95, peak=1.0, frac=0.98) == "Vocal"
    assert analyzer._classify_vocal(0.80, peak=0.99, frac=0.60) == "Light Vocal"


def test_classify_vocal_backcompat_without_patch_shape() -> None:
    """Older analyses carry only the mean — behavior must match the historical
    mean-only rule exactly (no peak/frac → no sparse branch)."""
    assert analyzer._classify_vocal(0.297) == "Instrumental"
    assert analyzer._classify_vocal(0.703) == "Instrumental"
    assert analyzer._classify_vocal(0.80) == "Light Vocal"
    assert analyzer._classify_vocal(0.95) == "Vocal"
