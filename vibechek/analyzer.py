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
import os
import urllib.request
from collections import defaultdict
from dataclasses import asdict, dataclass, field
import multiprocessing
from multiprocessing import cpu_count
from pathlib import Path
from typing import Any, Callable

from mutagen import File as MutagenFile  # noqa: N812 (mutagen's API)
from mutagen.flac import FLAC
from mutagen.id3 import ID3

from vibechek import cancellation
from vibechek.config import AnalysisConfig
from vibechek.filename import extract_from_filename
from vibechek.genres import GenreResult, get_best_genre
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
# https://github.com/papapew/Vibechek/releases/download/models-v1/ and bump
# the tag below.
_DEFAULT_MODEL_BASE_URLS = (
    "https://essentia.upf.edu/models",
    "https://github.com/papapew/Vibechek/releases/download/models-v1",
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
    """
    model_dir = Path(model_dir)
    model_dir.mkdir(parents=True, exist_ok=True)
    descriptors: dict[str, dict[str, Any]] = {}
    errors: list[str] = []

    # Two parts per model (weights + metadata) so the overall progress bar
    # has 2*N steps. We emit bytes-within-current-file as a fractional step
    # for smooth UX during big downloads.
    items = list(MODELS.items())
    total_steps = len(items) * 2

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
        if _needs_download(weights_path, weights_urls[0]):
            try:
                _download_from_mirrors(
                    weights_urls,
                    weights_path,
                    label=f"{name}.pb",
                    on_progress=lambda done, total, n=name: emit(
                        weights_step, (done, total), f"{n} weights ({_fmt_bytes(done)}/{_fmt_bytes(total)})"
                    ),
                )
            except Exception as e:  # noqa: BLE001
                log.error("Failed to download %s weights: %s", name, e)
                errors.append(f"{name}.pb: {e}")
                # Clean up any partial file so a retry doesn't think it's done
                weights_path.unlink(missing_ok=True)
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
        if _needs_download(metadata_path, metadata_urls[0]):
            try:
                _download_from_mirrors(
                    metadata_urls,
                    metadata_path,
                    label=f"{name}.json",
                    on_progress=lambda done, total, n=name: emit(
                        metadata_step, (done, total), f"{n} metadata"
                    ),
                )
            except Exception as e:  # noqa: BLE001
                log.error("Failed to download %s metadata: %s", name, e)
                errors.append(f"{name}.json: {e}")
                metadata_path.unlink(missing_ok=True)
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


def _needs_download(path: Path, url: str) -> bool:
    """True if `path` is missing OR clearly truncated relative to `url`.

    *Important*: if the HEAD probe fails (network error, DNS, server down),
    we now trust the local file only if it passes a basic sanity check
    (>100KB for .pb, >200B for .json). A failed HEAD used to silently
    accept ANY local file, which meant a previous run that wrote a 50KB
    "Service Unavailable" HTML page would sit there forever — every
    install reporting "models OK" while analyze blew up at runtime.
    """
    if not path.exists():
        return True

    # Sanity check the local file size FIRST — fast and doesn't need network.
    min_size = 200 if path.suffix == ".json" else 100_000
    local_size = path.stat().st_size
    if local_size < min_size:
        log.warning(
            "Local %s is %d bytes — too small to be a real model file (min %d). Refetching.",
            path.name, local_size, min_size,
        )
        return True

    try:
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req, timeout=10) as resp:
            expected = int(resp.headers.get("Content-Length") or 0)
    except Exception as e:  # noqa: BLE001
        # Network blip: keep the file IF it passed the size sanity check above.
        # We DON'T trust a download that never finished — that's caught by the
        # size check. We do trust a complete previous download.
        log.info(
            "HEAD probe failed for %s (%s) — using existing %d-byte local file (passed size sanity check).",
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
    (audit #21) hands off to the GitHub Release mirror, then to any
    user-configured VIBECHEK_MODELS_URL, without the user noticing.

    Raises RuntimeError listing every mirror's last error if all fail.
    """
    if not urls:
        raise ValueError("No mirror URLs provided")

    mirror_errors: list[str] = []
    for url in urls:
        try:
            _download_with_progress(
                url, dest, label,
                on_progress=on_progress, chunk_size=chunk_size,
                max_attempts=max_attempts_per_mirror,
            )
            return  # success
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
    import socket as _socket
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
        except (urllib.error.URLError, _socket.timeout, ConnectionResetError,
                TimeoutError, OSError) as e:
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
            min_size = 200 if dest.suffix == ".json" else 100_000
            if bytes_done < min_size:
                raise RuntimeError(
                    f"unexpectedly small file ({bytes_done} bytes) — likely an error page"
                )

            tmp_dest.replace(dest)
        except Exception:
            tmp_dest.unlink(missing_ok=True)
            raise


def load_models(model_dir: Path, use_gpu: str = "auto") -> dict[str, Any]:
    """Instantiate Essentia model wrappers. Raises if essentia isn't installed.

    `use_gpu` is forwarded to apply_gpu_preference BEFORE the essentia/tensorflow
    import — this is the only point where CUDA_VISIBLE_DEVICES can still affect
    TF's device enumeration.
    """
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

    return loaded


# ---------------------------------------------------------------------------
# Audio feature extraction
# ---------------------------------------------------------------------------


def _classify_vocal(score: float) -> str:
    if score < 0.3:
        return "Instrumental"
    if score < 0.6:
        return "Light Vocal"
    return "Vocal"


def _classify_mood(brightness: float) -> str:
    if brightness < 0.4:
        return "Dark"
    if brightness > 0.6:
        return "Bright"
    return "Neutral"


def _pick_timeslot(genre: str | None, energy: int, bpm: float | None) -> str:
    """Map (genre, energy, BPM) → DJ set timeslot label."""
    if genre in ("Ambient", "Downtempo", "Chillout", "Trip Hop"):
        return "Opener" if energy <= 2 else "Afterhours"
    if genre in ("Hardcore", "Gabber", "Hardstyle", "Hard Techno"):
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
    if "genre" in models and "genre_classes" in models:
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
        key, scale, _strength = KeyExtractor()(audio_44k)
        result.ml_key = key_to_camelot(f"{key} {scale}")
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
            voice_score = float(pred[1]) if len(pred) > 1 else float(pred[0])
            result.ml_vocal = _classify_vocal(voice_score)
        except Exception as e:  # noqa: BLE001
            log.debug("Vocal detection failed for %s: %s", filepath.name, e)
            result.ml_vocal = "Unknown"

    # ---------- Mood / energy ----------
    mood_scores: dict[str, float] = {}
    for mood in ("aggressive", "happy", "relaxed", "sad"):
        if mood not in models:
            continue
        try:
            pred = np.mean(models[mood](embeddings), axis=0)
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
        # Genre-based fallback when mood models failed
        genre = result.ml_genre or ""
        if genre in ("Techno", "Hard Techno", "Industrial", "EBM", "Hardcore", "Gabber"):
            result.ml_energy, result.ml_mood = 4, "Dark"
        elif genre in ("Deep House", "Ambient", "Downtempo", "Chillout"):
            result.ml_energy, result.ml_mood = 2, "Neutral"
        elif genre in ("Trance", "Psytrance", "Happy Hardcore", "Eurodance"):
            result.ml_energy, result.ml_mood = 4, "Bright"
        else:
            result.ml_energy, result.ml_mood = 3, "Neutral"

    result.ml_timeslot = _pick_timeslot(result.ml_genre, result.ml_energy or 3, result.ml_bpm)

    # ---------- Direction (energy curve over the track) ----------
    try:
        if "aggressive" in models:
            third = len(embeddings) // 3
            if third > 0:
                pred_full = models["aggressive"](embeddings)
                start_e = float(np.mean(pred_full[:third]))
                end_e = float(np.mean(pred_full[-third:]))
                diff = end_e - start_e
                if diff > 0.08:
                    result.ml_direction = "Up"
                elif diff < -0.08:
                    result.ml_direction = "Down"
                else:
                    result.ml_direction = "Steady"
            else:
                result.ml_direction = "Steady"
        else:
            result.ml_direction = "Steady"
    except Exception:  # noqa: BLE001
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
        record.ml_analysis = {k: v for k, v in asdict(ml).items() if v is not None}

    return record


# ---------------------------------------------------------------------------
# Parallel runner
# ---------------------------------------------------------------------------

# Worker-local model cache. multiprocessing.Pool initializer populates this
# per worker process; the worker function then reuses it across files.
_WORKER_MODELS: dict[str, Any] | None = None


def _worker_init(model_dir: str, use_gpu: str) -> None:
    """multiprocessing.Pool initializer. If load_models raises, multiprocessing
    silently restarts the worker in an infinite loop — masking real errors
    like OOM, missing essentia install, or corrupted model files behind a
    forever hang.

    We wrap load_models so init errors crash the worker FAST and visibly:
      - log the traceback to stderr (will end up in the WSL stderr stream
        the parent captures)
      - re-raise so the pool sees the worker as broken; we set
        maxtasksperchild=1 in the Pool call so the bad init doesn't auto-respawn
    """
    global _WORKER_MODELS
    try:
        _WORKER_MODELS = load_models(Path(model_dir), use_gpu=use_gpu)
    except Exception as e:  # noqa: BLE001
        import sys as _sys
        import traceback as _tb
        _sys.stderr.write(
            f"VIBECHEK_WORKER_INIT_FAIL: {type(e).__name__}: {e}\n"
            f"{_tb.format_exc()}\n"
        )
        _sys.stderr.flush()
        # Re-raise so the pool marks this worker as broken. Combined with
        # maxtasksperchild=1, this triggers a single retry; if it fails again,
        # the second attempt's exception kills the pool cleanly instead of
        # hanging forever.
        raise


# Per-worker GPU memory budget. ~1.5 GB covers a TF/essentia worker:
# the bundled Discogs-EffNet weights (~500 MB) plus per-worker activation
# tensors and a CUDA context (~800-1000 MB at steady state). Tuned against
# 4 GB / 8 GB / 24 GB consumer cards; below 1500 we see OOM cascades.
_GPU_WORKER_MB = 1500

# Fallback cap when VRAM probing fails — preserves prior behaviour of
# "at most 4 workers in GPU mode" so we never regress on systems where
# nvidia-smi is missing or unreadable (e.g. WSL with no GPU passthrough).
_GPU_FALLBACK_CAP = 4


def _probe_free_vram_mb() -> int | None:
    """Return free VRAM in MB across all visible GPUs via `nvidia-smi`, or None.

    We sum free memory across devices because TF with the default
    `CUDA_VISIBLE_DEVICES` picks GPU 0; if the user has a multi-GPU rig and
    GPU 0 is busy (e.g. driving the display), summing slightly overestimates.
    That's OK — the cap is a *ceiling* anchored by `_GPU_FALLBACK_CAP` below
    and the user's `workers` setting above. We err toward more workers than
    fewer on multi-GPU rigs; OOM still surfaces via the stall watchdog.

    Returns None on any failure (binary missing, timeout, parse error). Callers
    must treat None as "unknown" and fall back to the conservative cap.
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

    total_free = 0
    saw_any = False
    for line in out.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            total_free += int(line)
            saw_any = True
        except ValueError:
            # A malformed row (rare) shouldn't poison the whole probe — skip it.
            continue
    return total_free if saw_any else None


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


def analyze_directory(
    library_path: Path,
    config: AnalysisConfig | None = None,
    on_progress: ProgressCallback | None = None,
    output_path: Path | None = None,
    skip: int = 0,
    limit: int | None = None,
    skip_paths: set[str] | None = None,
) -> dict[str, Any]:
    """Analyze every audio file under `library_path`.

    Writes an incremental JSON report to `output_path` (if provided) every 50
    tracks so a crash doesn't lose all progress. Uses `config.workers` parallel
    processes when >1.

    When `skip_paths` is provided, files whose absolute string path is in the
    set are skipped — used by the GUI's incremental "analyze new tracks only"
    flow so re-runs don't re-process the whole library.
    """
    if config is None:
        config = AnalysisConfig()

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

    pf = preflight(config.models_dir, quick_wsl=False)
    if not pf.ready:
        raise RuntimeError(
            "Cannot analyze: " + "; ".join(pf.reasons_not_ready) +
            ". Run `vibechek preflight` (or check Settings in the GUI) "
            "for actionable instructions."
        )

    # If native essentia is missing but WSL has it, route through WSL transparently.
    if pf.analyze_via == "wsl":
        return _analyze_via_wsl(
            library_path, config, on_progress, output_path, skip, limit,
            distro=pf.wsl.usable_distro,  # type: ignore[union-attr]
        )

    # If native essentia is missing but the managed Linux/macOS venv has it,
    # route through that venv. Mirrors the WSL path; no path translation needed.
    if pf.analyze_via == "native_venv":
        return _analyze_via_native_venv(
            library_path, config, on_progress, output_path, skip, limit,
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
        # Reserve 2 GB for the host; assume ~800 MB per worker for models + TF.
        usable_mb = max(0, total_mb - 2048)
        memory_cap = max(1, usable_mb // 800)
        if memory_cap < workers:
            log.warning(
                "Capping workers from %d -> %d based on available RAM "
                "(%d MB total, ~800 MB per worker)",
                workers, memory_cap, total_mb,
            )
            workers = memory_cap
    except ImportError:
        pass  # psutil missing — fall through with the cpu_count-based number

    # GPU contention cap — VRAM-aware (audit #11).
    #
    # Old behaviour: hardcoded `workers = 4`, regardless of card. That OOM-kills
    # 4 GB cards (4 workers * 1.5 GB = 6 GB needed) and wastes 24 GB cards
    # (4 workers leaves 18 GB idle). Now we probe free VRAM and pick
    # max(1, free_mb // _GPU_WORKER_MB), falling back to the old cap of 4 if
    # the probe fails (no nvidia-smi, WSL without GPU passthrough, etc).
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
                "GPU mode active but VRAM probe failed — capping workers at %d. "
                "Set workers explicitly in Settings if your GPU can handle more.",
                _GPU_FALLBACK_CAP,
            )
        if gpu_cap < workers:
            msg = (
                f"Capped workers from {workers} to {gpu_cap} due to {cap_reason}"
            )
            log.warning(msg)
            # Surface to the GUI: log.warning alone is invisible to users.
            # report_progress swallows exceptions, so a flaky callback won't
            # crash the analyze. We send total=total so the GUI's progress bar
            # state is unchanged — this is purely a status message piggybacked
            # on the progress channel (current=0 means "not yet started").
            report_progress(on_progress, 0, total, msg)
            workers = gpu_cap

    log.info("Analyzing %d files with %d worker(s), GPU=%s", total, workers, config.use_gpu)

    if workers == 1:
        models = load_models(config.models_dir, use_gpu=config.use_gpu)
        for i, filepath in enumerate(files):
            cancellation.check()  # Raises CancelledError if user clicked Cancel
            report_progress(on_progress, i + 1, total, filepath.name)
            try:
                results.append(asdict(analyze_track(filepath, models)))
            except Exception as e:  # noqa: BLE001
                results.append({
                    "path": str(filepath),
                    "filename": filepath.name,
                    "extension": filepath.suffix.lower(),
                    "size_mb": 0.0,
                    "error": str(e),
                })
            if output_path and ((i + 1) % 50 == 0 or (i + 1) == total):
                _write_partial(output_path, results, total, in_progress=(i + 1) < total)
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
        spawn_ctx = multiprocessing.get_context("spawn")
        # maxtasksperchild=200: every 200 tracks the worker recycles, freeing
        # any TF memory leaks that essentia / TF native code might accumulate.
        # Cheap; one re-init per ~200 tracks is invisible alongside analysis.
        with spawn_ctx.Pool(
            processes=workers,
            initializer=_worker_init,
            initargs=(str(config.models_dir), config.use_gpu),
            maxtasksperchild=200,
        ) as pool:
            # Stall watchdog: if no result arrives in STALL_TIMEOUT seconds, the
            # pool is wedged (workers all crashed during init, or all OOM-killed).
            # Tear down with a useful error instead of hanging until the RPC
            # timeout (1 hour) cuts us off.
            import time as _time
            STALL_TIMEOUT = 300  # 5 minutes between any two results = dead
            last_result_at = _time.monotonic()
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
                            )
                        if cancellation.is_cancelled():
                            raise cancellation.CancelledError(
                                "Analysis cancelled by user"
                            )

            for i in range(total):
                try:
                    record = _next_with_stall_check()
                except cancellation.CancelledError:
                    pool.terminate()
                    pool.join()
                    raise
                except StopIteration:
                    break
                last_result_at = _time.monotonic()
                _ = last_result_at  # silence pyflakes; intentional touch
                # On cancel: terminate the pool (kills outstanding workers) and bail.
                if cancellation.is_cancelled():
                    pool.terminate()
                    pool.join()
                    raise cancellation.CancelledError("Analysis cancelled by user")
                results.append(record)
                report_progress(on_progress, i + 1, total, Path(record.get("path", "")).name)
                if output_path and ((i + 1) % 50 == 0 or (i + 1) == total):
                    _write_partial(output_path, results, total, in_progress=(i + 1) < total)

    report = _build_report(results, total, in_progress=False)
    if output_path:
        Path(output_path).write_text(
            json.dumps(report, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    return report


def _analyze_via_native_venv(
    library_path: Path,
    config: AnalysisConfig,
    on_progress: ProgressCallback | None,
    output_path: Path | None,
    skip: int,
    limit: int | None,
) -> dict[str, Any]:
    """Route analyze to the managed `~/.vibechek/venv/bin/vibechek` on Linux/macOS.

    No path translation needed (unlike `_analyze_via_wsl`) — the venv runs in
    the host filesystem. We just shell out, stream progress, and read the
    result JSON back.
    """
    import json as _json
    import re
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
    ]
    if workers > 0:
        args += ["--workers", str(workers)]
    if skip:
        args += ["--skip", str(skip)]
    if limit:
        args += ["--limit", str(limit)]

    progress_re = re.compile(r"(\d+)\s*/\s*(\d+)")

    def on_line(line: str) -> None:
        if not line:
            return
        m = progress_re.search(line)
        if m and on_progress:
            on_progress(int(m.group(1)), int(m.group(2)), line[:80])

    result = run_vibechek_in_native_venv(args, on_stderr_line=on_line)

    if result.returncode != 0:
        raise RuntimeError(
            f"vibechek analyze in managed venv exited with "
            f"{result.returncode}. stdout tail:\n{result.stdout[-1500:]}"
        )

    if not local_output.exists():
        raise RuntimeError(
            f"managed-venv analyze finished but no output file at {local_output}"
        )

    report = _json.loads(local_output.read_text(encoding="utf-8"))

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
    ]
    if workers > 0:
        args += ["--workers", str(workers)]
    if skip:
        args += ["--skip", str(skip)]
    if limit:
        args += ["--limit", str(limit)]

    # Progress lines from `vibechek analyze` look like "Progress: 50/12000 ...".
    # We re-emit them as JSON-RPC notifications. Every stderr line is ALSO
    # captured into a bounded buffer so a failure exit can show what went
    # wrong (previously we just got "exited with 1" and empty stdout —
    # useless for diagnosis).
    progress_re = re.compile(r"(\d+)\s*/\s*(\d+)")
    stderr_tail: list[str] = []

    def on_line(line: str) -> None:
        # Keep the last 80 stderr lines for the error message.
        stderr_tail.append(line)
        if len(stderr_tail) > 80:
            stderr_tail.pop(0)
        if not line:
            return
        m = progress_re.search(line)
        if m and on_progress:
            on_progress(int(m.group(1)), int(m.group(2)), line[:80])

    result = run_vibechek_in_wsl(distro, args, on_stderr_line=on_line)

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

    try:
        report = _json.loads(local_output.read_text(encoding="utf-8"))
    except _json.JSONDecodeError as e:
        raise RuntimeError(
            f"WSL analyze ({distro}) wrote {local_output.stat().st_size} bytes "
            f"to {local_output} but they don't parse as JSON: {e}. "
            f"First 200 bytes: {local_output.read_text(encoding='utf-8', errors='replace')[:200]!r}"
        ) from e
    for track in report.get("tracks", []):
        if "path" in track:
            track["path"] = wsl_to_win_path(track["path"])

    if output_path is not None:
        # Rewrite the file with translated paths so external consumers see Windows paths
        local_output.write_text(
            _json.dumps(report, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    else:
        # Clean up our temp file
        try:
            local_output.unlink()
        except OSError:
            pass

    return report


def _write_partial(output_path: Path, results: list[dict[str, Any]], total: int, in_progress: bool) -> None:
    Path(output_path).write_text(
        json.dumps(_build_report(results, total, in_progress), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def _build_report(results: list[dict[str, Any]], total: int, in_progress: bool) -> dict[str, Any]:
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
