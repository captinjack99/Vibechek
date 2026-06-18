"""Model catalog + downloader for the Essentia / ONNX ML models.

Extracted from analyzer.py (which re-exports these names for back-compat).
Owns the model catalog (`MODELS`, the SHA256 pins, the mirror base URLs, the
ONNX head layout) and the streaming, mirror-failover, hash-verified, and
cancellable downloader. Kept import-light (numpy/essentia-free) so the CLI,
doctor, and preflight can import the catalog without pulling the ML stack.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from vibechek.utils import ProgressCallback, report_progress

log = logging.getLogger(__name__)


# Where to fetch ML models from. essentia.upf.edu is the academic source
# maintained by UPF Barcelona's MTG — stable for years, but academic
# infrastructure is famously fragile. A URL change or outage there would
# break every Vibechek install until we shipped an update. So:
#
#   1. `VIBECHEK_MODELS_URL` env var wins (power users + self-hosters).
#   2. Otherwise we try the UPF source first, then a GitHub Release mirror
#      as a hot-swappable fallback. `_download_with_progress` walks the
#      tuple, trying each URL in turn — a single domain outage no longer
#      breaks the install.
#
# When publishing a new mirror, upload the .pb + .json files to
# https://github.com/captinjack99/Vibechek/releases/download/models-v1/ and bump
# the tag below.
_DEFAULT_MODEL_BASE_URLS = (
    "https://essentia.upf.edu/models",
    "https://github.com/captinjack99/Vibechek/releases/download/models-v1",
)
_USER_MODEL_BASE_URL = os.environ.get("VIBECHEK_MODELS_URL", "").strip() or None
MODEL_BASE_URLS: tuple[str, ...] = (
    (_USER_MODEL_BASE_URL,) if _USER_MODEL_BASE_URL else _DEFAULT_MODEL_BASE_URLS
)
# Kept for back-compat: callers still reference the singular MODEL_BASE_URL.
MODEL_BASE_URL = MODEL_BASE_URLS[0]

# Each entry: (subdirectory, weights filename, metadata filename)
MODELS: dict[str, tuple[str, str, str]] = {
    "effnet": (
        "feature-extractors/discogs-effnet",
        "discogs-effnet-bs64-1.pb",
        "discogs-effnet-bs64-1.json",
    ),
    "genre_discogs400": (
        "classification-heads/genre_discogs400",
        "genre_discogs400-discogs-effnet-1.pb",
        "genre_discogs400-discogs-effnet-1.json",
    ),
    "danceability": (
        "classification-heads/danceability",
        "danceability-discogs-effnet-1.pb",
        "danceability-discogs-effnet-1.json",
    ),
    "voice_instrumental": (
        "classification-heads/voice_instrumental",
        "voice_instrumental-discogs-effnet-1.pb",
        "voice_instrumental-discogs-effnet-1.json",
    ),
    "aggressive": (
        "classification-heads/mood_aggressive",
        "mood_aggressive-discogs-effnet-1.pb",
        "mood_aggressive-discogs-effnet-1.json",
    ),
    "happy": (
        "classification-heads/mood_happy",
        "mood_happy-discogs-effnet-1.pb",
        "mood_happy-discogs-effnet-1.json",
    ),
    "relaxed": (
        "classification-heads/mood_relaxed",
        "mood_relaxed-discogs-effnet-1.pb",
        "mood_relaxed-discogs-effnet-1.json",
    ),
    "sad": (
        "classification-heads/mood_sad",
        "mood_sad-discogs-effnet-1.pb",
        "mood_sad-discogs-effnet-1.json",
    ),
}


# Content-hash pinning for downloaded model files. A compromised or
# corrupted mirror could ship a TensorFlow .pb that pretends to be the
# Discogs-EffNet weights but actually emits adversarial classifications
# (or worse — `tf.io` deserialization is not safe against hostile graphs).
# We verify SHA256 after every download and re-fetch on mismatch.
#
# Schema: MODEL_SHA256[name][suffix] -> hex digest of the file.
# `suffix` is "pb" (weights) or "json" (metadata).
#
# Fields are POPULATED on the next release-build pass (`scripts/pin_model_
# hashes.py` downloads each .pb / .json from the canonical mirror, computes
# the SHA256, and rewrites this dict). Until then the dict is empty and
# `verify_model_sha256` no-ops — preserving the current "warn on size
# mismatch only" behaviour. When the dict is populated, EVERY download is
# strictly verified and a mismatch raises with a `vibechek verify-models`
# remediation hint.
MODEL_SHA256: dict[str, dict[str, str]] = {
    # "effnet": {
    #     "pb": "0000000000000000000000000000000000000000000000000000000000000000",
    #     "json": "0000000000000000000000000000000000000000000000000000000000000000",
    # },
    # ... one entry per model in MODELS ...
}

# Converted ONNX classification heads. Filenames match what
# scripts/convert_heads_to_onnx.py produces and what onnx_backend loads.
# essentia hosts only the .pb originals + the backbone .onnx, so the converted
# heads live on our own mirror release (the backbone already emits genre, so
# genre_discogs400 is optional — fetched for its class labels + as a fallback).
_ONNX_MODELS_RELEASE = "models-onnx-v1"
# ONNX models live in this dedicated subdir of the models dir, kept SEPARATE
# from the essentia `.pb` set. The converted-head class-label JSON
# (danceability.json, genre_discogs400.json, …) share filenames with essentia's
# `.pb` metadata JSON but carry different content; in a shared dir each engine's
# download clobbers the other's. The subdir makes the two engines independent.
_ONNX_SUBDIR = "onnx"
_ONNX_HEAD_STEMS: tuple[str, ...] = (
    "genre_discogs400",
    "danceability",
    "voice_instrumental",
    "mood_aggressive",
    "mood_happy",
    "mood_relaxed",
    "mood_sad",
)
# SHA256 of each converted head file ("<stem>.onnx" / "<stem>.json"), pinned
# from the models-onnx-v1 bundle (scripts/build_onnx_model_bundle.py). Every
# onnx-head download is strictly verified against these; a mismatch re-fetches
# then raises. Re-run the bundle script + repaste this dict when the heads are
# re-converted for a new model release.
MODEL_SHA256_ONNX: dict[str, str] = {
    "danceability.json": "e6da634e028c1a3ceb08233efd06f0fa25aa2f579667b80bcb3b88d568932092",
    "danceability.onnx": "b96e2e958efb0b3fb8e3613ed2b8ebbb8fc88ed84a7046e7b46137e6f445517e",
    "genre_discogs400.json": "680e374c8a4d9a38267b781e4786755144a59437dc4e971212de11d64053dba3",
    "genre_discogs400.onnx": "1c20d063de1ba9a5be9cc3216d2d4be4b7a6235bd854a8bf258e50143741583a",
    "mood_aggressive.json": "93acd1253818197611a23659858eb2b3956aa8b1dfbc8a3368c78041553f260e",
    "mood_aggressive.onnx": "3d6c810800eb91d2dc9825cea0614e72149a76757b07424e1f15b58ae2b06ad2",
    "mood_happy.json": "4564708c2e726aef1033286e39616d144523f8ed678e2dbe80a8f581d86d06b7",
    "mood_happy.onnx": "b864f6d56f90de4d4197c2650a8c166fe5494b2090069a27a6c56618a65298a7",
    "mood_relaxed.json": "c49d32ae8009003f6b91ef82fa9b04f3ef06ad0d251cbe8cb3616ff1e59c30bc",
    "mood_relaxed.onnx": "a22a8c9a72d6bda398766545d0a95a34fc011850ae967ac3d835f4b041e23b98",
    "mood_sad.json": "b97302cd69c0cc37a710f0ddbdb489b82ae95a0ed5abbf3f5a85a503b2d85a1c",
    "mood_sad.onnx": "3070f6be544a32a02d442a08e892ed34869ac90d176ee734aad61bd03d56c459",
    "voice_instrumental.json": "1ee201a221c5d74221b09017ba55154c9e3e59fa118340a95912d6d0d29b989d",
    "voice_instrumental.onnx": "dc75fd3674273a10a8897c1e25f29f134af132fb8a45a56379d93b7f6de7ce60",
}


def _onnx_head_bases() -> list[str]:
    """Mirror base URL(s) for the converted ONNX heads (flat filenames).

    Respects the VIBECHEK_MODELS_URL override (used for local mirrors / tests);
    otherwise points at our models-onnx GitHub release.
    """
    if _USER_MODEL_BASE_URL:
        return [_USER_MODEL_BASE_URL]
    return [
        f"https://github.com/captinjack99/Vibechek/releases/download/{_ONNX_MODELS_RELEASE}"
    ]


def verify_model_sha256(path: Path, expected: str | None) -> None:
    """Validate `path`'s SHA256 against `expected`. No-op when `expected` is None.

    Streams the file in 1 MiB chunks so we don't slurp the 100+ MB EffNet
    weights into memory all at once. Raises `RuntimeError` on mismatch with
    a hint at `vibechek verify-models` (the CLI subcommand that re-downloads
    every model from scratch — that's how the user recovers from a poisoned
    cache).

    Designed to be cheap to call from `download_models` even when the
    `MODEL_SHA256` table is empty: callers pass `expected=None` and we
    return immediately. This lets us wire the verification call site into
    `download_models` now, ahead of the release-build pass that populates
    the table.
    """
    if not expected:
        return
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    got = h.hexdigest()
    if got.lower() != expected.lower():
        raise RuntimeError(
            f"Model file {path.name} failed SHA256 check: "
            f"expected {expected[:12]}…, got {got[:12]}…. "
            f"Run `vibechek verify-models` to re-download from the canonical "
            f"mirror, or set VIBECHEK_MODELS_URL to a trusted source."
        )


def _expected_sha256(model_name: str, suffix: str) -> str | None:
    """Look up the pinned SHA256 for one model file, or None if not pinned."""
    return MODEL_SHA256.get(model_name, {}).get(suffix)


def download_models(
    model_dir: Path,
    on_progress: ProgressCallback | None = None,
    engine: str = "essentia_tf",
) -> dict[str, dict[str, Any]]:
    """Download missing model files. Returns descriptors keyed by model name.

    Streams each file with byte-level progress emitted to `on_progress` (the
    GUI shows a real progress bar instead of jumping in 1/8 steps). Validates
    that downloads actually succeeded — partial / wrong-size files are
    deleted and the model name added to `errors`. If ANY model failed, raises
    `RuntimeError` at the end so the caller can surface the failure rather
    than returning fake success.

    Idempotent: existing valid files are skipped. A file whose size doesn't
    match the server's Content-Length is treated as missing and re-fetched.

    `engine`: when "onnx", ALSO fetch the official EffNet backbone `.onnx`
    (`discogs-effnet-bsdynamic-1.onnx`) into `model_dir`. The `.pb` download is
    kept intact so the default essentia path is unaffected and so the heads' .pb
    (needed by the one-off conversion in scripts/convert_heads_to_onnx.py) are
    still fetched. essentia does NOT host the head `.onnx` files — those are
    produced by that conversion script, not by this download.
    """
    model_dir = Path(model_dir)
    model_dir.mkdir(parents=True, exist_ok=True)
    descriptors: dict[str, dict[str, Any]] = {}
    errors: list[str] = []

    # Two parts per model (weights + metadata) so the overall progress bar
    # has 2*N steps. We emit bytes-within-current-file as a fractional step
    # for smooth UX during big downloads.
    # The ONNX engine is TF-free and never loads the essentia `.pb` set, so skip
    # it entirely for engine="onnx" (no ~200 MB of unused TF weights, and no
    # essentia `.json` metadata to collide with the converted-head class labels).
    items = [] if engine == "onnx" else list(MODELS.items())
    total_steps = len(items) * 2 or 1

    def emit(step_idx: int, byte_progress: tuple[int, int] | None, label: str) -> None:
        """Translate (step, file-progress) into a 0..total_steps progress tick."""
        if byte_progress is not None:
            done_bytes, total_bytes = byte_progress
            inner = (done_bytes / total_bytes) if total_bytes > 0 else 0
            current = step_idx + inner
        else:
            current = step_idx + 1
        report_progress(on_progress, int(current * 100), total_steps * 100, label)

    def _candidate_urls(subdir: str, fname: str) -> list[str]:
        """Build the URL fallback chain for one file across all mirror bases."""
        return [f"{base}/{subdir}/{fname}" for base in MODEL_BASE_URLS]

    for i, (name, (subdir, weights_name, metadata_name)) in enumerate(items):
        weights_path = model_dir / f"{name}.pb"
        metadata_path = model_dir / f"{name}.json"

        # ---- weights ----
        weights_step = i * 2
        weights_urls = _candidate_urls(subdir, weights_name)
        if _needs_download(weights_path, weights_urls[0], _expected_sha256(name, "pb")):
            try:
                _download_from_mirrors(
                    weights_urls,
                    weights_path,
                    label=f"{name}.pb",
                    on_progress=lambda done, total, n=name, step=weights_step: emit(
                        step, (done, total), f"{n} weights ({_fmt_bytes(done)}/{_fmt_bytes(total)})"
                    ),
                )
            except Exception as e:  # noqa: BLE001
                log.error("Failed to download %s weights: %s", name, e)
                errors.append(f"{name}.pb: {e}")
                # Do NOT delete weights_path on failure: the download streams to
                # a `.partial` (cleaned by _do_one_download), so an existing
                # weights_path is a previously-good cached file — deleting it on
                # a transient/offline failure destroys valid local models.
                continue
        # Content-hash check (no-op when MODEL_SHA256[name]["pb"] is unset).
        # Verifies whether we just downloaded OR are reusing a cached file —
        # a poisoned mirror that served a bad .pb on a previous run would
        # otherwise stay cached forever.
        try:
            verify_model_sha256(weights_path, _expected_sha256(name, "pb"))
        except RuntimeError as e:
            log.error("SHA256 verification failed for %s: %s", name, e)
            errors.append(f"{name}.pb: {e}")
            weights_path.unlink(missing_ok=True)
            continue
        emit(weights_step, None, f"{name} weights ready")

        # ---- metadata ----
        metadata_step = i * 2 + 1
        metadata_urls = _candidate_urls(subdir, metadata_name)
        if _needs_download(metadata_path, metadata_urls[0], _expected_sha256(name, "json")):
            try:
                _download_from_mirrors(
                    metadata_urls,
                    metadata_path,
                    label=f"{name}.json",
                    on_progress=lambda done, total, n=name, step=metadata_step: emit(
                        step, (done, total), f"{n} metadata"
                    ),
                )
            except Exception as e:  # noqa: BLE001
                log.error("Failed to download %s metadata: %s", name, e)
                errors.append(f"{name}.json: {e}")
                # Keep any existing metadata file — a failed refetch must not
                # delete a good cached file (the `.partial` is cleaned downstream).
                # Still record the descriptor — the .pb may be usable without metadata
        # SHA256 for metadata is best-effort: metadata mismatch is far less
        # dangerous than weights mismatch (class labels can drift across model
        # versions without security impact), but a mismatch here likely means
        # version skew, so we warn loudly rather than fail.
        if metadata_path.exists():
            try:
                verify_model_sha256(metadata_path, _expected_sha256(name, "json"))
            except RuntimeError as e:
                log.warning("Metadata SHA256 mismatch for %s: %s", name, e)
        emit(metadata_step, None, f"{name} metadata ready")

        desc: dict[str, Any] = {
            "weights": str(weights_path),
            "metadata": str(metadata_path),
        }
        if metadata_path.exists():
            try:
                meta = json.loads(metadata_path.read_text(encoding="utf-8"))
                if "classes" in meta:
                    desc["classes"] = meta["classes"]
            except Exception as e:  # noqa: BLE001
                log.warning("Bad metadata for %s: %s", name, e)

        descriptors[name] = desc

    # ---- ONNX backbone (only when the onnx engine is selected) ----
    # The official EffNet backbone ONNX lives at the same essentia.upf.edu
    # subdir as its .pb. We fetch it alongside the .pb files (which the head
    # conversion script still needs); the head .onnx are produced by
    # scripts/convert_heads_to_onnx.py, not hosted upstream.
    if engine == "onnx":
        from vibechek.onnx_backend import BACKBONE_ONNX_FILENAME  # noqa: PLC0415

        # All ONNX files go in the dedicated subdir (see _ONNX_SUBDIR) so they
        # never collide with the essentia `.pb` set in the parent models dir.
        onnx_dir = model_dir / _ONNX_SUBDIR
        onnx_dir.mkdir(parents=True, exist_ok=True)
        onnx_path = onnx_dir / BACKBONE_ONNX_FILENAME
        onnx_urls = _candidate_urls(
            "feature-extractors/discogs-effnet", BACKBONE_ONNX_FILENAME
        )
        if _needs_download(onnx_path, onnx_urls[0], None):
            try:
                _download_from_mirrors(
                    onnx_urls,
                    onnx_path,
                    label=BACKBONE_ONNX_FILENAME,
                    on_progress=lambda done, total: report_progress(
                        on_progress, done, max(total, done),
                        f"effnet backbone ONNX ({_fmt_bytes(done)}/{_fmt_bytes(total)})",
                    ),
                )
            except Exception as e:  # noqa: BLE001
                log.error("Failed to download EffNet backbone ONNX: %s", e)
                errors.append(f"{BACKBONE_ONNX_FILENAME}: {e}")
                # Keep any existing backbone — never delete a good cached file on
                # a failed download (the `.partial` is cleaned downstream).
        descriptors["effnet_onnx"] = {"weights": str(onnx_path)}

        # ---- converted classification heads (.onnx + .json) ----
        # essentia hosts only the .pb originals, so these come from our own
        # models-onnx mirror. The backbone already emits genre, so the
        # genre_discogs400.onnx HEAD is optional — BUT genre_discogs400.json is
        # REQUIRED: it carries the 400 class labels, without which the engine
        # loads "ready" yet silently emits no genre. Non-genre head .onnx are
        # required; their tiny .json (class labels) are best-effort.
        head_bases = _onnx_head_bases()
        for stem in _ONNX_HEAD_STEMS:
            for suffix in ("onnx", "json"):
                fname = f"{stem}.{suffix}"
                dest = onnx_dir / fname
                urls = [f"{base}/{fname}" for base in head_bases]
                expected = MODEL_SHA256_ONNX.get(fname)
                required = (
                    (suffix == "onnx" and stem != "genre_discogs400")
                    or (suffix == "json" and stem == "genre_discogs400")
                )
                if _needs_download(dest, urls[0], expected):
                    try:
                        _download_from_mirrors(
                            urls, dest, label=fname,
                            on_progress=lambda done, total, n=fname: report_progress(
                                on_progress, done, max(total, done),
                                f"{n} ({_fmt_bytes(done)}/{_fmt_bytes(total)})",
                            ),
                        )
                    except Exception as e:  # noqa: BLE001
                        (log.error if required else log.warning)(
                            "Failed to download ONNX head %s: %s", fname, e
                        )
                        if required:
                            errors.append(f"{fname}: {e}")
                        # Keep an existing head: the download went to `.partial`
                        # (cleaned downstream). Deleting `dest` on a failed
                        # refetch was WIPING valid, locally-present ONNX heads
                        # whenever the (not-yet-hosted) mirror 404'd.
                        continue
                if dest.exists():
                    try:
                        verify_model_sha256(dest, expected)
                    except RuntimeError as e:
                        log.error("SHA256 mismatch for ONNX head %s: %s", fname, e)
                        if required:
                            errors.append(f"{fname}: {e}")
                        dest.unlink(missing_ok=True)
            descriptors[f"onnx_{stem}"] = {"weights": str(onnx_dir / f"{stem}.onnx")}

    if errors:
        raise RuntimeError(
            f"{len(errors)} model file(s) failed to download. "
            f"Check your network and retry. Errors: " + "; ".join(errors[:5])
        )

    return descriptors


def _fmt_bytes(n: int) -> str:
    """Smart byte formatter — KB for small files, MB for medium, GB for huge."""
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.0f} KB"
    if n < 1024 * 1024 * 1024:
        return f"{n / (1024 * 1024):.1f} MB"
    return f"{n / (1024 * 1024 * 1024):.2f} GB"


def _needs_download(path: Path, url: str, expected_sha256: str | None = None) -> bool:
    """True if `path` is missing OR clearly truncated relative to `url`.

    The HEAD probe (server `Content-Length`) is our primary "is the cached file
    complete?" signal. The size sanity check (>100KB for .pb, >200B for .json)
    is a fast first gate that rejects truncated downloads and HTML error pages
    a previous run may have written.

    *On HEAD failure (network error, DNS, server down) the completeness of the
    cached file is genuinely UNKNOWN* — we have no `Content-Length` to compare
    against. Audit fix (LOW): we no longer treat that "unknown" as "valid"
    unconditionally. The resolution depends on whether we have an out-of-band
    integrity check:

      * If a pinned SHA256 exists for this file, verify the CACHED file against
        it locally: keep it (return False) when it matches, refetch only on a
        genuine mismatch. (We must NOT re-download just to re-verify — that
        fails when the mirror is down / the ONNX heads aren't hosted yet, and
        the download-failure path must never delete an otherwise-good file.)
      * If no SHA is pinned (the current default — `MODEL_SHA256` is empty),
        keep the size-sane cached file rather than forcing a re-download we
        can't validate anyway; this preserves offline / flaky-network reuse and
        matches the prior behaviour, but is now an explicit, documented choice
        rather than a silent "HEAD failed → trust it".
    """
    if not path.exists():
        return True

    # Sanity check the local file size FIRST — fast and doesn't need network.
    # .json floor is tiny: converted ONNX head class-label JSON can be ~30 bytes
    # ({"classes":["sad","non_sad"]}); 200 wrongly rejected them. Weights (.pb /
    # .onnx) are always >>100KB. Integrity for pinned files is the SHA check.
    min_size = 16 if path.suffix == ".json" else 100_000
    local_size = path.stat().st_size
    if local_size < min_size:
        log.warning(
            "Local %s is %d bytes — too small to be a real model file (min %d). Refetching.",
            path.name, local_size, min_size,
        )
        return True

    # A pinned file's integrity is FULLY determined by its content hash — no
    # network probe is needed or wanted. Verify it locally: a match means the
    # cached file is sound, so keep it (works offline, when a mirror is down,
    # and for the not-yet-hosted ONNX heads). Only a genuine hash mismatch
    # refetches — and even then the download-failure path never deletes the
    # existing file. This is what stops every ONNX analyze from needlessly
    # re-fetching (and historically DELETING) valid, locally-present heads, and
    # avoids false "refetch" when a same-named file on a DIFFERENT mirror (e.g.
    # essentia's genre_discogs400.json vs our converted head's) has another size.
    if expected_sha256:
        try:
            verify_model_sha256(path, expected_sha256)
            return False
        except RuntimeError:
            log.warning("Cached %s fails its pinned SHA256 — refetching.", path.name)
            return True

    # Unpinned: fall back to a HEAD completeness probe (server Content-Length).
    try:
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req, timeout=10) as resp:
            expected = int(resp.headers.get("Content-Length") or 0)
    except Exception as e:  # noqa: BLE001
        # HEAD failed for an unpinned file → completeness unknown; keep the
        # size-sane cached file (a refetch we can't validate gains nothing and
        # breaks offline reuse).
        log.info(
            "HEAD probe failed for %s (%s) — keeping existing %d-byte local file "
            "(size-sane; no SHA pin to verify against).",
            url, e, local_size,
        )
        return False

    if expected > 0 and local_size != expected:
        log.warning(
            "Local %s is %d bytes but server says %d — refetching",
            path.name, local_size, expected,
        )
        return True
    return False


def _download_from_mirrors(
    urls: list[str],
    dest: Path,
    label: str,
    on_progress: Callable[[int, int], None] | None = None,
    chunk_size: int = 64 * 1024,
    max_attempts_per_mirror: int = 3,
) -> None:
    """Download `dest` from the first URL in `urls` that works.

    Each URL gets `max_attempts_per_mirror` tries with exponential backoff
    before we fall through to the next mirror. Net effect: a UPF outage
    hands off to the GitHub Release mirror, then to any
    user-configured VIBECHEK_MODELS_URL, without the user noticing.

    Raises RuntimeError listing every mirror's last error if all fail.
    """
    if not urls:
        raise ValueError("No mirror URLs provided")

    from vibechek import cancellation  # noqa: PLC0415

    mirror_errors: list[str] = []
    for url in urls:
        try:
            _download_with_progress(
                url, dest, label,
                on_progress=on_progress, chunk_size=chunk_size,
                max_attempts=max_attempts_per_mirror,
            )
            return  # success
        except cancellation.CancelledError:
            # A user cancel is not a mirror failure — do NOT fail over to the
            # next mirror (CancelledError subclasses RuntimeError, so without
            # this it would re-download from mirror 2 after a cancel).
            raise
        except RuntimeError as e:
            log.warning("Mirror %s failed for %s: %s", url, dest.name, e)
            mirror_errors.append(f"{url}: {e}")
            continue

    raise RuntimeError(
        f"All {len(urls)} mirror(s) failed for {dest.name}. "
        f"Errors:\n  " + "\n  ".join(mirror_errors)
    )


def _download_with_progress(
    url: str,
    dest: Path,
    label: str,
    on_progress: Callable[[int, int], None] | None = None,
    chunk_size: int = 64 * 1024,
    max_attempts: int = 3,
) -> None:
    """Stream `url` → `dest`, calling `on_progress(bytes_done, bytes_total)`.

    - Throttles progress emission to ~10/sec so we don't flood the JSON-RPC pipe.
    - Validates that the file matches the server's Content-Length when present.
    - Retries up to `max_attempts` times on transient network errors
      (urllib.error.URLError, socket timeouts, ConnectionResetError).
    - Sanity-checks the final size to catch HTML error pages masquerading as
      model files.
    - Honors HTTP_PROXY / HTTPS_PROXY env vars via urllib's default handler.
    """
    import time as _time
    import urllib.error

    last_err: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        if attempt > 1:
            backoff = min(2 ** (attempt - 1), 10)
            log.warning(
                "Retry %d/%d for %s after %ds (last error: %s)",
                attempt, max_attempts, dest.name, backoff, last_err,
            )
            _time.sleep(backoff)

        log.info("Downloading %s -> %s (attempt %d/%d)", url, dest.name, attempt, max_attempts)
        try:
            _do_one_download(url, dest, on_progress, chunk_size)
            return  # success
        except (urllib.error.URLError, ConnectionResetError, TimeoutError, OSError) as e:
            last_err = e
            # Network error: retry.
            continue
        except RuntimeError:
            # Truncated / too-small / wrong-size: these aren't transient, no retry.
            raise

    raise RuntimeError(
        f"Failed to download {dest.name} after {max_attempts} attempts. "
        f"Last error: {type(last_err).__name__}: {last_err}"
    ) from last_err


def _do_one_download(
    url: str,
    dest: Path,
    on_progress: Callable[[int, int], None] | None,
    chunk_size: int,
) -> None:
    """One attempt at downloading `url` to `dest`. Raises on failure."""
    import time as _time

    from vibechek import cancellation  # noqa: PLC0415

    with urllib.request.urlopen(url, timeout=30) as resp:
        total = int(resp.headers.get("Content-Length") or 0)
        bytes_done = 0
        last_emit = 0.0

        # Atomic write: temp file then rename. Avoids leaving a half-written
        # file in place if the network drops mid-download.
        tmp_dest = dest.with_suffix(dest.suffix + ".partial")
        try:
            with open(tmp_dest, "wb") as f:
                while True:
                    # A flag read per 64 KB chunk — effectively free, and it
                    # makes Cancel actually stop a multi-GB fetch (the CLAP
                    # checkpoint is 2.2 GB) instead of letting it run to
                    # completion behind a "cancelling…" dialog. The partial is
                    # unlinked by the except-cleanup below.
                    cancellation.check()
                    chunk = resp.read(chunk_size)
                    if not chunk:
                        break
                    f.write(chunk)
                    bytes_done += len(chunk)
                    now = _time.monotonic()
                    if on_progress and (now - last_emit) > 0.1:
                        try:
                            on_progress(bytes_done, total)
                        except Exception:  # noqa: BLE001
                            pass
                        last_emit = now

            # Final progress tick for the UI
            if on_progress:
                try:
                    on_progress(bytes_done, max(total, bytes_done))
                except Exception:  # noqa: BLE001
                    pass

            # Validate size
            if total > 0 and bytes_done != total:
                raise RuntimeError(
                    f"truncated download: got {bytes_done} bytes, expected {total}"
                )
            # Sanity check on the content — refuse anything implausibly small
            # for an Essentia model (smallest is ~514KB; smallest metadata ~1KB).
            min_size = 16 if dest.suffix == ".json" else 100_000  # tiny ONNX head class-label JSON
            if bytes_done < min_size:
                raise RuntimeError(
                    f"unexpectedly small file ({bytes_done} bytes) — likely an error page"
                )

            tmp_dest.replace(dest)
        except Exception:
            tmp_dest.unlink(missing_ok=True)
            raise
