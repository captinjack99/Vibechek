"""Tests for the 'native' inference engine (WSL-free Windows path).

"native" = ONNX inference + the pure-NumPy mel frontend + a DSP-only native
essentia wheel (decode/BPM/key) run in-process. It uses the same ONNX model set
as "onnx" and FORCES the NumPy frontend (the DSP-only wheel ships no
TensorflowInputMusiCNN). Preflight routes it analyze_via="native" when essentia
imports locally (covered by the existing preflight behavior).
"""

from __future__ import annotations

from pathlib import Path

import vibechek.analyzer as az
from vibechek.config import _DEFAULT_INFERENCE_ENGINE, AnalysisConfig, _subset
from vibechek.preflight import _model_files_for_engine


def test_config_native_engine_is_windows_only() -> None:
    """"native" is the bundled-Windows-installer engine. On Linux/macOS the
    onnx engine IS the in-process path and the managed venvs have no native
    flavor, so a saved config carrying "native" (e.g. copied from a Windows
    box) snaps back to the platform default instead of preflighting one venv
    and analyzing against another. CI's three OS legs exercise both branches
    for real."""
    import sys
    cfg = _subset(AnalysisConfig, {"inference_engine": "native"})
    if sys.platform == "win32":
        assert cfg.inference_engine == "native"
    else:
        assert cfg.inference_engine == "essentia_tf"


def test_config_snaps_unknown_engine_to_default() -> None:
    cfg = _subset(AnalysisConfig, {"inference_engine": "ONNX_typo"})
    assert cfg.inference_engine == _DEFAULT_INFERENCE_ENGINE


def test_preflight_native_checks_the_onnx_model_set(tmp_path: Path) -> None:
    native = [n for n, *_ in _model_files_for_engine("native", tmp_path)]
    onnx = [n for n, *_ in _model_files_for_engine("onnx", tmp_path)]
    assert native == onnx
    assert any("backbone" in n for n in native)


def test_load_models_native_forces_numpy_frontend(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_load_onnx(model_dir, use_gpu="auto", numpy_frontend=None):
        captured["numpy_frontend"] = numpy_frontend
        return {"effnet": object()}

    import vibechek.onnx_backend as ob

    monkeypatch.setattr(az, "download_models", lambda *a, **k: {})
    monkeypatch.setattr(ob, "load_onnx_models", fake_load_onnx)
    monkeypatch.setattr(az, "_maybe_load_clap", lambda *a, **k: None)

    az.load_models(Path("models"), engine="native")
    assert captured["numpy_frontend"] is True

    captured.clear()
    az.load_models(Path("models"), engine="onnx")
    # "onnx" leaves it None (honors the VIBECHEK_NUMPY_FRONTEND env default).
    assert captured["numpy_frontend"] is None
