"""Regression: model downloads must never DESTROY a valid cached file.

Two coupled bugs (found driving the ONNX engine, whose head .onnx mirror isn't
hosted yet so it 404s):

  1. `_needs_download` re-fetched a locally-present, pinned file whenever the
     mirror's HEAD probe failed ("re-download to re-verify") — even though the
     file could be verified LOCALLY against the pin.
  2. The download-failure path then `unlink`ed the existing file, so a failed
     re-download DELETED the good model. With the unhosted ONNX mirror, every
     analyze wiped the staged heads.

These tests are pure-stdlib + model_download import (CI-clean — no essentia / onnx /
network).
"""
from __future__ import annotations

import hashlib
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from vibechek import model_download


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
    assert model_download._needs_download(f, "http://nope.invalid/danceability.onnx", pin) is False


def test_needs_download_refetches_when_cached_fails_pin(tmp_path, monkeypatch) -> None:
    f = tmp_path / "danceability.onnx"
    f.write_bytes(b"x" * 200_000)
    monkeypatch.setattr(urllib.request, "urlopen", _boom_head)
    # HEAD fails AND the cached file's hash != pin -> a genuine refetch.
    assert model_download._needs_download(f, "http://nope.invalid/danceability.onnx", "ab" * 32) is True


def test_failed_download_does_not_delete_existing_models(tmp_path, monkeypatch) -> None:
    """The headline destructive bug: a failed re-download must leave existing
    cached model files intact (the download streams to a `.partial`, so `dest`
    is a previously-good file)."""
    from vibechek.onnx_backend import BACKBONE_ONNX_FILENAME  # noqa: PLC0415

    md = tmp_path
    # The ONNX engine keeps its files in the dedicated <models>/onnx/ subdir
    # (model_download._ONNX_SUBDIR), so seed the fixture THERE — seeding the parent
    # root would let the assertion pass trivially without exercising the path
    # download_models actually writes to.
    onnx_dir = md / model_download._ONNX_SUBDIR
    onnx_dir.mkdir()
    (onnx_dir / BACKBONE_ONNX_FILENAME).write_bytes(b"b" * 200_000)
    for stem in model_download._ONNX_HEAD_STEMS:
        (onnx_dir / f"{stem}.onnx").write_bytes(b"h" * 200_000)
        (onnx_dir / f"{stem}.json").write_text('{"classes": ["a", "b"]}', encoding="utf-8")
    before = sorted(p.name for p in onnx_dir.glob("*.onnx"))
    assert before, "fixture should have created .onnx files"

    # Force the download path for every file and make every download fail.
    monkeypatch.setattr(model_download, "_needs_download", lambda *a, **k: True)

    def boom_dl(*_a, **_k):
        raise RuntimeError("All 1 mirror(s) failed (HTTP 404 — not hosted)")

    monkeypatch.setattr(model_download, "_download_from_mirrors", boom_dl)
    monkeypatch.setattr(model_download, "verify_model_sha256", lambda *a, **k: None)

    with pytest.raises(RuntimeError):  # it still reports the failures at the end
        model_download.download_models(md, engine="onnx")

    after = sorted(p.name for p in onnx_dir.glob("*.onnx"))
    assert after == before, (
        "a failed re-download DELETED existing cached models — "
        f"before={before} after={after}"
    )


def test_download_models_stages_bundled_heads_when_mirror_unreachable(tmp_path, monkeypatch) -> None:
    """A FRESH onnx/native install must get its classification heads from the
    BUNDLED assets (staged), NOT the mirror — so the one-click Windows native
    engine works on a clean box where the head .onnx mirror would 404. Without
    staging, a fresh install silently loses every genre/mood/vocal head."""
    def boom_dl(*_a, **_k):  # every network fetch fails — only staging can supply heads
        raise RuntimeError("mirror unreachable (404)")

    monkeypatch.setattr(model_download, "_download_from_mirrors", boom_dl)

    # The backbone .onnx is NOT bundled, so its fetch still fails → final raise.
    with pytest.raises(RuntimeError):
        model_download.download_models(tmp_path, engine="onnx")

    onnx_dir = tmp_path / model_download._ONNX_SUBDIR
    staged = sorted(p.name for p in onnx_dir.glob("*.onnx"))
    assert "danceability.onnx" in staged, (
        "bundled ONNX heads must be staged offline for a fresh native/onnx install"
    )
    # genre_discogs400.json carries the 400 class labels and is REQUIRED.
    assert (onnx_dir / "genre_discogs400.json").exists()


# --- Cancellation: a multi-GB download must stop mid-stream on cancel. ---


def test_do_one_download_aborts_mid_stream_on_cancel(tmp_path, monkeypatch) -> None:
    """Cancel during the byte loop raises CancelledError and removes the
    .partial (regression for the CLAP 2.2 GB checkpoint: before this, Cancel
    let the whole download run to completion behind the dialog)."""
    from vibechek import cancellation

    class _FakeResp:
        headers = {"Content-Length": "1000"}

        def __init__(self) -> None:
            self.reads = 0

        def read(self, _n: int) -> bytes:
            self.reads += 1
            if self.reads == 2:
                cancellation.cancel()  # flag flips AFTER the first chunk landed
            if self.reads >= 5:
                # Safety valve: if the cancellation check regresses, end the
                # stream so the test FAILS (truncated-size RuntimeError !=
                # CancelledError) instead of hanging the suite forever.
                return b""
            return b"x" * 100

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _FakeResp())
    dest = tmp_path / "music_clap.pt"

    cancellation.begin("clap-setup")
    try:
        with pytest.raises(cancellation.CancelledError):
            model_download._do_one_download(
                "http://example.invalid/ckpt", dest, on_progress=None, chunk_size=100,
            )
    finally:
        cancellation.end()

    assert not dest.exists()
    assert not dest.with_suffix(dest.suffix + ".partial").exists(), (
        "a cancelled download stranded its .partial"
    )


def test_download_from_mirrors_does_not_fail_over_on_cancel(monkeypatch, tmp_path) -> None:
    """CancelledError subclasses RuntimeError — without the explicit re-raise,
    the mirror loop would treat a user cancel as a mirror failure and start the
    SAME multi-GB download again from mirror 2."""
    from vibechek import cancellation

    attempts: list[str] = []

    def _cancelled_download(url, dest, label, on_progress=None, chunk_size=0, max_attempts=0):
        attempts.append(url)
        raise cancellation.CancelledError("cancelled by user")

    monkeypatch.setattr(model_download, "_download_with_progress", _cancelled_download)

    with pytest.raises(cancellation.CancelledError):
        model_download._download_from_mirrors(
            ["http://mirror-one.invalid/f", "http://mirror-two.invalid/f"],
            tmp_path / "f.bin", label="f",
        )
    assert attempts == ["http://mirror-one.invalid/f"], (
        f"cancel failed over to the next mirror: {attempts}"
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


def test_candidate_model_urls_flatten_github_release_bases(monkeypatch) -> None:
    """GitHub release assets are flat: `releases/download/<tag>/<subdir>/<f>`
    404s unconditionally, which made the advertised mirror failover fictional
    (the weekly smoke died whenever UPF timed out). Release bases must emit
    the flat form; directory-style bases keep the upstream layout."""
    monkeypatch.setattr(
        model_download, "MODEL_BASE_URLS",
        (
            "https://essentia.upf.edu/models",
            "https://github.com/captinjack99/Vibechek/releases/download/models-v1",
        ),
    )
    urls = model_download.candidate_model_urls(
        "feature-extractors/discogs-effnet", "discogs-effnet-bs64-1.pb"
    )
    assert urls == [
        "https://essentia.upf.edu/models/feature-extractors/discogs-effnet/"
        "discogs-effnet-bs64-1.pb",
        "https://github.com/captinjack99/Vibechek/releases/download/models-v1/"
        "discogs-effnet-bs64-1.pb",
    ]


def test_candidate_model_urls_user_override_keeps_directory_layout(monkeypatch) -> None:
    """A VIBECHEK_MODELS_URL self-hosted mirror serves the upstream tree."""
    monkeypatch.setattr(
        model_download, "MODEL_BASE_URLS", ("https://mirror.example/models",),
    )
    urls = model_download.candidate_model_urls("classification-heads/x", "x-1.pb")
    assert urls == ["https://mirror.example/models/classification-heads/x/x-1.pb"]


def test_backbone_onnx_sha256_is_pinned() -> None:
    """The ONNX backbone is fetched over the network and fed to onnxruntime —
    it must carry a content pin like every other downloaded model file."""
    pin = model_download.BACKBONE_ONNX_SHA256
    assert isinstance(pin, str) and len(pin) == 64
    int(pin, 16)  # valid hex


# ---------------------------------------------------------------------------
# The label JSON is output semantics, not decoration; and a
# cached file we can't READ must not unwind the whole loop out of a pool worker.
# ---------------------------------------------------------------------------


def test_metadata_pin_mismatch_is_fatal_and_drops_the_file(tmp_path, monkeypatch) -> None:
    """`classes` from the metadata JSON is the genre string written into the
    user's tags and the folder name organize moves the track into — a rotated
    label list silently relabels the whole library. It used to be a log.warning
    while the .pb next to it verified strictly."""
    monkeypatch.setattr(
        model_download, "MODELS",
        {"genre_discogs400": ("sub", "w.pb", "m.json")},
    )
    monkeypatch.setattr(model_download, "_needs_download", lambda *a, **k: False)

    weights = tmp_path / "genre_discogs400.pb"
    weights.write_bytes(b"w" * 200_000)
    meta = tmp_path / "genre_discogs400.json"
    meta.write_text('{"classes": ["Trance", "Techno"]}', encoding="utf-8")

    def verify(path, expected):
        if str(path).endswith(".json"):
            raise RuntimeError(
                f"Model file {Path(path).name} failed SHA256 check: expected a, got b"
            )

    monkeypatch.setattr(model_download, "verify_model_sha256", verify)

    with pytest.raises(RuntimeError) as ei:
        model_download.download_models(tmp_path, engine="essentia_tf")

    assert "genre_discogs400.json" in str(ei.value)
    assert not meta.exists(), (
        "a label file that fails its pin must not stay cached — _needs_download "
        "would keep re-fetching it while the descriptor served the bad labels"
    )


def test_metadata_matching_its_pin_still_supplies_the_classes(tmp_path, monkeypatch) -> None:
    """The strict path must not break the normal case."""
    monkeypatch.setattr(
        model_download, "MODELS",
        {"genre_discogs400": ("sub", "w.pb", "m.json")},
    )
    monkeypatch.setattr(model_download, "_needs_download", lambda *a, **k: False)
    monkeypatch.setattr(model_download, "verify_model_sha256", lambda p, e: None)

    (tmp_path / "genre_discogs400.pb").write_bytes(b"w" * 200_000)
    (tmp_path / "genre_discogs400.json").write_text(
        '{"classes": ["Techno", "Trance"]}', encoding="utf-8",
    )

    descriptors = model_download.download_models(tmp_path, engine="essentia_tf")
    assert descriptors["genre_discogs400"]["classes"] == ["Techno", "Trance"]


class _LockedPath:
    """A cached model file that exists but can't be stat'd (AV / cloud-sync
    client / flaky share holding an exclusive handle)."""

    suffix = ".pb"
    name = "effnet.pb"

    def exists(self) -> bool:
        return True

    def stat(self):
        raise PermissionError(13, "Permission denied", "effnet.pb")


def test_needs_download_does_not_escape_when_the_cached_file_cannot_be_stated() -> None:
    """`_needs_download` runs inside `_worker_init` -> `load_models`, OUTSIDE the
    caller's per-model try. An escaping OSError killed every pool worker at init;
    the pool respawned into the same error until the 300 s stall watchdog fired
    with a generic 'likely ran out of memory' message."""
    assert model_download._needs_download(
        _LockedPath(), "http://nope.invalid/effnet.pb", "ab" * 32,
    ) is True


def test_needs_download_does_not_escape_when_the_cached_file_cannot_be_read(
    tmp_path, monkeypatch,
) -> None:
    f = tmp_path / "effnet.pb"
    f.write_bytes(b"x" * 200_000)
    pin = _sha(f)

    def locked(path, expected):
        raise PermissionError(13, "Permission denied", str(path))

    monkeypatch.setattr(model_download, "verify_model_sha256", locked)
    # Unreadable is NOT "verified good": refetch, and let the caller's own
    # `except OSError` arm name the file if it is still unreadable.
    assert model_download._needs_download(
        f, "http://nope.invalid/effnet.pb", pin,
    ) is True
