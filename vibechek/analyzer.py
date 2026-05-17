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

import json
import logging
import os
import urllib.request
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from multiprocessing import Pool, cpu_count
from pathlib import Path
from typing import Any

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

MODEL_BASE_URL = "https://essentia.upf.edu/models"

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
    """The full per-file record written to analysis.json."""

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


# ---------------------------------------------------------------------------
# Model download / load
# ---------------------------------------------------------------------------


def download_models(
    model_dir: Path,
    on_progress: ProgressCallback | None = None,
) -> dict[str, dict[str, Any]]:
    """Download missing models. Returns descriptors for each (paths + class labels)."""
    model_dir = Path(model_dir)
    model_dir.mkdir(parents=True, exist_ok=True)
    descriptors: dict[str, dict[str, Any]] = {}

    items = list(MODELS.items())
    for i, (name, (subdir, weights_name, metadata_name)) in enumerate(items):
        report_progress(on_progress, i + 1, len(items), name)

        weights_path = model_dir / f"{name}.pb"
        metadata_path = model_dir / f"{name}.json"

        if not weights_path.exists():
            url = f"{MODEL_BASE_URL}/{subdir}/{weights_name}"
            log.info("Downloading %s weights from %s", name, url)
            try:
                urllib.request.urlretrieve(url, weights_path)
            except Exception as e:  # noqa: BLE001
                log.warning("Could not download %s: %s", name, e)
                continue

        if not metadata_path.exists():
            url = f"{MODEL_BASE_URL}/{subdir}/{metadata_name}"
            try:
                urllib.request.urlretrieve(url, metadata_path)
            except Exception as e:  # noqa: BLE001
                log.warning("Could not download %s metadata: %s", name, e)

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

    return descriptors


def load_models(model_dir: Path, use_gpu: str = "auto") -> dict[str, Any]:
    """Instantiate Essentia model wrappers. Raises if essentia isn't installed.

    `use_gpu` is forwarded to apply_gpu_preference BEFORE the essentia/tensorflow
    import — this is the only point where CUDA_VISIBLE_DEVICES can still affect
    TF's device enumeration.
    """
    apply_gpu_preference(use_gpu)

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

    embeddings = models["effnet"](audio_16k)

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

    if mood_scores:
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
    global _WORKER_MODELS
    _WORKER_MODELS = load_models(Path(model_dir), use_gpu=use_gpu)


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
    from vibechek.preflight import preflight  # noqa: PLC0415

    pf = preflight(config.models_dir)
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

    file_strs = [str(f) for f in files]
    results: list[dict[str, Any]] = []

    # Resolve "auto" worker count: leave one core for the OS/GUI.
    requested = config.workers if config.workers and config.workers > 0 else max(1, cpu_count() - 1)
    workers = max(1, min(requested, cpu_count()))
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
        with Pool(
            processes=workers,
            initializer=_worker_init,
            initargs=(str(config.models_dir), config.use_gpu),
        ) as pool:
            for i, record in enumerate(pool.imap_unordered(_worker_analyze, file_strs)):
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

    workers = config.workers if config.workers and config.workers > 0 else 0

    args = [
        "analyze", wsl_input,
        "-o", wsl_output,
        "--gpu", config.use_gpu,
    ]
    if workers > 0:
        args += ["--workers", str(workers)]
    if skip:
        args += ["--skip", str(skip)]
    if limit:
        args += ["--limit", str(limit)]

    # Progress lines from `vibechek analyze` look like "Progress: 50/12000 ..."
    # We re-emit them as JSON-RPC notifications for the GUI.
    progress_re = re.compile(r"(\d+)\s*/\s*(\d+)")

    def on_line(line: str) -> None:
        if not line:
            return
        m = progress_re.search(line)
        if m and on_progress:
            on_progress(int(m.group(1)), int(m.group(2)), line[:80])

    result = run_vibechek_in_wsl(distro, args, on_stderr_line=on_line)

    if result.returncode != 0:
        raise RuntimeError(
            f"vibechek analyze inside WSL ({distro}) exited with "
            f"{result.returncode}. stdout tail:\n{result.stdout[-1500:]}"
        )

    # Read the analysis.json the WSL side wrote and rewrite paths
    if not local_output.exists():
        raise RuntimeError(
            f"WSL analyze finished but no output file at {local_output}"
        )

    report = _json.loads(local_output.read_text(encoding="utf-8"))
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
    "download_models",
    "load_models",
    "analyze_audio_features",
    "analyze_track",
    "analyze_directory",
]
