"""ML analysis pipeline.

Wraps Essentia's pre-trained Discogs-EffNet models to classify each track on
genre, subgenre, energy, mood, timeslot, direction, vocal content, plus BPM
and key via Essentia's signal-processing extractors.

Essentia is loaded lazily inside `load_models()` and `analyze_track()` so the
rest of the package (CLI, tagger, organizer) remains importable on systems
where essentia-tensorflow isn't installed.

Source: port of `legacy/analyze_dj_tracks_v2.py`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import multiprocessing
import os
import re
import urllib.request
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from multiprocessing import cpu_count
from pathlib import Path
from typing import Any

from mutagen import File as MutagenFile  # noqa: N812 (mutagen's API)
from mutagen.flac import FLAC
from mutagen.id3 import ID3

from vibechek import cancellation
from vibechek.config import AnalysisConfig
from vibechek.filename import extract_from_filename
from vibechek.genres import GenreResult, get_best_genre
from vibechek.io import atomic_write_json
from vibechek.keys import key_to_camelot
from vibechek.resources import apply_gpu_preference
from vibechek.utils import (
    ProgressCallback,
    find_audio_files,
    report_progress,
)

log = logging.getLogger(__name__)

# Quiet TensorFlow when essentia eventually imports it
os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "3")

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

# Mood model class-index lookup: which output index represents the positive
# (named) class. Determined by Essentia model documentation.
_MOOD_INDEX = {"aggressive": 0, "happy": 0, "relaxed": 1, "sad": 1}

# Essentia KeyExtractor pitch profile. A shoot-out of every Essentia profile on
# the 72-track gold corpus (internal/bughunt/profile_shootout.py) ranked Shaath's
# profile highest for our electronic-music libraries — ahead of the EDM-tuned
# "edma" it replaced (66.7% vs 65.3% exact-Camelot single-read; the gap widens
# once segment-voting is layered on, see KEY_VOTE_SEGMENTS). Exposed as a module
# constant so it can be tuned without touching call sites.
KEY_PROFILE = "shaath"

# Key detection majority-votes across this many equal track segments rather than
# reading the whole file once. A single full-track read shows a systematic
# confusion on this corpus: major tracks get reported as their PARALLEL MINOR
# (same tonic, wrong mode — D major → D minor), 14:1 in that direction. Voting
# over thirds dilutes it (segments where the tonality is unambiguous outvote the
# ones that flip), halving those errors. Measured on the gold corpus this lifts
# exact-Camelot 65% → 71% and harmonically-mixable (exact+relative+adjacent)
# 69% → 78%, at ~no added cost — three third-length reads process the same total
# samples as one full read. Voting with the full read as an anchor was tested and
# was WORSE (the biased full read dominates the tally), so segments vote alone.
KEY_VOTE_SEGMENTS = 3
# A segment shorter than this (1 s at 44.1 kHz) is too brief for a stable read;
# tracks too short to yield KEY_VOTE_SEGMENTS such segments fall back to one read.
_MIN_KEY_SEGMENT_SAMPLES = 44100


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


# ---------------------------------------------------------------------------
# Result schema
# ---------------------------------------------------------------------------


@dataclass
class MLResult:
    """ML-derived attributes for a single track."""

    ml_genre: str | None = None
    ml_subgenre: str | None = None
    ml_genre_confidence: float | None = None
    ml_genre_raw_confidence: float | None = None
    ml_bpm: float | None = None
    ml_key: str | None = None
    ml_energy: int | None = None
    ml_mood: str | None = None
    ml_timeslot: str | None = None
    ml_direction: str | None = None
    ml_vocal: str | None = None
    # Raw voice probability (0..1) from the voice/instrumental model, stored so
    # the classification can be re-derived or re-tuned without re-running the
    # model. `ml_vocal` is the label derived from this via _classify_vocal.
    ml_vocal_score: float | None = None
    ml_danceability: float | None = None
    ml_mood_scores: dict[str, float] | None = None
    ml_error: str | None = None


@dataclass
class TrackAnalysis:
    """The full per-file record written to analysis.json.

    `existing_tags` and `ml_analysis` are stored as plain dicts here because
    the analyzer builds them dynamically. On the wire (and in TypeScript) they
    take their narrower structured forms — see `__ts_overrides__` below.
    """

    path: str
    filename: str
    extension: str
    size_mb: float
    filename_artist: str | None = None
    filename_title: str | None = None
    filename_bpm: int | None = None
    filename_key: str | None = None
    filename_mix: str | None = None
    existing_tags: dict[str, Any] = field(default_factory=dict)
    ml_analysis: dict[str, Any] | None = None  # MLResult as dict, or None
    error: str | None = None

    # Read by scripts/generate_ts_types.py: replace the Python-level dict types
    # with the structured TS interfaces the wire actually carries. ExistingTags
    # is hand-written in ui/src/types/index.ts (no Python dataclass yet); MLResult
    # is generated from this same module.
    __ts_overrides__ = {
        "existing_tags": "ExistingTags",
        "ml_analysis": "MLResult | null",
    }


# ---------------------------------------------------------------------------
# Model download / load
# ---------------------------------------------------------------------------


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


def _per_worker_mb(genre_classifier: str) -> int:
    """RAM budget per analysis worker, used to cap the worker count.

    The baseline ~800 MB covers the essentia/TF model weights + runtime. The
    CLAP genre classifier loads a ~2.2 GB fp32 checkpoint + the torch runtime
    INTO EVERY worker (load_models → _maybe_load_clap), so its per-worker
    footprint is ~3.5 GB — sizing CLAP runs off the 800 MB assumption put a
    32 GB box at 15 workers (~50 GB resident) and an OOM-killer storm whose
    only symptom was the 300 s stall-watchdog error.
    """
    return 3500 if genre_classifier == "clap" else 800


def _maybe_load_clap(loaded: dict[str, Any], genre_classifier: str, use_gpu: str) -> None:
    """When the CLAP genre classifier is selected, load the encoder + bundled kNN
    reference into the models dict (alongside the essentia/onnx models, so one
    worker does BPM/key AND the CLAP genre). Best-effort: if CLAP isn't installed
    we log and fall back to the engine's Discogs genre rather than failing analyze.
    """
    if genre_classifier != "clap":
        return
    try:
        from vibechek import clap_genre  # noqa: PLC0415

        loaded["clap_model"] = clap_genre.load_clap_model(use_gpu=use_gpu)
        loaded["clap_reference"] = clap_genre.load_reference()
        log.info("CLAP genre classifier loaded (%d reference vectors)",
                 loaded["clap_reference"].emb.shape[0])
    except Exception as e:  # noqa: BLE001 — never break analyze on a missing opt-in model
        log.warning("CLAP genre classifier unavailable, using Discogs genre: %s", e)


def load_models(
    model_dir: Path,
    use_gpu: str = "auto",
    engine: str = "essentia_tf",
    genre_classifier: str = "discogs",
) -> dict[str, Any]:
    """Instantiate the model callables for the analysis pipeline.

    `engine` selects the inference backend (from `AnalysisConfig.inference_engine`):
      * "essentia_tf" (default) — the essentia-tensorflow path below, UNCHANGED.
      * "onnx" (experimental) — delegates to `vibechek.onnx_backend.load_onnx_models`,
        which returns callables with the IDENTICAL signatures so every downstream
        caller (`analyze_audio_features` and its vocal/mood/genre logic) is
        byte-unchanged. Only this function knows which engine is in play.

    `genre_classifier` ("discogs" default | "clap") selects the GENRE source: the
    engine's Discogs-EffNet head, or the pure-audio CLAP+kNN student loaded
    alongside it (BPM/key/mood still come from the engine either way).

    `use_gpu` is forwarded to apply_gpu_preference BEFORE the essentia/tensorflow
    import — this is the only point where CUDA_VISIBLE_DEVICES can still affect
    TF's device enumeration. (For the onnx engine, GPU selection happens via the
    ONNX Runtime execution-provider chain instead.)
    """
    if engine == "onnx":
        # Ensure the backbone .onnx is present (heads come from the conversion
        # script), then hand off entirely to the ONNX backend.
        download_models(model_dir, engine="onnx")
        from vibechek.onnx_backend import load_onnx_models  # noqa: PLC0415

        # ONNX files live in the dedicated subdir (download_models put them there).
        loaded = load_onnx_models(model_dir / _ONNX_SUBDIR, use_gpu=use_gpu)
        _maybe_load_clap(loaded, genre_classifier, use_gpu)
        return loaded

    apply_gpu_preference(use_gpu)

    # *Critical for multi-worker GPU mode*: TF allocates ALL GPU memory at
    # init by default. With workers=19 and one 8 GB GPU, all 19 processes
    # fight for the same memory and CUDA OOM-kills them. Setting
    # TF_FORCE_GPU_ALLOW_GROWTH=true tells TF to grow allocations as needed
    # instead, letting many workers coexist on one GPU.
    #
    # Also set TF_CPP_MIN_LOG_LEVEL to silence the noisy startup logs that
    # would otherwise spam the JSON-RPC stdout pipe.
    os.environ.setdefault("TF_FORCE_GPU_ALLOW_GROWTH", "true")
    os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "2")

    try:
        import essentia
        from essentia.standard import (
            TensorflowPredict2D,
            TensorflowPredictEffnetDiscogs,
        )
    except ImportError as e:
        raise RuntimeError(
            "essentia-tensorflow is not installed. Install with: "
            "pip install 'vibechek[ml]' (Linux/macOS) — see docs/ for Windows."
        ) from e

    essentia.log.infoActive = False
    essentia.log.warningActive = False

    descriptors = download_models(model_dir)
    loaded: dict[str, Any] = {}

    if "effnet" in descriptors:
        loaded["effnet"] = TensorflowPredictEffnetDiscogs(
            graphFilename=descriptors["effnet"]["weights"],
            output="PartitionedCall:1",
        )

    if "genre_discogs400" in descriptors:
        loaded["genre"] = TensorflowPredict2D(
            graphFilename=descriptors["genre_discogs400"]["weights"],
            input="serving_default_model_Placeholder",
            output="PartitionedCall:0",
        )
        loaded["genre_classes"] = descriptors["genre_discogs400"].get("classes", [])

    # Other heads use varying node names — try the patterns we've seen
    node_patterns = [
        ("serving_default_model_Placeholder", "PartitionedCall:0"),
        ("model/Placeholder", "model/Softmax"),
        ("model/Placeholder", "model/Sigmoid"),
        ("Placeholder", "Softmax"),
        ("Placeholder", "Sigmoid"),
    ]

    def _try_load(name: str, weights: str) -> Any | None:
        for input_node, output_node in node_patterns:
            try:
                return TensorflowPredict2D(
                    graphFilename=weights,
                    input=input_node,
                    output=output_node,
                )
            except RuntimeError:
                continue
        log.warning("Could not load model %s with any known node pattern", name)
        return None

    for name in ("danceability", "voice_instrumental", "aggressive", "happy", "relaxed", "sad"):
        if name in descriptors:
            model = _try_load(name, descriptors[name]["weights"])
            if model is not None:
                loaded[name] = model
                # Stash the model's class label order so downstream column
                # selection resolves by NAME rather than a hardcoded index.
                # A re-ordered model release would otherwise silently invert
                # labels (e.g. voice↔instrumental). Falls back to index when
                # metadata is absent (see `_class_index`).
                classes = descriptors[name].get("classes")
                if classes:
                    loaded[f"{name}_classes"] = classes

    _maybe_load_clap(loaded, genre_classifier, use_gpu)
    return loaded


# ---------------------------------------------------------------------------
# Audio feature extraction
# ---------------------------------------------------------------------------


# Voice/instrumental classification cutoffs (voice probability 0..1).
#
# Recalibrated after real-world misclassification: the model rates the
# prominent melodic leads in instrumental dance tracks as voice-like, so genume
# instrumentals land ~0.64-0.69 (Robert Miles "Children" 0.69, Eric Prydz
# "Pjanoo" 0.64) while pure instrumentals sit at 0.06-0.22 and true vocals
# cluster at 0.76+. The old 0.6 "Vocal" cutoff therefore mislabelled those
# instrumentals as "Vocal". New bands:
#   < 0.72           → Instrumental  (covers the melodic-hook instrumentals)
#   0.72 .. 0.88     → Light Vocal   (ambiguous / sparse vocals)
#   >= 0.88          → Vocal         (sustained lead vocals)
# 0.72 (not 0.70) so Robert Miles "Children" (~0.70-0.71 depending on run)
# lands as Instrumental rather than straddling the boundary. Both cutoffs are
# configurable per-field via TaggingConfig so a user can tune them without code
# changes (and re-derive from the stored ml_vocal_score).
VOCAL_INSTRUMENTAL_MAX = 0.72
VOCAL_FULL_MIN = 0.88


def _class_index(
    classes: list[str] | None,
    label: str,
    fallback: int,
) -> int:
    """Resolve the output-vector index for `label` from a model's class list.

    Matches case-insensitively and tolerates substring labels (Essentia heads
    name classes "voice"/"instrumental", "danceable"/"not_danceable", etc.).
    Returns `fallback` when `classes` is missing or the label isn't found, so a
    model whose metadata we couldn't read still behaves exactly as before.
    """
    if classes:
        target = label.lower()
        for i, c in enumerate(classes):
            if isinstance(c, str) and target in c.lower():
                return i
    return fallback


def _classify_vocal(
    score: float,
    instrumental_max: float = VOCAL_INSTRUMENTAL_MAX,
    full_min: float = VOCAL_FULL_MIN,
) -> str:
    if score < instrumental_max:
        return "Instrumental"
    if score < full_min:
        return "Light Vocal"
    return "Vocal"


def _classify_mood(brightness: float) -> str:
    if brightness < 0.4:
        return "Dark"
    if brightness > 0.6:
        return "Bright"
    return "Neutral"


# Minimum start→end delta in the per-frame aggressive (energy proxy) score
# before we call a track "Up"/"Down" rather than "Steady".
#
# Empirically validated against 40 real DJ tracks (internal/bughunt/
# direction_timeslot_probe.py, 2026-06-17): the signed first-third→last-third
# delta has mean +0.027 / std 0.096 / |Δ| median 0.06, and ±0.08 yields a
# 70/25/5 Steady/Up/Down split — non-degenerate (the pre-fix column bug was
# 100% Steady) and intentionally PRECISION-LEANING (it sits just above the |Δ|
# median, so only a clear trend earns a direction; a wrong Up/Down on a
# secondary field is worse than a conservative Steady). Lowering to 0.06 would
# flag ~50% of tracks directional — over-eager on marginal ±0.05-0.08 wobble,
# with no ground truth to justify it. Don't retune without re-running the probe.
DIRECTION_DELTA = 0.08


def _classify_direction(
    aggressive_raw: Any,
    agg_idx: int = _MOOD_INDEX["aggressive"],
    delta: float = DIRECTION_DELTA,
) -> str:
    """Classify a track's energy *direction* from the per-frame aggressive head.

    `aggressive_raw` is the raw, pre-mean output of the mood_aggressive model:
    shape ``(frames, 2)`` (a 2-class softmax per frame, columns = [aggressive,
    not-aggressive]). We compare the mean of the AGGRESSIVE column over the
    first third of the track against the last third.

    Historical bug (audit HIGH): the old code did ``np.mean(aggressive_raw[:third])``
    with no column selection, averaging BOTH softmax columns of the slice. Since
    each row sums to ~1.0, that mean was ~0.5 for every slice, so start≈end and
    EVERY track collapsed to "Steady" — a silently-dead feature still written to
    files. We now slice the aggressive column (`[:, agg_idx]`) before averaging.

    Returns "Up" / "Down" / "Steady". Defensive against short clips, 1-D inputs
    (already a column), and anything malformed (returns "Steady").
    """
    try:
        import numpy as np  # noqa: PLC0415 (lazy: keeps module import essentia-free)

        arr = np.asarray(aggressive_raw, dtype=float)
        if arr.ndim >= 2 and arr.shape[1] > agg_idx:
            col = arr[:, agg_idx]
        else:
            # 1-D fallback: treat the input as the energy curve directly.
            col = arr.reshape(-1)
        third = len(col) // 3
        if third <= 0:
            return "Steady"
        start_e = float(np.mean(col[:third]))
        end_e = float(np.mean(col[-third:]))
        diff = end_e - start_e
        if diff > delta:
            return "Up"
        if diff < -delta:
            return "Down"
        return "Steady"
    except Exception:  # noqa: BLE001
        return "Steady"


# DJ-plausible tempo band. RhythmExtractor2013 is solid but prone to the
# classic octave errors DJs hit constantly: a 140-BPM techno track detected as
# 70, or 174 drum-and-bass detected as 87 (and vice-versa). Real dance tempos
# overwhelmingly live in [70, 200); we fold detected values by halving/doubling
# into that band, then — when the filename advertises a BPM — snap to whichever
# octave matches the filename, since DJs name files deliberately.
BPM_BAND_MIN = 70.0
BPM_BAND_MAX = 200.0  # exclusive upper edge of the canonical band


def _snap_bpm_octave(
    detected_bpm: float | None,
    filename_bpm: int | None = None,
    band_min: float = BPM_BAND_MIN,
    band_max: float = BPM_BAND_MAX,
) -> float | None:
    """Fold a detected BPM into a DJ-plausible band and reconcile with the
    filename-advertised BPM when present. Pure + side-effect free.

    1. Fold the raw value by ×2 / ÷2 until it lands in ``[band_min, band_max)``
       (canonicalises octave errors like 70→140 or 348→87).
    2. If ``filename_bpm`` is given, compare the folded value against the
       filename value AND its ÷2 / ×2 octaves; if the half/double octave is a
       closer match to the filename than the folded value, prefer it. This
       fixes the case where the detector locked onto the wrong octave but the
       filename tells us the DJ's intended tempo (e.g. detected 87, filename
       174 → return 174).

    Returns the reconciled BPM (rounded to 1 dp) or None when no detection.
    """
    if detected_bpm is None:
        return None
    try:
        bpm = float(detected_bpm)
    except (TypeError, ValueError):
        return None
    if bpm <= 0:
        return None

    # Step 1: fold into the canonical band.
    while bpm < band_min:
        bpm *= 2.0
    while bpm >= band_max:
        bpm /= 2.0

    # Step 2: reconcile against the filename BPM, considering both octaves.
    if filename_bpm and filename_bpm > 0:
        target = float(filename_bpm)
        candidates = (bpm, bpm * 2.0, bpm / 2.0)
        best = min(candidates, key=lambda c: abs(c - target))
        # Only accept the octave-shifted candidate if it's meaningfully closer
        # to the filename than the folded value (guards against noise nudging us
        # off a correct detection when the filename itself is half/double).
        if abs(best - target) + 1e-9 < abs(bpm - target):
            bpm = best

    return round(bpm, 1)


def _pick_timeslot(
    genre: str | None,
    subgenre: str | None,
    energy: int,
    bpm: float | None,
) -> str:
    """Map (genre, subgenre, energy, BPM) → DJ set timeslot label.

    Note: ``genre`` is the DJ-friendly PARENT genre (e.g. "Techno", "House")
    while ``subgenre`` carries the finer Discogs style (e.g. "Hard Techno",
    "Deep House"). The peak/chill special-cases below therefore have to match
    the SUBGENRE for styles like Hard Techno — matching them against the parent
    ``genre`` never fires (the parent arrives as "Techno"/"House").
    """
    # Chill / downtempo material opens or closes a set. "Ambient"/"Downtempo"
    # are producible parent genres; "Trip Hop" arrives as the subgenre under
    # the "Downtempo" parent. ("Chillout" is not a producible genre at all and
    # was dead here.)
    if genre in ("Ambient", "Downtempo") or subgenre in ("Trip Hop",):
        return "Opener" if energy <= 2 else "Afterhours"
    # Peak-time material. "Hardcore" is a producible parent; "Gabber"/
    # "Hardstyle" arrive as subgenres under it and "Hard Techno" as a subgenre
    # under the "Techno" parent — so the hard styles must be matched on the
    # subgenre, not the parent genre.
    if genre in ("Hardcore",) or subgenre in ("Gabber", "Hardstyle", "Hard Techno"):
        return "Peak"

    if energy <= 1:
        slot = "Opener"
    elif energy <= 2:
        slot = "Opener"
    elif energy <= 3:
        slot = "Warm-Up"
    else:
        slot = "Peak"

    # BPM extremes shift the timeslot
    if bpm is not None:
        if bpm < 110 and slot == "Peak":
            slot = "Warm-Up"
        elif bpm > 145 and slot in ("Opener", "Warm-Up") and energy >= 3:
            slot = "Peak"

    return slot


def _vote_key(key_extractor: Callable[[Any], Any], audio: Any) -> str | None:
    """Majority-vote the musical key across equal segments of ``audio``.

    ``key_extractor`` is a constructed Essentia ``KeyExtractor`` (or any callable
    returning ``(key, scale, strength)``). Splitting the track into
    ``KEY_VOTE_SEGMENTS`` parts and voting suppresses the single-read parallel-key
    (major↔minor) confusion documented at KEY_VOTE_SEGMENTS. Ties resolve to the
    earliest segment (``Counter.most_common`` keeps insertion order). Returns a
    Camelot string, or None when no read succeeds / parses.
    """
    reads: list[tuple[str, str]] = []
    n = len(audio)
    if n >= KEY_VOTE_SEGMENTS * _MIN_KEY_SEGMENT_SAMPLES:
        for i in range(KEY_VOTE_SEGMENTS):
            segment = audio[i * n // KEY_VOTE_SEGMENTS:(i + 1) * n // KEY_VOTE_SEGMENTS]
            try:
                key, scale, _strength = key_extractor(segment)
            except Exception:  # noqa: BLE001 — one bad segment must not sink the vote
                continue
            reads.append((key, scale))
    if not reads:
        # Too short to segment, or every segment read failed: one full read.
        key, scale, _strength = key_extractor(audio)
        reads.append((key, scale))
    winner = Counter(reads).most_common(1)[0][0]
    return key_to_camelot(f"{winner[0]} {winner[1]}")


def analyze_audio_features(filepath: Path, models: dict[str, Any]) -> MLResult:
    """Run the full Essentia analysis on a single track."""
    try:
        import numpy as np
        from essentia.standard import KeyExtractor, MonoLoader, RhythmExtractor2013
    except ImportError as e:
        raise RuntimeError("essentia-tensorflow not installed") from e

    result = MLResult()

    try:
        audio_16k = MonoLoader(filename=str(filepath), sampleRate=16000, resampleQuality=4)()
        audio_44k = MonoLoader(filename=str(filepath), sampleRate=44100, resampleQuality=4)()
    except Exception as e:  # noqa: BLE001
        result.ml_error = f"Could not decode audio: {e}"
        return result

    if "effnet" not in models:
        result.ml_error = "EffNet embedding model not loaded"
        return result

    # EffNet embedding can blow up on malformed audio (0-byte files, truncated
    # FLAC headers, corrupted MP3 sync bytes) — Essentia surfaces these as a
    # bare RuntimeError from native code that takes the whole worker down. We
    # bail early on failure so the parent analysis still records BPM/key from
    # the rhythm/key extractors run by the non-ML pass, instead of losing the
    # entire track to an unhandled exception in the worker pool.
    try:
        embeddings = models["effnet"](audio_16k)
    except Exception as e:  # noqa: BLE001
        result.ml_error = f"EffNet embedding failed: {e}"
        return result

    # ---------- Genre ----------
    # Pure-audio CLAP student (opt-in): a CLAP embedding matched by kNN against
    # the bundled reference library. ~2x the Discogs head on pure audio, and
    # unlike a tag it works on untagged tracks. Falls back to Discogs on failure.
    clap_done = False
    if "clap_model" in models and "clap_reference" in models:
        try:
            from vibechek import clap_genre  # noqa: PLC0415
            from vibechek.genres import split_tag_genre  # noqa: PLC0415

            audio_48k = MonoLoader(filename=str(filepath), sampleRate=48000, resampleQuality=4)()
            emb = clap_genre.embed_audio(models["clap_model"], audio_48k)
            # The 48 kHz buffer is the largest of the three decodes (~50% of
            # peak per-worker RAM on long mixes) and only the 3×20 s segments
            # inside embed_audio were needed — release it before inference.
            del audio_48k
            shares = clap_genre.knn_vote_shares(emb, models["clap_reference"])
            g = max(shares, key=shares.__getitem__) if shares else ""
            if g and g != "Unknown":
                parent, sub = split_tag_genre(g)
                # Confidence semantics must match the Discogs head's scale: the
                # tagger's genre gate (0.85) and the reconcile override floor
                # (0.90) were tuned against genres.get_best_genre's FAMILY-SUM
                # confidence. The kNN's raw top-1 vote share runs far lower
                # (mass splits across sibling subgenres), so an uncalibrated
                # share would leave most CLAP reads below the write gate —
                # defeating its headline use (untagged tracks). Family share =
                # the summed vote of every neighbour in the winner's family.
                fam_conf = sum(
                    s for lab, s in shares.items()
                    if split_tag_genre(lab)[0] == parent
                )
                result.ml_genre = parent
                result.ml_subgenre = sub
                result.ml_genre_confidence = round(min(fam_conf, 1.0), 3)
                result.ml_genre_raw_confidence = round(float(shares[g]), 3)
                clap_done = True
        except Exception as e:  # noqa: BLE001
            log.warning("CLAP genre failed for %s (falling back to Discogs): %s", filepath.name, e)

    if not clap_done and "genre" in models and "genre_classes" in models:
        try:
            preds = models["genre"](embeddings)
            avg = np.mean(preds, axis=0)
            genre_result: GenreResult = get_best_genre(avg.tolist(), models["genre_classes"])
            result.ml_genre = genre_result.genre
            result.ml_subgenre = genre_result.subgenre
            result.ml_genre_confidence = genre_result.confidence
            result.ml_genre_raw_confidence = genre_result.raw_confidence
        except Exception as e:  # noqa: BLE001
            log.warning("Genre classification failed for %s: %s", filepath.name, e)

    # ---------- BPM ----------
    try:
        bpm, *_ = RhythmExtractor2013(method="multifeature")(audio_44k)
        result.ml_bpm = round(float(bpm), 1)
    except Exception as e:  # noqa: BLE001
        log.debug("BPM detection failed for %s: %s", filepath.name, e)

    # ---------- Key ----------
    try:
        result.ml_key = _vote_key(KeyExtractor(profileType=KEY_PROFILE), audio_44k)
    except Exception as e:  # noqa: BLE001
        log.debug("Key detection failed for %s: %s", filepath.name, e)

    # ---------- Danceability ----------
    if "danceability" in models:
        try:
            pred = np.mean(models["danceability"](embeddings), axis=0)
            danceability = float(pred[0]) if len(pred) > 1 else float(np.mean(pred))
            result.ml_danceability = round(danceability, 3)
        except Exception as e:  # noqa: BLE001
            log.debug("Danceability failed for %s: %s", filepath.name, e)

    # ---------- Voice / instrumental ----------
    if "voice_instrumental" in models:
        try:
            pred = np.mean(models["voice_instrumental"](embeddings), axis=0)
            # Resolve the "voice" column by label NAME from the model's class
            # metadata, falling back to index 1 (the historical hardcode) when
            # metadata is unavailable. A re-ordered model release would otherwise
            # silently invert every vocal label, and that score is written to
            # files. Same hardening would apply to danceability/moods if their
            # heads were ever re-ordered (those use _MOOD_INDEX / index 0).
            voice_idx = _class_index(
                models.get("voice_instrumental_classes"), "voice", fallback=1
            )
            voice_score = float(pred[voice_idx]) if len(pred) > 1 else float(pred[0])
            result.ml_vocal_score = round(voice_score, 3)
            result.ml_vocal = _classify_vocal(voice_score)
        except Exception as e:  # noqa: BLE001
            log.debug("Vocal detection failed for %s: %s", filepath.name, e)
            result.ml_vocal = "Unknown"

    # ---------- Mood / energy ----------
    mood_scores: dict[str, float] = {}
    # Cache the raw (pre-mean) aggressive prediction so the Direction block
    # below can slice it into thirds WITHOUT re-running the model. The
    # aggressive head is the most expensive inference per track; calling it
    # twice (once here, once for direction) doubled that cost across the whole
    # library for no benefit.
    aggressive_raw = None
    for mood in ("aggressive", "happy", "relaxed", "sad"):
        if mood not in models:
            continue
        try:
            raw = models[mood](embeddings)
            if mood == "aggressive":
                aggressive_raw = raw
            pred = np.mean(raw, axis=0)
            idx = _MOOD_INDEX[mood]
            mood_scores[mood] = float(pred[idx]) if len(pred) > 1 else float(pred)
        except Exception as e:  # noqa: BLE001
            log.debug("Mood %s failed for %s: %s", mood, filepath.name, e)

    # Need at least 2 of the 4 mood models to land before trusting the
    # blended energy/brightness math. Old behaviour: any non-empty
    # `mood_scores` triggered the blend, with `.get(..., 0.5)` defaults for
    # missing models — so a track where 3 of 4 mood models silently failed
    # got energy ~= int(round((0.5*0.35 + 0.5*0.35 + 0.5*0.15 + 0.5*0.15)*5))
    # = ~3 ("medium energy") for every track, even when the genre would
    # otherwise have steered it to 1 or 5. That's not a fallback, that's
    # fabrication. We now require ≥2 real scores; below that we drop to the
    # genre-table fallback below — at least the user sees that as "default
    # by genre" instead of a silently-confident lie.
    if len(mood_scores) >= 2:
        aggressive = mood_scores.get("aggressive", 0.5)
        relaxed = mood_scores.get("relaxed", 0.5)
        happy = mood_scores.get("happy", 0.5)
        sad = mood_scores.get("sad", 0.5)

        energy_raw = (
            aggressive * 0.35
            + (1 - relaxed) * 0.35
            + happy * 0.15
            + (1 - sad) * 0.15
        )
        result.ml_energy = max(0, min(5, int(round(energy_raw * 5))))

        brightness = happy * 0.4 + (1 - sad) * 0.3 + (1 - aggressive) * 0.3
        result.ml_mood = _classify_mood(brightness)

        result.ml_mood_scores = {k: round(v, 3) for k, v in mood_scores.items()}
    else:
        # Genre-based fallback when mood models failed.
        # `ml_genre` is the DJ-friendly PARENT genre; the finer Discogs styles
        # (Hard Techno, Deep House, …) live in `ml_subgenre`. Match parent
        # buckets against `genre` and subgenre-level styles against `subgenre`
        # so the special-cases actually fire ("Hard Techno"/"Deep House" never
        # arrive as the parent genre).
        genre = result.ml_genre or ""
        subgenre = result.ml_subgenre or ""
        if (
            genre in ("Techno", "Industrial", "Hardcore")
            or subgenre in ("Hard Techno", "EBM", "Gabber")
        ):
            result.ml_energy, result.ml_mood = 4, "Dark"
        elif (
            genre in ("Ambient", "Downtempo")
            or subgenre in ("Deep House",)
        ):
            result.ml_energy, result.ml_mood = 2, "Neutral"
        elif (
            genre in ("Trance",)
            or subgenre in ("Psy-Trance", "Happy Hardcore", "Eurodance")
        ):
            result.ml_energy, result.ml_mood = 4, "Bright"
        else:
            result.ml_energy, result.ml_mood = 3, "Neutral"

    # Use an explicit None check, not `or 3`: energy 0 is a legitimate computed
    # value (calmest tracks). `0 or 3` would silently treat those as medium
    # energy and assign the wrong timeslot.
    result.ml_timeslot = _pick_timeslot(
        result.ml_genre,
        result.ml_subgenre,
        result.ml_energy if result.ml_energy is not None else 3,
        result.ml_bpm,
    )

    # ---------- Direction (energy curve over the track) ----------
    # Reuse the aggressive prediction computed in the mood loop above instead
    # of re-running the model (the array is per-frame; the helper slices it
    # into thirds and compares the AGGRESSIVE column start-vs-end).
    if aggressive_raw is not None:
        result.ml_direction = _classify_direction(aggressive_raw)
    else:
        result.ml_direction = "Steady"

    return result


# ---------------------------------------------------------------------------
# Per-file metadata + ML
# ---------------------------------------------------------------------------


def _read_existing_tags(filepath: Path) -> dict[str, Any]:
    """Return a compact dict of existing tags for diff/comparison purposes."""
    tags: dict[str, Any] = {
        "artist": None, "title": None, "album": None, "genre": None,
        "bpm": None, "key": None, "subgenre": None,
        "energy": None, "mood": None, "timeslot": None, "direction": None, "vocal": None,
    }

    try:
        audio = MutagenFile(filepath, easy=True)
        if audio and audio.tags:
            for field_name, candidate_keys in (
                ("artist", ("artist", "albumartist")),
                ("title", ("title",)),
                ("album", ("album",)),
                ("genre", ("genre",)),
            ):
                for key in candidate_keys:
                    val = audio.tags.get(key)
                    if val:
                        tags[field_name] = val[0] if isinstance(val, list) else str(val)
                        break

            for key in ("bpm", "TBPM"):
                val = audio.tags.get(key)
                if val:
                    raw = val[0] if isinstance(val, list) else val
                    try:
                        tags["bpm"] = int(float(str(raw)))
                    except ValueError:
                        pass
                    break

            for key in ("initialkey", "key", "TKEY"):
                val = audio.tags.get(key)
                if val:
                    tags["key"] = val[0] if isinstance(val, list) else str(val)
                    break

        ext = filepath.suffix.lower()
        if ext == ".mp3":
            try:
                id3 = ID3(filepath)
                for txxx in id3.getall("TXXX"):
                    desc = txxx.desc.upper()
                    if desc == "ENERGY":
                        try:
                            tags["energy"] = int(txxx.text[0])
                        except (TypeError, ValueError):
                            tags["energy"] = txxx.text[0]
                    elif desc in ("MOOD", "TIMESLOT", "DIRECTION", "VOCAL"):
                        tags[desc.lower()] = txxx.text[0]
                tit1 = id3.get("TIT1")
                if tit1:
                    tags["subgenre"] = tit1.text[0]
            except Exception as e:  # noqa: BLE001
                log.debug("ID3 read failed for %s: %s", filepath, e)
        elif ext == ".flac":
            try:
                flac = FLAC(filepath)
                for k in ("energy", "mood", "timeslot", "direction", "vocal"):
                    val = flac.get(k.upper(), [None])[0]
                    if val is not None:
                        if k == "energy":
                            try:
                                tags[k] = int(val)
                            except (TypeError, ValueError):
                                tags[k] = val
                        else:
                            tags[k] = val
                tags["subgenre"] = (
                    flac.get("CONTENTGROUP", [None])[0]
                    or flac.get("GROUPING", [None])[0]
                )
            except Exception as e:  # noqa: BLE001
                log.debug("FLAC read failed for %s: %s", filepath, e)
    except Exception as e:  # noqa: BLE001
        tags["_error"] = str(e)

    return tags


def analyze_track(filepath: Path, models: dict[str, Any] | None = None) -> TrackAnalysis:
    """Build a complete TrackAnalysis record for a single file."""
    record = TrackAnalysis(
        path=str(filepath),
        filename=filepath.name,
        extension=filepath.suffix.lower(),
        size_mb=round(filepath.stat().st_size / (1024 * 1024), 2),
    )

    fn_info = extract_from_filename(filepath.name)
    record.filename_artist = fn_info["filename_artist"]
    record.filename_title = fn_info["filename_title"]
    record.filename_bpm = fn_info["filename_bpm"]
    record.filename_key = fn_info["filename_key"]
    record.filename_mix = fn_info["filename_mix"]

    record.existing_tags = _read_existing_tags(filepath)

    if models:
        ml = analyze_audio_features(filepath, models)
        # DJ octave-error guard: fold the detected BPM into a plausible band
        # and reconcile against the filename-advertised BPM (now that both are
        # available on the record). Keeps the raw value out of files when the
        # detector locked onto the wrong octave (70↔140, 87↔174).
        if ml.ml_bpm is not None:
            ml.ml_bpm = _snap_bpm_octave(ml.ml_bpm, record.filename_bpm)
        record.ml_analysis = {k: v for k, v in asdict(ml).items() if v is not None}

    return record


# ---------------------------------------------------------------------------
# Parallel runner
# ---------------------------------------------------------------------------

# Worker-local model cache. multiprocessing.Pool initializer populates this
# per worker process; the worker function then reuses it across files.
_WORKER_MODELS: dict[str, Any] | None = None


def _worker_init(model_dir: str, use_gpu: str, engine: str = "essentia_tf",
                 genre_classifier: str = "discogs") -> None:
    """multiprocessing.Pool initializer. If load_models raises, multiprocessing
    silently restarts the worker in an infinite loop — masking real errors
    like OOM, missing essentia install, or corrupted model files behind a
    forever hang.

    We wrap load_models so init errors crash the worker FAST and visibly:
      - log the traceback to stderr (will end up in the WSL stderr stream
        the parent captures), prefixed `VIBECHEK_WORKER_INIT_FAIL:`
      - re-raise so the pool sees the worker as broken.

    Note on respawn behaviour: this `multiprocessing.Pool` path runs with
    `maxtasksperchild=200` (the value in the `Pool(...)` call below), NOT 1, so
    a worker that fails *init* is respawned by the pool and re-fails until the
    300s stall watchdog tears the run down with a useful error. The
    deterministic fail-fast init-failure detection lives on the hybrid
    work-stealing pool, which reports an init-failure sentinel on its result
    queue (`__init_fail__`) so the parent aborts immediately. The lower-risk
    documentation fix here is to describe what actually happens rather than
    change the recycle cadence and risk a regression in the steady-state TF
    memory-leak mitigation the 200-task recycle provides.
    """
    global _WORKER_MODELS
    try:
        _WORKER_MODELS = load_models(Path(model_dir), use_gpu=use_gpu, engine=engine,
                                     genre_classifier=genre_classifier)
    except Exception as e:  # noqa: BLE001
        import sys as _sys
        import traceback as _tb
        _sys.stderr.write(
            f"VIBECHEK_WORKER_INIT_FAIL: {type(e).__name__}: {e}\n"
            f"{_tb.format_exc()}\n"
        )
        _sys.stderr.flush()
        # Re-raise so the pool marks this worker as broken. With this path's
        # maxtasksperchild=200 the pool respawns the worker, which re-fails on
        # init; the 300s stall watchdog then tears the run down with a useful
        # error. (The hybrid pool fails fast via its `__init_fail__` sentinel.)
        raise


def _normalize_version(v: str) -> str:
    """Reduce a vibechek version string to a comparable shape.

    pip writes PEP 440 forms ("0.4.0b2") in dist-info metadata, while
    `__version__` carries the human form ("0.4.0-beta.2"). Both need to
    compare equal for the WSL-drift check, so we strip non-alphanumerics,
    lowercase, and collapse "beta" → "b" (pip's canonical) so both forms
    converge to the same string. "0.4.0-beta.2" → "040b2"; "0.4.0b2" →
    "040b2".  Older sentinel "0.1.0-dev" stays as "010dev".
    """
    import re as _re
    s = v.strip().lower()
    # Canonicalize pre-release tag spellings before stripping separators —
    # otherwise "beta" stays as "beta" and won't match the "b" pip canonical.
    s = s.replace("beta", "b").replace("alpha", "a")
    # Drop everything that isn't [a-z0-9] — periods, dashes, underscores.
    return _re.sub(r"[^a-z0-9]", "", s)


def _wsl_install_is_outdated(wsl_version: str, sidecar_version: str) -> bool:
    """True only when the WSL `vibechek` install is STRICTLY OLDER than the sidecar.

    Audit fix (LOW): the old guard blocked on *any* version mismatch
    (`_normalize_version(a) != _normalize_version(b)`), which wrongly rejected a
    WSL install that was actually NEWER than the sidecar (e.g. a user who
    upgraded the WSL venv ahead of the desktop app) and relied on a non-PEP-440
    string-normalization that mis-handles forms like "0.4.0rc1" vs "0.4.0".

    We now compare with `packaging.version.parse`, which canonicalises both pip
    ("0.4.0b2") and human ("0.4.0-beta.2") spellings to the same PEP 440 version
    and orders pre-releases correctly. Only a strictly-older WSL install is
    missing whatever safety patch landed since, so only that case is worth
    refusing to dispatch on. If either string can't be parsed as PEP 440 we
    fall back to the previous normalized-string EQUALITY test (block on
    mismatch) so we never *loosen* the guard on un-parseable inputs.
    """
    try:
        from packaging.version import InvalidVersion, parse  # noqa: PLC0415
    except ImportError:
        # packaging missing (shouldn't happen — it's a pip dependency): fall
        # back to the conservative equality check.
        return _normalize_version(wsl_version) != _normalize_version(sidecar_version)
    try:
        return parse(wsl_version) < parse(sidecar_version)
    except InvalidVersion:
        return _normalize_version(wsl_version) != _normalize_version(sidecar_version)


# Per-worker GPU memory budget. ~2.5 GB covers a steady-state essentia + TF
# worker process under multi-worker contention:
#
#   - Persistent CUDA context: ~800 MB
#   - EffNet + Discogs-400 + 6 mood heads, materialized as TF graphs: ~600 MB
#   - Activation buffers + intermediate tensors: ~400 MB
#   - Growth-allocator fragmentation: ~700 MB (this is the biggest hidden cost)
#
# The fragmentation overhead is the killer — TF_FORCE_GPU_ALLOW_GROWTH=true
# tells TF to grow allocations as needed, but it never shrinks. After dozens
# of inferences with varying batch shapes, each worker's footprint is
# significantly larger than the sum of its tensors. With N workers sharing
# one card, the effective steady-state per-worker can be 50-100% higher
# than naive accounting suggests.
#
# Tuned against an RTX 4070 Laptop (8 GB shared with the desktop):
#
#   _GPU_WORKER_MB = 1500 (original) → cap = 5 → stalled after ~12 tracks
#   _GPU_WORKER_MB = 1800              → cap = 4 → stalled at startup (init OOM)
#   _GPU_WORKER_MB = 2500 (current)    → cap = 3 → ran 12 tracks cleanly
#
# 2500 might leave parallelism on the table for dedicated cards (a 24-GB
# card gets 9 workers instead of 13), but the previous cap was producing
# 5-min stall-watchdog errors with no useful diagnostic — the worst possible
# failure mode. We prefer "always finishes" over "sometimes faster".
_GPU_WORKER_MB = 2500

# Fallback cap when VRAM probing fails — preserves prior behaviour of
# "at most 4 workers in GPU mode" so we never regress on systems where
# nvidia-smi is missing or unreadable (e.g. WSL with no GPU passthrough).
_GPU_FALLBACK_CAP = 4


def _visible_gpu_index() -> int:
    """The physical GPU index our workers will actually use.

    Every worker pins a single device: the GPU path sets
    `CUDA_VISIBLE_DEVICES=0`, so by default that's physical GPU 0. If the
    environment already constrains `CUDA_VISIBLE_DEVICES` (a power user pinning
    a specific card), honor the FIRST entry of that list instead. Returns 0 on
    anything unparseable.
    """
    raw = os.environ.get("CUDA_VISIBLE_DEVICES", "").strip()
    if not raw:
        return 0
    first = raw.split(",")[0].strip()
    try:
        idx = int(first)
        return idx if idx >= 0 else 0
    except ValueError:
        # Could be a GPU UUID (CUDA accepts those) — we can't map it to an
        # nvidia-smi row index cheaply, so fall back to device 0.
        return 0


def _probe_free_vram_mb() -> int | None:
    """Return free VRAM in MB on the SINGLE GPU our workers pin, via `nvidia-smi`.

    Audit fix (MED): the old probe SUMMED `memory.free` across every visible
    GPU, but every worker pins one device (`CUDA_VISIBLE_DEVICES=0`, or the
    first entry the environment already set). On a 2×8 GB rig the sum (~16 GB)
    sized ~6 workers that ALL piled onto GPU 0 → CUDA OOM, with only the 5-min
    stall watchdog as a backstop. We now query just the honored device's free
    VRAM so the worker cap reflects the card that's actually used.

    Returns None on any failure (binary missing, timeout, parse error, or the
    target index not present). Callers must treat None as "unknown" and fall
    back to the conservative cap.
    """
    import shutil as _shutil  # noqa: PLC0415  (lazy to keep import cost flat)
    import subprocess as _subprocess  # noqa: PLC0415

    smi = _shutil.which("nvidia-smi")
    if not smi:
        return None
    try:
        out = _subprocess.run(
            [smi, "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, _subprocess.TimeoutExpired) as e:
        log.debug("nvidia-smi free-VRAM probe failed: %s", e)
        return None
    if out.returncode != 0:
        log.debug("nvidia-smi returned %d: %s", out.returncode, out.stderr.strip())
        return None

    # One row per physical GPU, in index order. Pick the row for the device our
    # workers actually pin rather than summing across the whole machine.
    rows: list[int] = []
    for line in out.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(int(line))
        except ValueError:
            # A malformed row (rare) shouldn't poison the whole probe — skip it.
            continue
    if not rows:
        return None

    target = _visible_gpu_index()
    if target < len(rows):
        return rows[target]
    # Target index out of range (e.g. CUDA_VISIBLE_DEVICES points past the
    # visible cards) — fall back to the first card we can see.
    return rows[0]


# ---------------------------------------------------------------------------
# Hybrid CPU + GPU worker pool (work-stealing)
# ---------------------------------------------------------------------------
#
# The old multi-worker path ran ONE device: either N GPU workers (capped by
# VRAM, often just ~3 on an 8 GB card) OR N CPU workers — never both. On a box
# with a modest GPU and many cores, the GPU's low worker cap throttled total
# throughput while 16 CPU cores sat idle.
#
# The hybrid pool runs GPU workers (CUDA_VISIBLE_DEVICES=0) AND CPU workers
# (CUDA_VISIBLE_DEVICES=-1) at the same time, all pulling from ONE shared work
# queue. That queue IS the load balancer: a worker grabs the next track the
# instant it finishes its current one, so a fast device naturally processes
# more tracks than a slow one — no need to predict the split or clock devices
# ahead of time (we DO measure per-device throughput, but only to report it).
#
# Recycling: each worker exits after `maxtasks` tracks (freeing any TF/essentia
# native memory growth — the reliable way is process exit), and the supervisor
# respawns a same-device replacement as long as work remains, keeping the
# GPU/CPU ratio stable across the whole run.

_HYBRID_SENTINEL = None  # not used as a queue item; termination is via done_event


def _hybrid_worker_loop(in_q, out_q, done_event, model_dir, device, maxtasks, engine="essentia_tf", genre_classifier="discogs"):  # type: ignore[no-untyped-def]
    """One worker process: load models for `device`, then pull+analyze tracks.

    `device` is "0" for the GPU or "-1" for CPU-only (set as
    CUDA_VISIBLE_DEVICES before TF imports). Exits after `maxtasks` tracks (so
    the supervisor can recycle it) or when `done_event` is set. Init failure is
    reported on `out_q` as a sentinel so the parent can fail fast instead of
    hanging.
    """
    import queue as _queue
    import time as _time

    # Test hook (env-var, so it survives the spawn re-import that loses
    # monkeypatches): exercise the FULL real machinery — spawn, the shared
    # queue, work-stealing, recycling, the supervisor — with a fast fake
    # analyze and no essentia/TF. This is what lets the hybrid pool be tested
    # identically on Windows (spawn-only) and Linux, instead of skipping.
    fake = os.environ.get("VIBECHEK_FAKE_ANALYZE") == "1"

    os.environ["CUDA_VISIBLE_DEVICES"] = device
    use_gpu = "off" if device == "-1" else "on"
    if not fake:
        try:
            models = load_models(Path(model_dir), use_gpu=use_gpu, engine=engine,
                                 genre_classifier=genre_classifier)
        except Exception as e:  # noqa: BLE001
            import sys as _sys
            import traceback as _tb
            _sys.stderr.write(
                f"VIBECHEK_WORKER_INIT_FAIL: {type(e).__name__}: {e}\n{_tb.format_exc()}\n"
            )
            _sys.stderr.flush()
            try:
                out_q.put(("__init_fail__", device, f"{type(e).__name__}: {e}"))
            except Exception:  # noqa: BLE001
                pass
            return
    else:
        models = {"_fake": True}

    done = 0
    while done < maxtasks and not done_event.is_set():
        try:
            item = in_q.get(timeout=0.5)
        except _queue.Empty:
            continue
        if item is None:
            break
        idx, path = item
        t0 = _time.monotonic()
        if fake:
            _time.sleep(0.003)  # cheap, deterministic stand-in for analysis
            p = Path(path)
            rec = {"path": path, "filename": p.name,
                   "extension": p.suffix.lower(), "size_mb": 1.0, "error": None}
        else:
            try:
                rec = asdict(analyze_track(Path(path), models))
            except Exception as e:  # noqa: BLE001
                p = Path(path)
                rec = {
                    "path": path,
                    "filename": p.name,
                    "extension": p.suffix.lower(),
                    "size_mb": 0.0,
                    "error": str(e),
                }
        try:
            out_q.put((idx, rec, device, _time.monotonic() - t0))
        except Exception:  # noqa: BLE001
            # Parent gone / queue closed — stop quietly.
            break
        done += 1
    # Clean exit frees native TF memory; the supervisor respawns us if work
    # remains (so memory growth is bounded to ~maxtasks per worker lifetime).


class _HybridPool:
    """Supervises a fixed set of GPU + CPU worker processes over a shared queue.

    Exposes a tiny iterator-style surface (`next(timeout)` raising
    `multiprocessing.TimeoutError`) so the existing analyze loop's stall
    watchdog can drive it unchanged. Results arrive out of order (work-stealing);
    the caller doesn't rely on ordering.
    """

    def __init__(self, ctx, file_strs, model_dir, gpu_workers, cpu_workers, maxtasks, engine="essentia_tf", genre_classifier="discogs"):  # type: ignore[no-untyped-def]
        self._ctx = ctx
        self._model_dir = str(model_dir)
        self._maxtasks = maxtasks
        self._genre_classifier = genre_classifier
        self._engine = engine
        self.total = len(file_strs)
        self._results_out = 0
        # Per-device throughput accounting (track count + summed seconds).
        self.device_counts: dict[str, int] = {"0": 0, "-1": 0}
        self.device_seconds: dict[str, float] = {"0": 0.0, "-1": 0.0}

        self._in_q = ctx.Queue()
        self._out_q = ctx.Queue()
        self._done_event = ctx.Event()
        for item in enumerate(file_strs):  # (idx, path)
            self._in_q.put(item)

        # The device plan: one slot per worker. Respawns replace the same slot
        # device so the GPU:CPU ratio holds for the whole run.
        self._slots = ["0"] * gpu_workers + ["-1"] * cpu_workers
        self._procs: list = []
        for device in self._slots:
            self._procs.append(self._spawn(device))

    def _spawn(self, device):  # type: ignore[no-untyped-def]
        p = self._ctx.Process(
            target=_hybrid_worker_loop,
            args=(self._in_q, self._out_q, self._done_event,
                  self._model_dir, device, self._maxtasks, self._engine,
                  self._genre_classifier),
            daemon=True,
        )
        p._vibechek_device = device  # type: ignore[attr-defined]
        p.start()
        return p

    def _reap_and_respawn(self):
        """Replace workers that exited (recycle/crash) while work remains."""
        if self._done_event.is_set():
            return
        for i, p in enumerate(list(self._procs)):
            if not p.is_alive():
                p.join(timeout=0.1)
                if self._results_out < self.total:
                    device = getattr(p, "_vibechek_device", "-1")
                    self._procs[i] = self._spawn(device)

    def next(self, timeout):  # type: ignore[no-untyped-def]
        """Return the next (idx, record, device, seconds) or raise on timeout."""
        import multiprocessing as _mp
        import queue as _queue
        import time as _time

        deadline = _time.monotonic() + timeout
        while True:
            self._reap_and_respawn()
            try:
                item = self._out_q.get(timeout=0.2)
            except _queue.Empty:
                if _time.monotonic() > deadline:
                    raise _mp.TimeoutError from None
                continue
            if item and item[0] == "__init_fail__":
                # A worker couldn't load models — fail fast with the reason
                # rather than letting the stall watchdog time out in 5 min.
                raise RuntimeError(
                    f"Worker init failed on device "
                    f"{'GPU' if item[1] == '0' else 'CPU'}: {item[2]}"
                )
            idx, rec, device, seconds = item
            self._results_out += 1
            self.device_counts[device] = self.device_counts.get(device, 0) + 1
            self.device_seconds[device] = self.device_seconds.get(device, 0.0) + seconds
            if self._results_out >= self.total:
                self._done_event.set()
            return idx, rec, device, seconds

    def throughput_summary(self) -> str:
        # Report per-device track COUNT (how the shared queue split the work)
        # and average per-track latency (how fast each device is). We avoid a
        # "tracks/sec" figure because workers run in parallel — summing
        # per-track seconds and dividing would understate the real wall-clock
        # rate and confuse rather than inform.
        parts = []
        for dev, label in (("0", "GPU"), ("-1", "CPU")):
            n = self.device_counts.get(dev, 0)
            secs = self.device_seconds.get(dev, 0.0)
            if n > 0:
                avg = secs / n if n else 0.0
                parts.append(f"{label}: {n} tracks (avg {avg:.1f}s/track)")
        return " • ".join(parts) if parts else "(no throughput data)"

    def terminate(self):
        self._done_event.set()
        for p in self._procs:
            try:
                p.terminate()
            except Exception:  # noqa: BLE001
                pass

    def join(self, timeout=5):  # type: ignore[no-untyped-def]
        for p in self._procs:
            try:
                p.join(timeout=timeout)
            except Exception:  # noqa: BLE001
                pass


def _run_hybrid_pool(
    file_strs: list[str],
    config: AnalysisConfig,
    gpu_workers: int,
    cpu_workers: int,
    total: int,
    results: list[dict[str, Any]],
    on_progress: ProgressCallback | None,
    on_track: TrackCallback | None,
    output_path: Path | None,
) -> None:
    """Drive a `_HybridPool`, appending each completed record to `results`.

    Mirrors the single-pool loop exactly — same stall watchdog (300s with no
    result ⇒ dead pool), same cooperative cancellation, same per-track event /
    on_track / checkpoint handling — so the GUI behaves identically whether the
    run is hybrid or single-device.
    """
    import time as _time

    spawn_ctx = multiprocessing.get_context("spawn")
    _emit_event("stage", name="spawning_workers",
                message=f"Spawning {gpu_workers} GPU + {cpu_workers} CPU "
                        f"worker(s) (loading models, may take 10-30 s)...")
    report_progress(on_progress, 0, total,
                    f"Spawning {gpu_workers} GPU + {cpu_workers} CPU workers...")

    pool = _HybridPool(
        spawn_ctx, file_strs, str(config.models_dir),
        gpu_workers, cpu_workers, maxtasks=200, engine=config.inference_engine,
        genre_classifier=config.genre_classifier,
    )
    STALL_TIMEOUT = 300  # 5 min between any two results = dead pool

    try:
        for i in range(total):
            deadline = _time.monotonic() + STALL_TIMEOUT
            while True:
                try:
                    _idx, record, _device, _seconds = pool.next(timeout=10)
                    break
                except multiprocessing.TimeoutError:
                    if _time.monotonic() > deadline:
                        raise RuntimeError(
                            f"Analyze stalled — no track completed in "
                            f"{STALL_TIMEOUT}s. The hybrid worker pool is dead "
                            f"(likely OOM or essentia init crash). Check the "
                            f"sidecar log for VIBECHEK_WORKER_INIT_FAIL lines."
                        ) from None
                    if cancellation.is_cancelled():
                        raise cancellation.CancelledError("Analysis cancelled by user") from None

            if cancellation.is_cancelled():
                raise cancellation.CancelledError("Analysis cancelled by user")

            results.append(record)
            report_progress(on_progress, i + 1, total, Path(record.get("path", "")).name)
            _emit_event("track", index=i + 1, total=total, record=record)
            if on_track is not None:
                try:
                    on_track(record, i + 1, total)
                except Exception:  # noqa: BLE001
                    log.exception("on_track callback raised; ignoring")
            if output_path and ((i + 1) % 50 == 0 or (i + 1) == total):
                try:
                    _write_partial(output_path, results, total, in_progress=(i + 1) < total)
                except OSError as e:
                    log.warning("Partial checkpoint write failed (continuing): %s", e)

        # Per-device throughput — answers "how fast is each device working?"
        summary = pool.throughput_summary()
        log.info("Hybrid analyze throughput — %s", summary)
        _emit_event("stage", name="throughput", message=f"Throughput — {summary}")
    finally:
        pool.terminate()
        pool.join()


# ---------------------------------------------------------------------------
# Structured event stream — for surfacing analyze progress to a parent sidecar
# ---------------------------------------------------------------------------
#
# When run as a sidecar subprocess (Windows sidecar → WSL `vibechek analyze`,
# or the managed-venv variant), Rich's progress bar uses `\r` to overwrite a
# single line, which Python's line-buffered subprocess.PIPE collapses into ONE
# giant blob that only flushes after the process exits. The parent sidecar
# therefore sees NO progress between "starting" and "done" — the user stares
# at "starting…" for 30-60 s before the first byte of feedback arrives.
#
# To fix that, we emit our own structured-line stream IN PARALLEL with Rich:
#
#     VIBECHEK_EVENT\t<type>\t<json-payload>
#
# Each event is its own line (terminated by `\n`), so subprocess pipe
# buffering can't merge them. The parent's stderr reader grep-matches the
# sentinel prefix, parses the JSON, and re-emits as a JSON-RPC notification.
# The Rich progress bar still renders for interactive CLI users.
#
# Activation is via the `VIBECHEK_STREAM_PROGRESS=1` environment variable so
# interactive CLI invocations don't pay the extra IO cost — the Windows
# sidecar sets it in the WSL launcher (`vibechek/wsl.py:run_vibechek_in_wsl`)
# and the managed-venv launcher (`vibechek/native_install.py:run_vibechek_in_native_venv`).
import os as _os_for_events
import sys as _sys_for_events
import threading as _threading_for_events

EVENT_PREFIX = "VIBECHEK_EVENT\t"
_EVENT_LOCK = _threading_for_events.Lock()
_EVENT_STREAM_ON = _os_for_events.environ.get("VIBECHEK_STREAM_PROGRESS") == "1"


def _emit_event(event_type: str, **payload: Any) -> None:
    """Emit a structured event line if VIBECHEK_STREAM_PROGRESS=1.

    No-op for interactive CLI usage. The parent sidecar parses these lines
    out of the WSL/venv subprocess's stderr and turns them into JSON-RPC
    notifications the GUI subscribes to. Never raises — if JSON encoding
    fails (non-serializable payload), the event is silently dropped so a
    flaky event call can't crash analyze.
    """
    if not _EVENT_STREAM_ON:
        return
    try:
        encoded = json.dumps(payload, default=str, ensure_ascii=False)
    except Exception:
        return
    line = EVENT_PREFIX + event_type + "\t" + encoded + "\n"
    with _EVENT_LOCK:
        try:
            _sys_for_events.stderr.write(line)
            _sys_for_events.stderr.flush()
        except Exception:  # noqa: BLE001
            pass


def _worker_analyze(filepath_str: str) -> dict[str, Any]:
    filepath = Path(filepath_str)
    try:
        return asdict(analyze_track(filepath, _WORKER_MODELS))
    except Exception as e:  # noqa: BLE001
        return {
            "path": filepath_str,
            "filename": filepath.name,
            "extension": filepath.suffix.lower(),
            "size_mb": 0.0,
            "error": str(e),
        }


TrackCallback = Callable[[dict[str, Any], int, int], None]


def analyze_directory(
    library_path: Path,
    config: AnalysisConfig | None = None,
    on_progress: ProgressCallback | None = None,
    output_path: Path | None = None,
    skip: int = 0,
    limit: int | None = None,
    skip_paths: set[str] | None = None,
    on_track: TrackCallback | None = None,
) -> dict[str, Any]:
    """Analyze every audio file under `library_path`.

    Writes an incremental JSON report to `output_path` (if provided) every 50
    tracks so a crash doesn't lose all progress. Uses `config.workers` parallel
    processes when >1.

    When `skip_paths` is provided, files whose absolute string path is in the
    set are skipped — used by the GUI's incremental "analyze new tracks only"
    flow so re-runs don't re-process the whole library.

    `on_track(record, current_idx, total)` is called as each track's ML record
    becomes available — the RPC layer uses this to stream `track_analyzed`
    notifications to the GUI so analyzed tracks appear live instead of after
    the whole batch finishes. Pass None to opt out (CLI does this).
    """
    if config is None:
        config = AnalysisConfig()

    # Surface "scanning files" as soon as the call starts. Without this, the
    # GUI's progress overlay sits at "starting…" through find_audio_files +
    # the slow preflight for 5-30 s, which is a particularly bad UX problem. The
    # event channel ignores it when VIBECHEK_STREAM_PROGRESS isn't set, so
    # interactive CLI users don't see anything new.
    _emit_event("stage", name="scanning",
                message="Scanning library for audio files...")
    report_progress(on_progress, 0, 0, "Scanning library...")

    files = find_audio_files(library_path)
    if skip_paths:
        files = [f for f in files if str(f) not in skip_paths]
    if skip:
        files = files[skip:]
    if limit:
        files = files[:limit]
    total = len(files)

    if total == 0:
        return {"status": "complete", "summary": {"total_files": 0, "analyzed": 0, "errors": 0},
                "tracks": [], "statistics": {}}

    _emit_event("stage", name="preflight",
                message=f"Checking environment ({total} files queued)...")
    report_progress(on_progress, 0, total, f"Checking environment ({total} files)...")

    # Pre-flight: catch missing essentia / models BEFORE we spawn a worker pool.
    # An ImportError inside a multiprocessing.Pool initializer hangs the pool
    # silently instead of surfacing the error — we never want to land there.
    #
    # *quick_wsl=False*: the default preflight uses the quick WSL probe for
    # sub-second GUI responsiveness, but quick mode can't tell us whether a
    # WSL distro actually has essentia. We're about to start a multi-hour
    # operation; an extra ~5 seconds of probe time is fine, and without it
    # we'd false-fail every analyze-via-WSL run.
    from vibechek.preflight import preflight  # noqa: PLC0415

    pf = preflight(config.models_dir, quick_wsl=False, engine=config.inference_engine)
    if not pf.ready:
        raise RuntimeError(
            "Cannot analyze: " + "; ".join(pf.reasons_not_ready) +
            ". Run `vibechek preflight` (or check Settings in the GUI) "
            "for actionable instructions."
        )

    # If native essentia is missing but WSL has it, route through WSL transparently.
    if pf.analyze_via == "wsl":
        # Look up the installed WSL vibechek version from the preflight's
        # already-probed WSLStatus so `_analyze_via_wsl` doesn't pay for a
        # second `detect_wsl(quick=False)` (5-30 s) just to read one field.
        wsl_distro = pf.wsl.usable_distro  # type: ignore[union-attr]
        wsl_version = None
        if pf.wsl is not None:
            wsl_version = next(
                (d.vibechek_version for d in pf.wsl.distros
                 if d.name == wsl_distro and d.vibechek_version),
                None,
            )
        _emit_event("stage", name="wsl_dispatch",
                    message=f"Starting WSL analyzer ({wsl_distro})...")
        report_progress(on_progress, 0, total,
                        f"Starting WSL analyzer ({wsl_distro})...")
        return _analyze_via_wsl(
            library_path, config, on_progress, output_path, skip, limit,
            distro=wsl_distro,
            wsl_vibechek_version=wsl_version,
            on_track=on_track,
        )

    # If native essentia is missing but the managed Linux/macOS venv has it,
    # route through that venv. Mirrors the WSL path; no path translation needed.
    if pf.analyze_via == "native_venv":
        _emit_event("stage", name="venv_dispatch",
                    message="Starting managed-venv analyzer...")
        report_progress(on_progress, 0, total, "Starting managed-venv analyzer...")
        return _analyze_via_native_venv(
            library_path, config, on_progress, output_path, skip, limit,
            on_track=on_track,
        )

    file_strs = [str(f) for f in files]
    results: list[dict[str, Any]] = []

    # Resolve worker count with TWO real-world constraints baked in:
    #
    #   1. *Memory*: each worker holds ~500 MB of model weights. We use
    #      psutil to find total RAM, reserve 2 GB for the OS / GUI / other
    #      apps, and cap workers at floor(available / 800 MB) (the 800 MB
    #      buffer covers TF runtime overhead beyond just the weights).
    #
    #   2. *GPU contention*: even with TF_FORCE_GPU_ALLOW_GROWTH=true,
    #      N workers each carving up one GPU is fragile. We cap at 4 in GPU
    #      mode — empirically the sweet spot for ~8 GB consumer cards.
    requested = config.workers if config.workers and config.workers > 0 else max(1, cpu_count() - 1)
    workers = max(1, min(requested, cpu_count()))

    # Memory cap
    try:
        import psutil  # noqa: PLC0415
        total_mb = psutil.virtual_memory().total // (1024 * 1024)
        per_worker_mb = _per_worker_mb(config.genre_classifier)
        usable_mb = max(0, total_mb - 2048)
        memory_cap = max(1, usable_mb // per_worker_mb)
        if memory_cap < workers:
            log.warning(
                "Capping workers from %d -> %d based on available RAM "
                "(%d MB total, ~%d MB per worker%s)",
                workers, memory_cap, total_mb, per_worker_mb,
                " — CLAP genre model loads in every worker" if per_worker_mb != 800 else "",
            )
            workers = memory_cap
    except ImportError:
        pass  # psutil missing — fall through with the cpu_count-based number

    # Split the RAM-bounded budget into GPU + CPU workers.
    #
    # `workers` is the RAM-capped total. In GPU mode we probe free VRAM to size
    # the GPU subset (`gpu_cap`), then — when hybrid is enabled — fill the rest
    # of the RAM budget with CPU workers instead of throwing that headroom away.
    # The old behaviour capped the WHOLE run to `gpu_cap` (≈3 on an 8 GB card),
    # leaving most cores idle; hybrid keeps all of them busy via the shared
    # work queue.
    ram_cap = workers  # RAM-bounded total (== memory_cap when psutil present)
    gpu_workers = 0
    cpu_workers = ram_cap  # default (use_gpu=off, or no GPU): all CPU

    if config.use_gpu in ("auto", "on"):
        free_vram_mb = _probe_free_vram_mb()
        if free_vram_mb is not None:
            gpu_cap = max(1, free_vram_mb // _GPU_WORKER_MB)
            cap_reason = (
                f"{free_vram_mb // 1024} GB free VRAM "
                f"(~{_GPU_WORKER_MB} MB per worker)"
            )
        else:
            gpu_cap = _GPU_FALLBACK_CAP
            cap_reason = (
                "nvidia-smi unavailable; using conservative GPU cap of "
                f"{_GPU_FALLBACK_CAP}"
            )
            log.warning(
                "GPU mode active but VRAM probe failed — capping GPU workers at "
                "%d. Hybrid CPU workers still fill the rest of the RAM budget.",
                _GPU_FALLBACK_CAP,
            )
        gpu_workers = min(gpu_cap, ram_cap)

        if config.hybrid_cpu_gpu and ram_cap > gpu_workers:
            # Hybrid: GPU workers + CPU workers (filling remaining RAM, bounded
            # by core count) run concurrently against one shared queue.
            cpu_workers = max(0, min(cpu_count(), ram_cap) - gpu_workers)
            msg = (
                f"Hybrid mode: {gpu_workers} GPU + {cpu_workers} CPU workers "
                f"({cap_reason}; CPU fills the rest of the RAM budget)."
            )
            log.info(msg)
            report_progress(on_progress, 0, total, msg)
            _emit_event("stage", name="hybrid_plan", message=msg,
                        gpu_workers=gpu_workers, cpu_workers=cpu_workers,
                        reason=cap_reason)
        else:
            # Single-device GPU pool: hybrid disabled, or GPU cap already
            # covers the whole RAM budget. Surface the cap so the user
            # understands why fewer workers than configured are running.
            cpu_workers = 0
            if gpu_workers < ram_cap:
                requested_workers = (
                    config.workers if config.workers and config.workers > 0
                    else max(1, cpu_count() - 1)
                )
                msg = (
                    f"Capped to {gpu_workers} GPU worker(s) from "
                    f"{requested_workers} ({cap_reason}). Enable hybrid CPU+GPU "
                    f"(Settings) or set GPU mode 'off' to use more CPU workers."
                )
                log.warning(msg)
                report_progress(on_progress, 0, total, msg)
                _emit_event("stage", name="worker_cap", message=msg,
                            requested=requested_workers, applied=gpu_workers,
                            reason=cap_reason)

    workers = gpu_workers + cpu_workers
    hybrid = gpu_workers > 0 and cpu_workers > 0

    log.info("Analyzing %d files with %d worker(s) (GPU=%d, CPU=%d, mode=%s)",
             total, workers, gpu_workers, cpu_workers, config.use_gpu)
    _emit_event("stage", name="analyzing",
                message=f"Analyzing {total} files with {workers} worker(s)"
                        + (f" ({gpu_workers} GPU + {cpu_workers} CPU)" if hybrid else ""))
    report_progress(on_progress, 0, total,
                    f"Analyzing {total} files with {workers} worker(s)")

    if hybrid:
        _run_hybrid_pool(
            file_strs, config, gpu_workers, cpu_workers, total,
            results, on_progress, on_track, output_path,
        )
    elif workers == 1:
        _emit_event("stage", name="loading_models",
                    message="Loading ML models...")
        report_progress(on_progress, 0, total, "Loading ML models...")
        models = load_models(
            config.models_dir, use_gpu=config.use_gpu, engine=config.inference_engine,
            genre_classifier=config.genre_classifier,
        )
        for i, filepath in enumerate(files):
            cancellation.check()  # Raises CancelledError if user clicked Cancel
            report_progress(on_progress, i + 1, total, filepath.name)
            try:
                record = asdict(analyze_track(filepath, models))
            except Exception as e:  # noqa: BLE001
                record = {
                    "path": str(filepath),
                    "filename": filepath.name,
                    "extension": filepath.suffix.lower(),
                    "size_mb": 0.0,
                    "error": str(e),
                }
            results.append(record)
            # Per-track stream — sidecar relays this to the GUI so analyzed
            # tracks appear live instead of after the whole batch.
            _emit_event("track", index=i + 1, total=total, record=record)
            if on_track is not None:
                try:
                    on_track(record, i + 1, total)
                except Exception:  # noqa: BLE001
                    log.exception("on_track callback raised; ignoring")
            if output_path and ((i + 1) % 50 == 0 or (i + 1) == total):
                # A transient checkpoint-write failure (disk full, permission
                # blip) must NOT abort the run — that would discard the
                # in-memory results AND defeat the point of checkpointing. The
                # final write at the end is the backstop.
                try:
                    _write_partial(output_path, results, total, in_progress=(i + 1) < total)
                except OSError as e:
                    log.warning("Partial checkpoint write failed (continuing): %s", e)
    else:
        # *Always use spawn for multi-worker analyze* — even on Linux where
        # Python defaults to fork on <3.14. Reasons:
        #
        #   1. essentia bundles TensorFlow as a native C++ lib. If the parent
        #      process has touched TF for any reason (preflight, system_info,
        #      anything), fork()-ing it leaves the child with a half-initialized
        #      CUDA context that segfaults or hangs on first use.
        #   2. Some Python libraries (e.g., libxml, libcairo) install atfork
        #      handlers that lock up after fork() if any worker thread was
        #      mid-call. spawn sidesteps the entire class of problem.
        #   3. The slight startup cost (~1 sec per worker for re-imports) is
        #      dwarfed by the per-track analysis time.
        _emit_event("stage", name="spawning_workers",
                    message=f"Spawning {workers} worker process(es) "
                            f"(loading models, may take 10-30 s)...")
        report_progress(on_progress, 0, total,
                        f"Spawning {workers} workers (loading models)...")
        spawn_ctx = multiprocessing.get_context("spawn")
        # maxtasksperchild=200: every 200 tracks the worker recycles, freeing
        # any TF memory leaks that essentia / TF native code might accumulate.
        # Cheap; one re-init per ~200 tracks is invisible alongside analysis.
        with spawn_ctx.Pool(
            processes=workers,
            initializer=_worker_init,
            initargs=(str(config.models_dir), config.use_gpu, config.inference_engine,
                      config.genre_classifier),
            maxtasksperchild=200,
        ) as pool:
            # Stall watchdog: if no result arrives in STALL_TIMEOUT seconds, the
            # pool is wedged (workers all crashed during init, or all OOM-killed).
            # Tear down with a useful error instead of hanging until the RPC
            # timeout (1 hour) cuts us off.
            import time as _time
            STALL_TIMEOUT = 300  # 5 minutes between any two results = dead
            iterator = pool.imap_unordered(_worker_analyze, file_strs)

            def _next_with_stall_check():
                """Like next(iterator) but raises RuntimeError on stall."""
                deadline = _time.monotonic() + STALL_TIMEOUT
                # multiprocessing's imap iterator doesn't expose a timeout
                # directly, but it's actually a `_PoolReadyResult` wrapper
                # whose `.next(timeout)` we can use.
                while True:
                    try:
                        return iterator.next(timeout=10)  # type: ignore[attr-defined]
                    except multiprocessing.TimeoutError:
                        if _time.monotonic() > deadline:
                            raise RuntimeError(
                                f"Analyze stalled — no track completed in "
                                f"{STALL_TIMEOUT}s. The worker pool is dead "
                                f"(likely OOM or essentia init crash). Check "
                                f"the sidecar log for VIBECHEK_WORKER_INIT_FAIL "
                                f"lines."
                            ) from None
                        if cancellation.is_cancelled():
                            raise cancellation.CancelledError(
                                "Analysis cancelled by user"
                            ) from None

            for i in range(total):
                try:
                    record = _next_with_stall_check()
                except cancellation.CancelledError:
                    pool.terminate()
                    pool.join()
                    raise
                except StopIteration:
                    break
                # On cancel: terminate the pool (kills outstanding workers) and bail.
                if cancellation.is_cancelled():
                    pool.terminate()
                    pool.join()
                    raise cancellation.CancelledError("Analysis cancelled by user")
                results.append(record)
                report_progress(on_progress, i + 1, total, Path(record.get("path", "")).name)
                _emit_event("track", index=i + 1, total=total, record=record)
                if on_track is not None:
                    try:
                        on_track(record, i + 1, total)
                    except Exception:  # noqa: BLE001
                        log.exception("on_track callback raised; ignoring")
                if output_path and ((i + 1) % 50 == 0 or (i + 1) == total):
                    # See single-worker path: a transient checkpoint-write
                    # failure must not abort the whole analyze.
                    try:
                        _write_partial(output_path, results, total, in_progress=(i + 1) < total)
                    except OSError as e:
                        log.warning("Partial checkpoint write failed (continuing): %s", e)

    report = _build_report(
        results, total, in_progress=False,
        genre_policy=(config.genre_source_policy, config.genre_ml_override_confidence),
        web_cfg={"enabled": config.genre_web_lookup, "backend": config.genre_llm_backend},
    )
    if output_path:
        # Atomic write — a kill/power-loss/disk-full mid-write must not
        # truncate the report (which can represent 30+ min of GPU time).
        atomic_write_json(Path(output_path), report, indent=2)
    return report


def _make_event_aware_line_handler(
    on_progress: ProgressCallback | None,
    on_track: TrackCallback | None,
    stderr_tail: list[str] | None = None,
    *,
    progress_re: re.Pattern[str] | None = None,
    noise_re: re.Pattern[str] | None = None,
) -> Callable[[str], None]:
    """Build an `on_stderr_line` handler that parses VIBECHEK_EVENT lines.

    The WSL / managed-venv `vibechek analyze` subprocess emits two channels:

      1. Structured `VIBECHEK_EVENT\\t<type>\\t<json>` lines when
         `VIBECHEK_STREAM_PROGRESS=1` is set in the env. These carry the
         "starting WSL...", "spawning workers...", per-track records that the
         GUI needs for live feedback.
      2. Plain Rich progress bar output, parsed by the legacy regex
         `(\\d+)\\s*/\\s*(\\d+)`. This is kept as a fallback for the
         interactive CLI path and any older WSL install that pre-dates the
         event channel.

    `stderr_tail` (if provided) gets every non-noise line appended for the
    bounded error-context buffer the WSL launcher uses on failure exit.
    `noise_re` filters out essentia / TF chatter before the tail accumulates.

    Returns a closure ready to pass to `run_vibechek_in_wsl(..., on_stderr_line=...)`
    or `run_vibechek_in_native_venv(..., on_stderr_line=...)`.
    """
    if progress_re is None:
        progress_re = re.compile(r"(\d+)\s*/\s*(\d+)")

    def _on_line(line: str) -> None:
        # 1) Structured event channel — the primary signal during analyze.
        if line.startswith(EVENT_PREFIX):
            try:
                _, event_type, payload_json = line.split("\t", 2)
                payload = json.loads(payload_json)
            except (ValueError, json.JSONDecodeError) as e:
                log.debug("Failed to parse VIBECHEK_EVENT line: %s", e)
                return
            if event_type == "stage":
                if on_progress is not None:
                    msg = str(payload.get("message", payload.get("name", "")))
                    try:
                        on_progress(0, 0, msg)
                    except Exception:  # noqa: BLE001
                        log.exception("on_progress raised on stage event")
            elif event_type == "track":
                idx = int(payload.get("index", 0))
                total = int(payload.get("total", 0))
                record = payload.get("record") or {}
                if on_progress is not None:
                    try:
                        on_progress(idx, total,
                                    Path(record.get("path", "")).name)
                    except Exception:  # noqa: BLE001
                        log.exception("on_progress raised on track event")
                if on_track is not None:
                    try:
                        on_track(record, idx, total)
                    except Exception:  # noqa: BLE001
                        log.exception("on_track raised; ignoring")
            return  # don't fall through to tail/regex for event lines

        # 2) Bounded stderr tail for error-context (used by the WSL caller).
        if stderr_tail is not None:
            if noise_re is None or not noise_re.search(line):
                stderr_tail.append(line)
                if len(stderr_tail) > 80:
                    stderr_tail.pop(0)

        # 3) Legacy "N/M" regex — covers the rare case of a pre-event WSL
        # install where Rich's final progress line is the only signal.
        if not line:
            return
        m = progress_re.search(line)
        if m and on_progress is not None:
            try:
                on_progress(int(m.group(1)), int(m.group(2)), line[:80])
            except Exception:  # noqa: BLE001
                log.exception("on_progress raised on legacy progress line")

    return _on_line


def _analyze_via_native_venv(
    library_path: Path,
    config: AnalysisConfig,
    on_progress: ProgressCallback | None,
    output_path: Path | None,
    skip: int,
    limit: int | None,
    on_track: TrackCallback | None = None,
) -> dict[str, Any]:
    """Route analyze to the managed `~/.vibechek/venv/bin/vibechek` on Linux/macOS.

    No path translation needed (unlike `_analyze_via_wsl`) — the venv runs in
    the host filesystem. We just shell out, stream progress, and read the
    result JSON back.
    """
    import json as _json
    import tempfile

    from vibechek.native_install import run_vibechek_in_native_venv

    if output_path is None:
        tmp = tempfile.NamedTemporaryFile(
            suffix=".json", prefix="vibechek-venv-", delete=False,
        )
        tmp.close()
        local_output = Path(tmp.name)
    else:
        local_output = Path(output_path)

    workers = config.workers if config.workers and config.workers > 0 else 0

    args = [
        "analyze", str(library_path),
        "-o", str(local_output),
        "--gpu", config.use_gpu,
        "--models-dir", str(config.models_dir),
        "--engine", config.inference_engine,
        "--genre-policy", config.genre_source_policy,
        "--genre-classifier", config.genre_classifier,
        "--genre-llm-backend", config.genre_llm_backend,
        "--genre-override-confidence", str(config.genre_ml_override_confidence),
        "--genre-web-lookup" if config.genre_web_lookup else "--no-genre-web-lookup",
    ]
    if workers > 0:
        args += ["--workers", str(workers)]
    if skip:
        args += ["--skip", str(skip)]
    if limit:
        args += ["--limit", str(limit)]
    if not config.hybrid_cpu_gpu:
        args += ["--no-hybrid"]

    # Shared handler — parses both the structured VIBECHEK_EVENT channel
    # (stage transitions, per-track records) AND the legacy Rich-progress
    # regex. See _make_event_aware_line_handler's docstring for the schema.
    on_line = _make_event_aware_line_handler(
        on_progress=on_progress,
        on_track=on_track,
    )

    result = run_vibechek_in_native_venv(args, on_stderr_line=on_line, engine=config.inference_engine)

    if result.returncode != 0:
        raise RuntimeError(
            f"vibechek analyze in managed venv exited with "
            f"{result.returncode}. stdout tail:\n{result.stdout[-1500:]}"
        )

    if not local_output.exists() or local_output.stat().st_size == 0:
        raise RuntimeError(
            f"managed-venv analyze finished but wrote no output to {local_output}"
        )

    # Read bytes once + decode defensively (a truncated multibyte sequence
    # would raise UnicodeDecodeError, not JSONDecodeError).
    raw_bytes = local_output.read_bytes()
    raw_text = raw_bytes.decode("utf-8", errors="replace")
    try:
        report = _json.loads(raw_text)
    except _json.JSONDecodeError as e:
        raise RuntimeError(
            f"managed-venv analyze wrote {len(raw_bytes)} bytes to "
            f"{local_output} but they don't parse as JSON: {e}. "
            f"First 200 bytes: {raw_text[:200]!r}"
        ) from e

    if output_path is None:
        try:
            local_output.unlink()
        except OSError:
            pass

    return report


def _analyze_via_wsl(
    library_path: Path,
    config: AnalysisConfig,
    on_progress: ProgressCallback | None,
    output_path: Path | None,
    skip: int,
    limit: int | None,
    distro: str,
    wsl_vibechek_version: str | None = None,
    on_track: TrackCallback | None = None,
) -> dict[str, Any]:
    """Route the analyze to vibechek-inside-WSL.

    All file paths get translated: Windows `C:\\foo` ↔ WSL `/mnt/c/foo`. The
    resulting analysis.json comes back with WSL paths in it; we rewrite them
    to Windows paths before returning so the GUI sees a consistent view.
    """
    import json as _json
    import re
    import tempfile

    from vibechek.wsl import run_vibechek_in_wsl, win_to_wsl_path, wsl_to_win_path

    # Pick an output file the WSL side can write to (under Windows tmp so we
    # can read it back from native Python).
    if output_path is None:
        tmp = tempfile.NamedTemporaryFile(
            suffix=".json", prefix="vibechek-wsl-", delete=False,
        )
        tmp.close()
        local_output = Path(tmp.name)
    else:
        local_output = Path(output_path)

    wsl_input = win_to_wsl_path(str(library_path))
    wsl_output = win_to_wsl_path(str(local_output))
    # Critical: tell WSL where the models live. The user downloaded them on
    # the Windows side; WSL sees that path under /mnt/c/.... Without this,
    # WSL would default to its own ~/.local/share/Vibechek/models/, find
    # nothing, and either re-download (slow) or fail silently.
    wsl_models_dir = win_to_wsl_path(str(config.models_dir))

    workers = config.workers if config.workers and config.workers > 0 else 0

    args = [
        "analyze", wsl_input,
        "-o", wsl_output,
        "--gpu", config.use_gpu,
        "--models-dir", wsl_models_dir,
        "--engine", config.inference_engine,
        "--genre-policy", config.genre_source_policy,
        "--genre-classifier", config.genre_classifier,
        "--genre-llm-backend", config.genre_llm_backend,
        "--genre-override-confidence", str(config.genre_ml_override_confidence),
        "--genre-web-lookup" if config.genre_web_lookup else "--no-genre-web-lookup",
    ]
    if workers > 0:
        args += ["--workers", str(workers)]
    if skip:
        args += ["--skip", str(skip)]
    if limit:
        args += ["--limit", str(limit)]
    if not config.hybrid_cpu_gpu:
        args += ["--no-hybrid"]

    # Route to the venv matching the engine: "venv-onnx" (plain essentia +
    # onnxruntime) for the TF-free ONNX path, "venv" (essentia-tensorflow)
    # otherwise. run_vibechek_in_wsl uses ONLY that venv's binary for onnx.
    venv_subdir = "venv-onnx" if config.inference_engine == "onnx" else "venv"

    # Drop high-volume essentia / TF noise lines from the bounded tail
    # buffer so an 80-line window survives the per-worker spam and actually
    # contains the real error (traceback, OOM kill, etc.). Without this
    # filter, "MusicExtractorSVM: no classifier models were configured by
    # default" (one line per worker init, repeated by every recycle) and TF's
    # GPU-init chatter saturate the window and we end up reporting "stderr
    # tail: 40 copies of MusicExtractorSVM" instead of the actual python
    # traceback that fired one line earlier.
    stderr_tail: list[str] = []
    _STDERR_NOISE = re.compile(
        r"MusicExtractorSVM:|"
        r"^\s*\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+: [IWE] tensorflow|"
        r"^Skipping registering GPU devices\.\.\.$|"
        r"^Your kernel may have been built without NUMA support\.$|"
        r"^pciBusID:|^coreClock:"
    )
    # Shared handler — parses structured VIBECHEK_EVENT lines (stage
    # transitions, per-track records) AND keeps the legacy Rich-progress
    # regex as a fallback. The structured channel is activated by setting
    # VIBECHEK_STREAM_PROGRESS=1 in the WSL launcher (see
    # vibechek/wsl.py:run_vibechek_in_wsl); the WSL `vibechek analyze`
    # process emits one VIBECHEK_EVENT line per stage transition and one
    # per finished track, which the GUI uses to drive a live "starting
    # WSL → spawning workers → analyzed: filename" feedback chain.
    #
    # *Path translation*: WSL-emitted track records carry POSIX paths
    # (/mnt/c/...). We translate them back to Windows form here BEFORE
    # forwarding to on_track — otherwise the GUI receives "/mnt/c/Users/..."
    # paths and Tauri's `convertFileSrc` produces broken asset:// URLs that
    # silently fail to load. The final report's tracks also get translated
    # below, but the streaming events need their own translation step or the
    # AudioPreview component breaks during live analyze.
    if on_track is not None:
        _wrapped_on_track = on_track

        def _on_track_translated(record: dict[str, Any], current: int, total: int) -> None:
            if "path" in record and isinstance(record["path"], str):
                record = {**record, "path": wsl_to_win_path(record["path"])}
            _wrapped_on_track(record, current, total)
        translated_on_track: TrackCallback | None = _on_track_translated
    else:
        translated_on_track = None

    on_line = _make_event_aware_line_handler(
        on_progress=on_progress,
        on_track=translated_on_track,
        stderr_tail=stderr_tail,
        noise_re=_STDERR_NOISE,
    )

    # Version-drift guard: catch the worst class of "silent exit 1" — the WSL
    # vibechek install is older than the sidecar, so it's missing whatever
    # safety patch landed since (worker cap, stall watchdog, atomic writes,
    # ...). The user's symptom is "exits 1 in seconds, stderr is just essentia
    # noise" because the old code paths haven't been written defensively.
    # Refuse to dispatch and surface a clear repair message instead. The
    # caller passes the version it already probed via preflight; if probing
    # failed (None), we skip the guard rather than block an otherwise-healthy
    # analyze on a flaky detection.
    if wsl_vibechek_version is not None:
        from vibechek import __version__ as _sidecar_version  # noqa: PLC0415
        # Auto-update IN PLACE when the WSL install is strictly older than the
        # sidecar. The WSL analyzer must always match the app so new flags +
        # safety patches (worker-cap, stall-watchdog, genre reconciliation, …)
        # Just Work — we update transparently instead of either aborting on an
        # unknown flag or silently degrading. A NEWER WSL venv (user upgraded
        # ahead of the app) is left alone. Fast path: vibechek package only,
        # targeting the engine's venv; clear error only if the update fails.
        if _wsl_install_is_outdated(wsl_vibechek_version, _sidecar_version):
            from vibechek.wsl import upgrade_vibechek_in_wsl  # noqa: PLC0415
            log.info(
                "WSL vibechek %s < sidecar %s — auto-updating in place (engine=%s)",
                wsl_vibechek_version, _sidecar_version, config.inference_engine,
            )
            report_progress(
                on_progress, 0, 0,
                f"Updating the analysis engine in {distro} to "
                f"{_sidecar_version} (one-time)…",
            )
            up = upgrade_vibechek_in_wsl(
                distro, on_progress=on_progress, engine=config.inference_engine,
            )
            if up.get("cancelled"):
                raise cancellation.CancelledError("Analysis cancelled by user")
            if not up.get("ok"):
                raise RuntimeError(
                    f"WSL vibechek was out of date ({wsl_vibechek_version} < "
                    f"{_sidecar_version}) and the automatic in-place update "
                    f"failed: {up.get('error', 'unknown error')}. Re-run "
                    f"Settings → \"Set up WSL\" to repair it."
                )

    result = run_vibechek_in_wsl(distro, args, on_stderr_line=on_line, venv_subdir=venv_subdir)

    if result.returncode != 0 and any("no such option" in ln.lower() for ln in stderr_tail):
        # Same-version-but-code-stale WSL install: the version strings MATCH
        # (we don't bump per commit) but the WSL CLI predates a flag we now
        # pass unconditionally, so click exits 2 with "No such option". The
        # version-drift guard above can't see this (it compares versions for
        # strict inequality) — treat click's rejection itself as the drift
        # signal: update the WSL package in place once and retry the analyze.
        from vibechek.wsl import upgrade_vibechek_in_wsl  # noqa: PLC0415
        log.warning(
            "WSL vibechek rejected a CLI flag (same-version code drift) — "
            "auto-updating in place and retrying once",
        )
        report_progress(on_progress, 0, 0,
                        f"Updating the analysis engine in {distro} (one-time)…")
        up = upgrade_vibechek_in_wsl(
            distro, on_progress=on_progress, engine=config.inference_engine,
        )
        if up.get("cancelled"):
            raise cancellation.CancelledError("Analysis cancelled by user")
        if up.get("ok"):
            stderr_tail.clear()
            result = run_vibechek_in_wsl(
                distro, args, on_stderr_line=on_line, venv_subdir=venv_subdir,
            )
        # If the upgrade failed we fall through: the generic error below shows
        # the "No such option" stderr tail, which is an honest description.

    if result.returncode != 0:
        stderr_blob = "\n".join(stderr_tail[-40:]) if stderr_tail else "(no stderr output)"
        raise RuntimeError(
            f"vibechek analyze inside WSL ({distro}) exited with "
            f"{result.returncode}.\n\n"
            f"stderr tail:\n{stderr_blob}\n\n"
            f"stdout tail:\n{result.stdout[-1500:] if result.stdout else '(empty)'}"
        )

    # Read the analysis.json the WSL side wrote and rewrite paths.
    # Empty output (0 bytes) means the WSL CLI crashed BEFORE writing
    # anything, even if it returned exit 0 (which can happen when the
    # crash is in a child process or via SyntaxError in the entry-point
    # shim). Surface a useful error instead of letting json.loads('') leak
    # the unhelpful "Expecting value: line 1 column 1 (char 0)" toast.
    if not local_output.exists() or local_output.stat().st_size == 0:
        stderr_blob = "\n".join(stderr_tail[-40:]) if stderr_tail else "(no stderr output)"
        raise RuntimeError(
            f"WSL analyze ({distro}) returned exit 0 but wrote no output to "
            f"{local_output}. This usually means the venv's `vibechek` shim "
            f"is corrupted (a pre-beta.10 CUDA install bug). Try re-running "
            f"the GUI — the next `wsl_status` call auto-repairs the shim.\n\n"
            f"stderr tail:\n{stderr_blob}"
        )

    # Read the bytes ONCE. A truncated multibyte sequence raises
    # UnicodeDecodeError (not JSONDecodeError), which must be caught too or it
    # escapes as an opaque crash instead of the friendly "doesn't parse"
    # message. Decode with errors="replace" so we always get a string for the
    # error preview, and let json.loads surface the structural problem.
    raw_bytes = local_output.read_bytes()
    raw_text = raw_bytes.decode("utf-8", errors="replace")
    try:
        report = _json.loads(raw_text)
    except _json.JSONDecodeError as e:
        raise RuntimeError(
            f"WSL analyze ({distro}) wrote {len(raw_bytes)} bytes "
            f"to {local_output} but they don't parse as JSON: {e}. "
            f"First 200 bytes: {raw_text[:200]!r}"
        ) from e
    for track in report.get("tracks", []):
        if "path" in track:
            track["path"] = wsl_to_win_path(track["path"])

    if output_path is not None:
        # Rewrite the file with translated paths so external consumers see
        # Windows paths. Atomic so a crash mid-rewrite doesn't truncate the
        # report the user just waited 30+ min for.
        atomic_write_json(local_output, report, indent=2)
    else:
        # Clean up our temp file
        try:
            local_output.unlink()
        except OSError:
            pass

    return report


def _write_partial(output_path: Path, results: list[dict[str, Any]], total: int, in_progress: bool) -> None:
    # Atomic write so a crash mid-checkpoint doesn't truncate the report —
    # the whole point of writing partials every 50 tracks is crash recovery,
    # which a non-atomic write_text would defeat (a kill during the write
    # leaves a zero-byte/corrupt file).
    #
    # Checkpoints ALWAYS write status="in_progress" (the caller's flag is
    # deliberately ignored): the genuinely-final report — with the user's
    # configured genre policy + web lookup applied — is written by
    # analyze_directory right after the loop. Reconciling here with default
    # args used to stamp "complete" + default-policy genres on disk at the
    # last checkpoint, which a crash/cancel during the (potentially long)
    # web-lookup phase would then leave behind as a lying "complete" report.
    del in_progress
    atomic_write_json(
        Path(output_path),
        _build_report(results, total, in_progress=True),
        indent=2,
    )


def _record_artist_title(r: dict[str, Any]) -> tuple[str, str]:
    """Best artist/title for a record (tag first, then filename-parsed)."""
    et = r.get("existing_tags") or {}
    artist = (et.get("artist") or r.get("filename_artist") or "").strip()
    title = (et.get("title") or r.get("filename_title") or "").strip()
    return artist, title


def _reconcile_record_genre(
    r: dict[str, Any], policy: str, override_conf: float,
    web_cfg: dict[str, Any] | None = None,
    web_cache: dict[tuple[str, str], dict[str, Any]] | None = None,
) -> None:
    """Reconcile one record's ML genre with its existing tag, in place.

    Keeps the pure-audio read in `ml_genre_audio`/`ml_subgenre_audio` and sets
    `ml_genre`/`ml_subgenre` to the effective (reconciled) value plus
    `ml_genre_source` ("tag"|"ml"|"ml_override"|"web"|"web_override") and
    `ml_genre_conflict`. When `web_cfg` is enabled, the online web-synthesis
    resolver supplies a grounded genre (cached per artist+title). Idempotent:
    always re-derives from the stashed audio values. See genres.reconcile_genre +
    AnalysisConfig.genre_source_policy / genre_web_lookup.
    """
    ml = r.get("ml_analysis")
    if not ml:
        return
    audio_genre = ml.get("ml_genre_audio", ml.get("ml_genre"))
    audio_sub = ml.get("ml_subgenre_audio", ml.get("ml_subgenre"))
    if not audio_genre:
        return
    from vibechek.genres import reconcile_genre  # noqa: PLC0415

    tag = (r.get("existing_tags") or {}).get("genre")

    web_genre = ""
    web_grounded = False
    if web_cfg and web_cfg.get("enabled"):
        artist, title = _record_artist_title(r)
        key = (artist.lower(), title.lower())
        wr: dict[str, Any] | None = None
        if web_cache is not None and key in web_cache:
            wr = web_cache[key]
        elif artist and title:
            from vibechek import genre_web  # noqa: PLC0415
            wr = genre_web.resolve(
                artist, title, tag or "", audio_genre or "",
                backend=web_cfg.get("backend", "ollama"),
                model=web_cfg.get("model", genre_web.DEFAULT_MODEL),
            )
            if web_cache is not None:
                web_cache[key] = wr
        if wr:
            web_genre = wr.get("genre", "") or ""
            web_grounded = bool(wr.get("source_matched"))

    rec = reconcile_genre(
        audio_genre, audio_sub or "",
        ml.get("ml_genre_raw_confidence") or ml.get("ml_genre_confidence") or 0.0,
        tag, policy, override_conf,
        web_genre=web_genre, web_grounded=web_grounded,
    )
    ml["ml_genre_audio"] = audio_genre
    ml["ml_subgenre_audio"] = audio_sub
    if web_genre:
        ml["ml_genre_web"] = web_genre
        ml["ml_genre_web_grounded"] = web_grounded
    ml["ml_genre"] = rec.genre
    ml["ml_subgenre"] = rec.subgenre
    ml["ml_genre_source"] = rec.source
    ml["ml_genre_conflict"] = rec.conflict
    if rec.source != "ml":
        ml["ml_genre_confidence"] = round(rec.confidence, 3)


def _build_report(
    results: list[dict[str, Any]], total: int, in_progress: bool,
    genre_policy: tuple[str, float] = ("prefer_tag", 0.90),
    web_cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
    # Reconcile ML genre against existing tags on the FINAL report only (partial
    # checkpoints stay raw-ML — they're transient). _reconcile_record_genre is
    # idempotent, so the final pass produces the configured result regardless.
    if not in_progress:
        pol, override = genre_policy
        # Online web-synthesis genre lookup is per-track network+LLM I/O, so only
        # run it on the final report, cached per artist+title.
        web_cache: dict[tuple[str, str], dict[str, Any]] | None = (
            {} if (web_cfg and web_cfg.get("enabled")) else None
        )
        if web_cache is not None:
            from vibechek import genre_web  # noqa: PLC0415

            # The local LLM backend dies with the WSL VM (reboot / `wsl
            # --shutdown`); ensure_backend() restarts the managed Ollama if it
            # can. When it can't, SKIP the web tier loudly instead of paying a
            # wasted web search per track only to silently fall back anyway.
            if not genre_web.ensure_backend(web_cfg.get("backend", "ollama")):
                log.warning(
                    "Online genre lookup enabled but the local LLM backend is "
                    "not reachable — falling back to tags + audio only",
                )
                _emit_event(
                    "stage", name="genre_web_unavailable",
                    message="Online genre lookup unavailable (local LLM not "
                            "reachable) — using tags + audio only",
                )
                web_cache = None
                web_cfg = None
        n = len(results)
        for i, r in enumerate(results):
            # In-process cancel point (native/CLI path): the web tier can take
            # seconds per track, and even the offline reconcile shouldn't pin a
            # cancelled run.
            cancellation.check()
            if web_cache is not None and (i % 5 == 0 or i == n - 1):
                _emit_event("stage", name="resolving_genres_online",
                            message=f"Resolving genres online ({i + 1}/{n})…")
            _reconcile_record_genre(r, pol, override, web_cfg, web_cache)

    genres: dict[str, int] = defaultdict(int)
    energies: dict[int, int] = defaultdict(int)
    timeslots: dict[str, int] = defaultdict(int)
    moods: dict[str, int] = defaultdict(int)

    for r in results:
        ml = r.get("ml_analysis") or {}
        if ml.get("ml_genre"):
            genres[ml["ml_genre"]] += 1
        if ml.get("ml_energy") is not None:
            energies[ml["ml_energy"]] += 1
        if ml.get("ml_timeslot"):
            timeslots[ml["ml_timeslot"]] += 1
        if ml.get("ml_mood"):
            moods[ml["ml_mood"]] += 1

    return {
        "status": "in_progress" if in_progress else "complete",
        "summary": {
            "total_files": total,
            "analyzed": sum(1 for r in results if r.get("ml_analysis")),
            "errors": sum(1 for r in results if r.get("error")),
        },
        "statistics": {
            "genres": dict(sorted(genres.items(), key=lambda x: -x[1])),
            "energy_distribution": dict(sorted(energies.items())),
            "timeslot_distribution": dict(timeslots),
            "mood_distribution": dict(moods),
        },
        "tracks": results,
    }


__all__ = [
    "MLResult",
    "TrackAnalysis",
    "MODELS",
    "MODEL_SHA256",
    "download_models",
    "load_models",
    "analyze_audio_features",
    "analyze_track",
    "analyze_directory",
    "verify_model_sha256",
]
