"""Regression: model downloads must never DESTROY a valid cached file.

Two coupled bugs (found driving the ONNX engine, whose head .onnx mirror isn't
hosted yet so it 404s):

  1. `_needs_download` re-fetched a locally-present, pinned file whenever the
     mirror's HEAD probe failed ("re-download to re-verify") — even though the
     file could be verified LOCALLY against the pin.
  2. The download-failure path then `unlink`ed the existing file, so a failed
     re-download DELETED the good model. With the unhosted ONNX mirror, every
     analyze wiped the staged heads.

These tests are pure-stdlib + analyzer import (CI-clean — no essentia / onnx /
network).
"""
from __future__ import annotations

import hashlib
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from vibechek import analyzer


def _sha(p: Path) -> str:
    h = hashlib.sha256()
    h.update(p.read_bytes())
    return h.hexdigest()


def _boom_head(*_a, **_k):
    raise urllib.error.URLError("mirror unreachable / 404")


def test_needs_download_keeps_valid_pinned_file_when_mirror_unreachable(tmp_path, monkeypatch) -> None:
    f = tmp_path / "danceability.onnx"
    f.write_bytes(b"x" * 200_000)  # size-sane (>100KB)
    pin = _sha(f)
    monkeypatch.setattr(urllib.request, "urlopen", _boom_head)
    # HEAD fails but the cached file matches its pin -> keep it, do NOT refetch.
    assert analyzer._needs_download(f, "http://nope.invalid/danceability.onnx", pin) is False


def test_needs_download_refetches_when_cached_fails_pin(tmp_path, monkeypatch) -> None:
    f = tmp_path / "danceability.onnx"
    f.write_bytes(b"x" * 200_000)
    monkeypatch.setattr(urllib.request, "urlopen", _boom_head)
    # HEAD fails AND the cached file's hash != pin -> a genuine refetch.
    assert analyzer._needs_download(f, "http://nope.invalid/danceability.onnx", "ab" * 32) is True


def test_failed_download_does_not_delete_existing_models(tmp_path, monkeypatch) -> None:
    """The headline destructive bug: a failed re-download must leave existing
    cached model files intact (the download streams to a `.partial`, so `dest`
    is a previously-good file)."""
    from vibechek.onnx_backend import BACKBONE_ONNX_FILENAME  # noqa: PLC0415

    md = tmp_path
    # The ONNX engine keeps its files in the dedicated <models>/onnx/ subdir
    # (analyzer._ONNX_SUBDIR), so seed the fixture THERE — seeding the parent
    # root would let the assertion pass trivially without exercising the path
    # download_models actually writes to.
    onnx_dir = md / analyzer._ONNX_SUBDIR
    onnx_dir.mkdir()
    (onnx_dir / BACKBONE_ONNX_FILENAME).write_bytes(b"b" * 200_000)
    for stem in analyzer._ONNX_HEAD_STEMS:
        (onnx_dir / f"{stem}.onnx").write_bytes(b"h" * 200_000)
        (onnx_dir / f"{stem}.json").write_text('{"classes": ["a", "b"]}', encoding="utf-8")
    before = sorted(p.name for p in onnx_dir.glob("*.onnx"))
    assert before, "fixture should have created .onnx files"

    # Force the download path for every file and make every download fail.
    monkeypatch.setattr(analyzer, "_needs_download", lambda *a, **k: True)

    def boom_dl(*_a, **_k):
        raise RuntimeError("All 1 mirror(s) failed (HTTP 404 — not hosted)")

    monkeypatch.setattr(analyzer, "_download_from_mirrors", boom_dl)
    monkeypatch.setattr(analyzer, "verify_model_sha256", lambda *a, **k: None)

    with pytest.raises(RuntimeError):  # it still reports the failures at the end
        analyzer.download_models(md, engine="onnx")

    after = sorted(p.name for p in onnx_dir.glob("*.onnx"))
    assert after == before, (
        "a failed re-download DELETED existing cached models — "
        f"before={before} after={after}"
    )


# --- One-click setup: bundled ONNX heads ship in the repo + stage cleanly. ---


def test_bundled_onnx_assets_present_in_repo() -> None:
    """The converted heads must ship inside the package (PyInstaller `datas`
    plus the source tree) so `setup_onnx_engine` works offline — no mirror."""
    from vibechek import onnx_backend  # noqa: PLC0415

    src = onnx_backend.bundled_onnx_assets_dir()
    assert src is not None, "bundled ONNX heads should ship in vibechek/onnx_assets"
    heads = sorted(p.name for p in src.glob("*.onnx"))
    assert heads, f"no bundled .onnx heads found in {src}"
    # The EffNet head set we convert (backbone is fetched separately, not bundled).
    assert "danceability.onnx" in heads


def test_stage_bundled_onnx_heads_copies_into_onnx_subdir(tmp_path) -> None:
    from vibechek import onnx_backend  # noqa: PLC0415

    res = onnx_backend.stage_bundled_onnx_heads(tmp_path)
    onnx_dir = tmp_path / "onnx"
    assert onnx_dir.is_dir(), "must stage into the models/onnx/ subdir"
    staged = sorted(p.name for p in onnx_dir.glob("*.onnx"))
    assert staged, "no heads staged"
    assert res["staged"], f"helper reported nothing staged: {res}"
    # Subdir keeps converted-head JSONs away from essentia's same-named .json.
    assert "danceability.onnx" in staged


def test_stage_bundled_onnx_heads_cleans_partial_leftovers(tmp_path) -> None:
    """A previously-aborted download leaves `*.partial` turds; staging must
    sweep them so a half-written file can't be mistaken for a real model."""
    from vibechek import onnx_backend  # noqa: PLC0415

    onnx_dir = tmp_path / "onnx"
    onnx_dir.mkdir()
    (onnx_dir / "danceability.onnx.partial").write_bytes(b"junk")
    onnx_backend.stage_bundled_onnx_heads(tmp_path)
    assert not list(onnx_dir.glob("*.partial")), "stale .partial files not cleaned"
