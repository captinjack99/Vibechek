"""ONNX model download/hosting wiring in vibechek.analyzer.

Import-clean on a `[dev]`-only install: imports only vibechek.analyzer (no
essentia / onnxruntime / numpy-heavy paths). Verifies the hosted-head
constants stay consistent with the converted bundle + the onnx_backend loader.
"""
from __future__ import annotations

import vibechek.analyzer as az
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
    monkeypatch.setattr(az, "_USER_MODEL_BASE_URL", None)
    bases = az._onnx_head_bases()
    assert len(bases) == 1
    assert bases[0].endswith(az._ONNX_MODELS_RELEASE)
    assert bases[0].startswith("https://github.com/papapew/Vibechek/releases/download/")


def test_onnx_head_bases_respects_user_override(monkeypatch) -> None:
    monkeypatch.setattr(az, "_USER_MODEL_BASE_URL", "https://mirror.example/models")
    assert az._onnx_head_bases() == ["https://mirror.example/models"]


def test_download_models_default_engine_does_not_fetch_onnx(monkeypatch, tmp_path) -> None:
    """engine='essentia_tf' must not touch any .onnx — the TF path is unchanged."""
    fetched: list[str] = []

    def _fake_needs(path, url, expected=None):  # noqa: ANN001
        return False  # everything already present → no network

    monkeypatch.setattr(az, "_needs_download", _fake_needs)
    monkeypatch.setattr(
        az, "_download_from_mirrors",
        lambda urls, dest, **kw: fetched.append(str(dest)),  # noqa: ARG005
    )
    # Pretend the .pb/.json already exist so the loop records descriptors only.
    descriptors = az.download_models(tmp_path, engine="essentia_tf")
    assert "effnet_onnx" not in descriptors
    assert not any(f.endswith(".onnx") for f in fetched)
