"""Tests for the ONNX inference backend (vibechek.onnx_backend).

These run WITHOUT onnxruntime, essentia, or any model files installed — every
runtime dependency is either mocked (a fake `onnxruntime` module injected into
`sys.modules`) or exercised purely (the patch-windowing math). This keeps the
Windows dev .venv and CI green; the real end-to-end parity proof lives in
scripts/onnx_parity.py and is run by hand in the analysis environment.

Covered:
  * build_providers EP-chain filtering + use_gpu="off" → CPU only
  * the patch-window index math (hop 64) as a pure function
  * the inference-engine flag defaults to the safe "essentia_tf"
  * graceful, clear errors when onnxruntime / heads / backbone are absent
"""

from __future__ import annotations

import sys
import types
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest

from vibechek import onnx_backend as ob
from vibechek.config import AnalysisConfig

# ---------------------------------------------------------------------------
# Fake onnxruntime helper — inject a module exposing get_available_providers /
# InferenceSession so the lazy `import onnxruntime` inside the backend resolves
# to our stub without the real package installed.
# ---------------------------------------------------------------------------


@contextmanager
def fake_onnxruntime(available_providers, session_factory=None):
    """Temporarily install a fake `onnxruntime` module in sys.modules."""
    mod = types.ModuleType("onnxruntime")
    mod.get_available_providers = lambda: list(available_providers)
    if session_factory is not None:
        mod.InferenceSession = session_factory
    else:
        def _no_session(*a, **k):  # pragma: no cover - guard
            raise AssertionError("InferenceSession should not be called in this test")
        mod.InferenceSession = _no_session
    old = sys.modules.get("onnxruntime")
    sys.modules["onnxruntime"] = mod
    try:
        yield mod
    finally:
        if old is not None:
            sys.modules["onnxruntime"] = old
        else:
            sys.modules.pop("onnxruntime", None)


# ---------------------------------------------------------------------------
# build_providers
# ---------------------------------------------------------------------------


def test_build_providers_full_chain_filtered_to_available() -> None:
    """Only providers ORT reports as available survive, in priority order."""
    available = [
        "CPUExecutionProvider",
        "CUDAExecutionProvider",
        "DirectMLExecutionProvider",
    ]
    with fake_onnxruntime(available):
        chain = ob.build_providers("auto")
    # CUDA before DirectML before CPU (priority order), ROCm/CoreML dropped.
    assert chain == [
        "CUDAExecutionProvider",
        "DirectMLExecutionProvider",
        "CPUExecutionProvider",
    ]


def test_build_providers_cpu_only_when_nothing_else_available() -> None:
    with fake_onnxruntime(["CPUExecutionProvider"]):
        assert ob.build_providers("auto") == ["CPUExecutionProvider"]


def test_build_providers_off_forces_cpu_only() -> None:
    """use_gpu='off' returns CPU only even when a GPU EP is available."""
    available = ["CUDAExecutionProvider", "CPUExecutionProvider"]
    with fake_onnxruntime(available):
        assert ob.build_providers("off") == ["CPUExecutionProvider"]


def test_build_providers_off_with_no_cpu_returns_empty() -> None:
    """Degenerate build with no CPU EP and gpu off → empty (ORT defaults)."""
    with fake_onnxruntime(["CUDAExecutionProvider"]):
        assert ob.build_providers("off") == []


def test_build_providers_appends_cpu_fallback_if_missing_from_order() -> None:
    """CPU is guaranteed last even if it somehow wasn't in the priority list."""
    # Only CoreML + CPU available; CoreML ranks above CPU in `preferred`.
    with fake_onnxruntime(["CoreMLExecutionProvider", "CPUExecutionProvider"]):
        chain = ob.build_providers("on")
    assert chain == ["CoreMLExecutionProvider", "CPUExecutionProvider"]


def test_build_providers_rocm_priority() -> None:
    """AMD Linux: ROCm ranks above DirectML/CoreML, below CUDA."""
    available = [
        "CPUExecutionProvider",
        "ROCmExecutionProvider",
        "DirectMLExecutionProvider",
    ]
    with fake_onnxruntime(available):
        chain = ob.build_providers("auto")
    assert chain == [
        "ROCmExecutionProvider",
        "DirectMLExecutionProvider",
        "CPUExecutionProvider",
    ]


def test_build_providers_missing_onnxruntime_raises_importerror() -> None:
    """With no onnxruntime installed, the lazy import surfaces ImportError."""
    # Ensure no stub is present.
    assert "onnxruntime" not in sys.modules or not hasattr(
        sys.modules.get("onnxruntime"), "get_available_providers"
    )
    with pytest.raises(ImportError):
        ob.build_providers("auto")


# ---------------------------------------------------------------------------
# Patch-window math (pure; mirrors scripts/onnx_parity.py recipe)
# ---------------------------------------------------------------------------


def test_patch_indices_hop_64() -> None:
    """A 256-frame clip windows at 0, 64, 128 (last full 128-frame patch)."""
    assert ob._patch_indices(256) == [0, 64, 128]


def test_patch_indices_partial_tail_not_emitted() -> None:
    """range stops at the last START that yields a FULL 128-frame patch."""
    # 300 frames: starts 0,64,128 (192 would need frames 192..319 > 300).
    assert ob._patch_indices(300) == [0, 64, 128]
    # 320 frames: 0,64,128,192 (192..319 fits exactly).
    assert ob._patch_indices(320) == [0, 64, 128, 192]


def test_patch_indices_exact_one_patch() -> None:
    assert ob._patch_indices(128) == [0]


def test_patch_indices_short_clip_yields_single_zero() -> None:
    """Clips shorter than one patch still produce a single (padded) patch."""
    assert ob._patch_indices(50) == [0]
    assert ob._patch_indices(0) == [0]


def test_patch_indices_custom_hop() -> None:
    assert ob._patch_indices(256, patch_frames=128, hop=128) == [0, 128]


def test_make_patches_shape_and_padding() -> None:
    """_make_patches stacks [k,128,96] and zero-pads a short final clip."""
    mel = np.arange(300 * 96, dtype=np.float32).reshape(300, 96)
    patches = ob._make_patches(mel)
    assert patches.shape == (3, 128, 96)
    assert patches.dtype == np.float32
    # First patch is the first 128 frames unchanged.
    assert np.array_equal(patches[0], mel[:128])

    short = np.ones((50, 96), dtype=np.float32)
    padded = ob._make_patches(short)
    assert padded.shape == (1, 128, 96)
    assert np.array_equal(padded[0, :50], short)
    assert np.all(padded[0, 50:] == 0.0)  # tail zero-padded


def test_make_patches_empty_mel_yields_single_zero_patch() -> None:
    """An empty/near-empty decode (ultra-short audio) must still yield one patch.

    Regression: `_make_patches` read `mel.shape[1]` to size the pad, which raised
    `IndexError: tuple index out of range` on a 1-D `(0,)` mel array. ORT then
    surfaced an opaque 'EffNet embedding failed: tuple index out of range' for the
    track instead of analyzing it like the essentia path does.
    """
    # 1-D empty array — no zero mel frames at all (audio < ~32 ms).
    empty_1d = ob._make_patches(np.array([], dtype=np.float32))
    assert empty_1d.shape == (1, 128, 96)
    assert empty_1d.dtype == np.float32
    assert np.all(empty_1d == 0.0)

    # 2-D but zero rows — same contract.
    empty_2d = ob._make_patches(np.zeros((0, 96), dtype=np.float32))
    assert empty_2d.shape == (1, 128, 96)
    assert np.all(empty_2d == 0.0)


# ---------------------------------------------------------------------------
# Engine flag default
# ---------------------------------------------------------------------------


def test_inference_engine_defaults_to_essentia_tf() -> None:
    """The default engine keeps the unchanged TF path — onnx is opt-in."""
    assert AnalysisConfig().inference_engine == "essentia_tf"


def test_inference_engine_roundtrips_in_config(tmp_path: Path) -> None:
    from vibechek.config import VibechekConfig

    cfg = VibechekConfig()
    cfg.analysis.inference_engine = "onnx"
    target = tmp_path / "config.json"
    cfg.save(target)
    loaded = VibechekConfig.load(target)
    assert loaded.analysis.inference_engine == "onnx"


# ---------------------------------------------------------------------------
# load_onnx_models — graceful errors
# ---------------------------------------------------------------------------


def test_load_onnx_models_missing_onnxruntime_raises_clear_error() -> None:
    with pytest.raises(RuntimeError, match="onnxruntime is not installed"):
        ob.load_onnx_models(Path("/nonexistent/models"), "auto")


def test_load_onnx_models_missing_backbone_raises_clear_error(tmp_path: Path) -> None:
    """With onnxruntime present but no backbone .onnx, the error names the file."""
    with fake_onnxruntime(["CPUExecutionProvider"]):
        with pytest.raises(RuntimeError, match="Backbone ONNX not found"):
            ob.load_onnx_models(tmp_path, "auto")


def test_load_head_session_missing_file_points_at_conversion_script(tmp_path: Path) -> None:
    """A missing head .onnx error points at scripts/convert_heads_to_onnx.py."""
    with fake_onnxruntime(["CPUExecutionProvider"]):
        with pytest.raises(RuntimeError, match="convert_heads_to_onnx.py"):
            ob._load_head_session(tmp_path / "voice_instrumental.onnx")


def test_read_classes_reads_metadata(tmp_path: Path) -> None:
    meta = tmp_path / "voice_instrumental.json"
    meta.write_text('{"classes": ["instrumental", "voice"]}', encoding="utf-8")
    assert ob._read_classes(meta) == ["instrumental", "voice"]


def test_read_classes_missing_file_returns_empty(tmp_path: Path) -> None:
    assert ob._read_classes(tmp_path / "nope.json") == []


def test_read_classes_bad_json_returns_empty(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert ob._read_classes(bad) == []


# ---------------------------------------------------------------------------
# load_onnx_models — full wiring with fully-faked sessions
# ---------------------------------------------------------------------------


class _FakeSession:
    """Minimal InferenceSession stand-in: records the path, returns fixed shapes."""

    def __init__(self, path, providers=None):
        self.path = str(path)
        self.providers = providers
        self._is_backbone = "bsdynamic" in self.path

    def get_inputs(self):
        name = "melspectrogram" if self._is_backbone else "embedding"
        return [types.SimpleNamespace(name=name)]

    def run(self, output_names, feeds):
        x = next(iter(feeds.values()))
        k = x.shape[0]
        if self._is_backbone:
            # outputs[0] = [k,400] genre, outputs[1] = [k,1280] embedding.
            return [np.zeros((k, 400), np.float32), np.ones((k, 1280), np.float32)]
        return [np.zeros((k, 2), np.float32)]


def _write_model_dir(tmp_path: Path) -> Path:
    """Create dummy backbone + head .onnx/.json files (content irrelevant — the
    fake session never reads them, it only needs the files to exist)."""
    (tmp_path / ob.BACKBONE_ONNX_FILENAME).write_bytes(b"x")
    for stem, _key in ob._HEAD_SPECS:
        (tmp_path / f"{stem}.onnx").write_bytes(b"x")
        (tmp_path / f"{stem}.json").write_text('{"classes": ["a", "b"]}', encoding="utf-8")
    return tmp_path


def test_load_onnx_models_returns_analyzer_compatible_keys(tmp_path: Path) -> None:
    """The returned dict mirrors analyzer.load_models's keys and callables."""
    _write_model_dir(tmp_path)
    with fake_onnxruntime(["CPUExecutionProvider"], session_factory=_FakeSession):
        models = ob.load_onnx_models(tmp_path, "auto")

    # Same key surface as the essentia path.
    for key in ("effnet", "genre", "genre_classes", "danceability",
                "voice_instrumental", "aggressive", "happy", "relaxed", "sad"):
        assert key in models, f"missing key {key}"

    # genre_classes came from the head metadata.
    assert models["genre_classes"] == ["a", "b"]
    # Per-head class lists are present too.
    assert models["voice_instrumental_classes"] == ["a", "b"]


def test_load_onnx_models_heads_pinned_to_cpu(tmp_path: Path) -> None:
    """Head sessions always run on CPU (GPU-neutral), regardless of use_gpu."""
    _write_model_dir(tmp_path)
    with fake_onnxruntime(
        ["CUDAExecutionProvider", "CPUExecutionProvider"], session_factory=_FakeSession
    ):
        models = ob.load_onnx_models(tmp_path, "on")
    # The head session was constructed with CPU-only providers.
    assert models["danceability"]._sess.providers == ["CPUExecutionProvider"]


def test_onnx_callables_have_essentia_signatures(tmp_path: Path) -> None:
    """effnet(audio)->[k,1280]; genre/head(emb)->[k,classes]; genre reuses backbone."""
    _write_model_dir(tmp_path)
    with fake_onnxruntime(["CPUExecutionProvider"], session_factory=_FakeSession):
        models = ob.load_onnx_models(tmp_path, "auto")

    # Drive the backbone with a fake mel-spectrogram by stubbing _melspec so we
    # don't need essentia. 300 mel frames → 3 patches → [3,1280] embeddings.
    effnet = models["effnet"]
    with patch.object(type(effnet), "_melspec", return_value=np.zeros((300, 96), np.float32)):
        emb = effnet(np.zeros(48000, dtype=np.float32))
    assert emb.shape == (3, 1280)

    # Genre callable reuses the backbone's cached 400-class activations.
    genre = models["genre"](emb)
    assert genre.shape == (3, 400)

    # A head maps embeddings → [k, n_classes].
    voice = models["voice_instrumental"](emb)
    assert voice.shape == (3, 2)


def test_genre_callable_falls_back_to_head_when_no_cached(tmp_path: Path) -> None:
    """If the backbone hasn't run, genre falls back to the converted head."""
    _write_model_dir(tmp_path)
    with fake_onnxruntime(["CPUExecutionProvider"], session_factory=_FakeSession):
        models = ob.load_onnx_models(tmp_path, "auto")
    genre = models["genre"]
    # No backbone run yet → last_genre_activations is None → uses head session,
    # which (fake) returns [k, 2] for the given embedding.
    out = genre(np.ones((2, 1280), np.float32))
    assert out.shape == (2, 2)


def test_load_onnx_models_missing_head_raises(tmp_path: Path) -> None:
    """Backbone present but a required head .onnx missing → clear error."""
    (tmp_path / ob.BACKBONE_ONNX_FILENAME).write_bytes(b"x")
    # Only genre head present (optional); voice/danceability/moods missing.
    with fake_onnxruntime(["CPUExecutionProvider"], session_factory=_FakeSession):
        with pytest.raises(RuntimeError, match="convert_heads_to_onnx.py"):
            ob.load_onnx_models(tmp_path, "auto")
