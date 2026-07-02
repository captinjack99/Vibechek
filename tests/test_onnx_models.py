"""ONNX model download/hosting wiring in vibechek.analyzer.

Import-clean on a `[dev]`-only install: imports only vibechek.analyzer (no
essentia / onnxruntime / numpy-heavy paths). Verifies the hosted-head
constants stay consistent with the converted bundle + the onnx_backend loader.
"""
from __future__ import annotations

import vibechek.analyzer as az
import vibechek.model_download as md
from vibechek.onnx_backend import _HEAD_SPECS


def test_every_required_head_has_pinned_sha256() -> None:
    """Each ONNX head ships a pinned .onnx + .json digest (verify is enforced)."""
    for stem in az._ONNX_HEAD_STEMS:
        assert f"{stem}.onnx" in az.MODEL_SHA256_ONNX, f"missing .onnx hash for {stem}"
        assert f"{stem}.json" in az.MODEL_SHA256_ONNX, f"missing .json hash for {stem}"


def test_pinned_hashes_are_sha256_hex() -> None:
    """Every pinned digest is a 64-char lowercase hex string."""
    for name, digest in az.MODEL_SHA256_ONNX.items():
        assert len(digest) == 64, f"{name}: not a sha256 ({len(digest)} chars)"
        int(digest, 16)  # raises ValueError if not hex
        assert digest == digest.lower(), f"{name}: digest must be lowercase"


def test_head_stems_match_onnx_backend_loader() -> None:
    """analyzer's download list matches the heads onnx_backend actually loads."""
    backend_stems = {file_stem for file_stem, _key in _HEAD_SPECS}
    assert set(az._ONNX_HEAD_STEMS) == backend_stems


def test_onnx_head_bases_default_points_at_release(monkeypatch) -> None:
    # _onnx_head_bases + its mirror constants live in vibechek.model_download now
    # (analyzer re-exports the public names, but this reads the module global the
    # function actually closes over, so patch it at its real home).
    monkeypatch.setattr(md, "_USER_MODEL_BASE_URL", None)
    bases = md._onnx_head_bases()
    assert len(bases) == 1
    assert bases[0].endswith(md._ONNX_MODELS_RELEASE)
    assert bases[0].startswith("https://github.com/captinjack99/Vibechek/releases/download/")


def test_onnx_head_bases_respects_user_override(monkeypatch) -> None:
    monkeypatch.setattr(md, "_USER_MODEL_BASE_URL", "https://mirror.example/models")
    assert md._onnx_head_bases() == ["https://mirror.example/models"]


def test_analyze_directory_rpc_threads_inference_engine(monkeypatch) -> None:
    """Regression: the analyze RPC must pass the selected engine into the
    AnalysisConfig. If it doesn't, the ONNX toggle is inert at analyze time
    (config defaults to essentia_tf) — the bug that made ONNX silently no-op.
    """
    import vibechek.analyzer as analyzer_mod
    import vibechek.rpc as rpc

    captured: dict = {}

    def fake_analyze(library_path, *, config, **_kw):  # noqa: ANN001
        captured["engine"] = config.inference_engine
        return {"summary": {}, "tracks": []}

    monkeypatch.setattr(analyzer_mod, "analyze_directory", fake_analyze)
    rpc._analyze_directory({"path": ".", "inference_engine": "onnx", "auto_save": False})
    assert captured["engine"] == "onnx"


def test_download_models_rpc_threads_engine(monkeypatch) -> None:
    """Regression: the download RPC must pass `engine` through (else ONNX
    downloads the TF .pb set and then can't find its .onnx files)."""
    import vibechek.analyzer as analyzer_mod
    import vibechek.rpc as rpc

    captured: dict = {}
    monkeypatch.setattr(
        analyzer_mod, "download_models",
        lambda target, on_progress=None, engine="essentia_tf": captured.update(engine=engine) or {},
    )
    rpc._download_models({"models_dir": ".", "engine": "onnx"})
    assert captured["engine"] == "onnx"


def test_preflight_rpc_threads_engine(monkeypatch) -> None:
    """Regression: the preflight RPC must forward `engine` so it checks the
    venv-onnx + .onnx models (not the TF venv/.pb)."""
    import vibechek.preflight as preflight_mod
    import vibechek.rpc as rpc

    captured: dict = {}
    monkeypatch.setattr(
        preflight_mod, "preflight",
        lambda md=None, *, quick_wsl=True, engine="essentia_tf": captured.update(engine=engine) or "PFR",
    )
    monkeypatch.setattr(preflight_mod, "to_dict", lambda _r: {})
    rpc._preflight({"engine": "onnx"})
    assert captured["engine"] == "onnx"


def test_engine_gpu_status_rpc_threads_engine(monkeypatch) -> None:
    """Regression: the GPU-status RPC must forward `engine` so the ONNX engine's
    GPU is probed via onnxruntime in venv-onnx (not the TF venv)."""
    import vibechek.rpc as rpc
    import vibechek.wsl as wsl_mod

    captured: dict = {}
    monkeypatch.setattr(
        wsl_mod, "probe_engine_gpu",
        lambda distro, *, force=False, engine="essentia_tf": captured.update(engine=engine),
    )
    monkeypatch.setattr(wsl_mod, "engine_gpu_info_to_dict", lambda _info: {})
    rpc._engine_gpu_status({"engine": "onnx"})
    assert captured["engine"] == "onnx"


def test_download_models_default_engine_does_not_fetch_onnx(monkeypatch, tmp_path) -> None:
    """engine='essentia_tf' must not touch any .onnx — the TF path is unchanged."""
    fetched: list[str] = []

    def _fake_needs(path, url, expected=None):  # noqa: ANN001
        return False  # everything already present → no network

    # Patch at model_download (the downloader's real home) — download_models
    # calls its OWN module globals, so patching the analyzer re-export is inert
    # and the test would hit the real network (it did: a Windows runner with no
    # route to essentia.upf.edu failed here while networked runners "passed").
    monkeypatch.setattr(md, "_needs_download", _fake_needs)
    monkeypatch.setattr(
        md, "_download_from_mirrors",
        lambda urls, dest, **kw: fetched.append(str(dest)),  # noqa: ARG005
    )
    # This test is about WHICH set gets fetched, not content integrity — the
    # fake "already present" files don't exist on disk, so blank the pin table
    # or the (now-populated) strict verification correctly fails every model.
    monkeypatch.setattr(md, "MODEL_SHA256", {})
    # Pretend the .pb/.json already exist so the loop records descriptors only.
    descriptors = md.download_models(tmp_path, engine="essentia_tf")
    assert "effnet_onnx" not in descriptors
    assert not any(f.endswith(".onnx") for f in fetched)
