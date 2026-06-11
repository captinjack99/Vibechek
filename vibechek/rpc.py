"""JSON-RPC server for the desktop sidecar.

The Tauri shell spawns `vibechek rpc` as a child process and communicates with
it via line-delimited JSON-RPC 2.0 over stdin/stdout. Long-running operations
emit `progress` notifications during execution.

Protocol (one JSON object per line):

    Request:       {"jsonrpc":"2.0","id":1,"method":"dedupe","params":{"path":"..."}}
    Response:      {"jsonrpc":"2.0","id":1,"result":{...}}
    Error:         {"jsonrpc":"2.0","id":1,"error":{"code":-32000,"message":"..."}}
    Notification:  {"jsonrpc":"2.0","method":"progress","params":{"current":50,"total":100,"message":"..."}}

Long-op requests MAY carry a client-generated `op_id` string in params. It is
stripped before the handler runs and echoed back — together with the op's
`kind` — on every `progress` / `track_analyzed` notification emitted while the
op runs, so the GUI can attribute events on the shared notification stream to
the exact operation instance that produced them. Both fields are omitted when
unknown (CLI / legacy clients), which consumers must treat as "match anything".

stdout is reserved for protocol traffic only. All logging goes to stderr.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import sys
import threading
import traceback
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from vibechek import __version__, cancellation
from vibechek.config import (
    AnalysisConfig,
    DuplicateConfig,
    OrganizationConfig,
    TaggingConfig,
    VibechekConfig,
)

log = logging.getLogger(__name__)

# How many requests can be processed in parallel. Long ops are still gated
# by the cancellation singleton (one at a time), but quick reads (config,
# system_info, preflight) can interleave so the GUI stays responsive.
_DISPATCH_WORKERS = 8

# Held while we check-and-reserve the cancellation singleton — see _dispatch.
# Without this, two cancellable RPCs landing on the thread pool simultaneously
# both pass the `is current_kind() None` check, both call `begin(kind)`, and
# the second clobbers the first's `_current_kind` entry.
_LONG_OP_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Write-path parameter validation
# ---------------------------------------------------------------------------
#
# The CLI clamps numeric inputs via click's FloatRange/IntRange; the RPC
# boundary historically did not, so a GUI bug or a hand-crafted request could
# push out-of-range values straight into the tagger/analyzer. A `0.0` (or
# negative) confidence threshold over-tags the WHOLE library; an inverted vocal
# band misclassifies every track; a negative worker/skip count goes downstream
# undefined; an `id3_text_encoding` outside {0,1,2,3} writes a corrupt encoding
# byte into MP3 frames (or raises per-file). These tiny helpers re-establish the
# same guarantees the CLI already gives, at the RPC seam.

# Encodings mutagen's ID3 frame constructors accept: 0=ISO-8859-1, 1=UTF-16,
# 2=UTF-16BE, 3=UTF-8. Anything else is corrupt; fall back to UTF-8 (3).
_VALID_ID3_ENCODINGS = frozenset({0, 1, 2, 3})

# Distro names we'll let through to the elevated PowerShell installer. Matches
# the WSL-side allowlist; validated centrally here so a malicious/garbled
# `distro` can't escape the literal and run arbitrary PowerShell behind UAC.
import re as _re

_DISTRO_RE = _re.compile(r"^[A-Za-z0-9._-]+$")


def _clamp01(value: Any, default: float) -> float:
    """Coerce `value` to a float in [0, 1]; fall back to `default` on garbage."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    if v != v:  # NaN
        return default
    return min(1.0, max(0.0, v))


def _nonneg_int(value: Any, default: int = 0) -> int:
    """Coerce `value` to an int >= 0; fall back to `default` on garbage."""
    try:
        v = int(value)
    except (TypeError, ValueError):
        return default
    return v if v >= 0 else 0


def _valid_id3_encoding(value: Any, default: int = 3) -> int:
    """Return `value` if it's a valid ID3 text encoding {0,1,2,3}, else `default`."""
    try:
        v = int(value)
    except (TypeError, ValueError):
        return default
    return v if v in _VALID_ID3_ENCODINGS else default


def _validate_distro(value: Any, default: str = "Ubuntu-24.04") -> str:
    """Validate a WSL distro name against the safe charset.

    Raises ValueError (→ INVALID_PARAMS at the dispatch seam) on anything that
    could break out of the PowerShell command string. An empty/None value falls
    back to the default distro.
    """
    if value is None or value == "":
        return default
    s = str(value)
    if not _DISTRO_RE.match(s):
        raise ValueError(
            f"Invalid distro name {s!r}: must match {_DISTRO_RE.pattern}"
        )
    return s


# ---------------------------------------------------------------------------
# Wire format
# ---------------------------------------------------------------------------

# Single shared writer. Concurrent handler threads write through this; the
# lock guarantees each JSON line lands atomically on stdout.
class _StdoutWriter:
    def __init__(self, stream):
        self.stream = stream
        self.lock = threading.Lock()

    def write(self, msg: dict[str, Any]) -> None:
        line = json.dumps(msg, default=_json_default) + "\n"
        with self.lock:
            self.stream.write(line)
            self.stream.flush()


# Bootstrapped on serve(); used by all the helpers below.
_writer: _StdoutWriter | None = None


def _write_message(msg: dict[str, Any]) -> None:
    """Write a single JSON-RPC message to stdout, flushed immediately."""
    if _writer is not None:
        _writer.write(msg)
    else:
        # Fallback for tests that import these helpers without calling serve()
        sys.stdout.write(json.dumps(msg, default=_json_default) + "\n")
        sys.stdout.flush()


def _json_default(o: Any) -> Any:
    """Make Path, dataclass, and Enum values JSON-serializable."""
    if isinstance(o, Path):
        return str(o)
    if is_dataclass(o):
        return asdict(o)
    if hasattr(o, "value"):  # IntEnum / StrEnum
        return o.value
    raise TypeError(f"{type(o).__name__} is not JSON serializable")


# Throttle state for _emit_progress. Long ops (analyze) can emit thousands of
# notifications per second; if the Tauri stdout reader falls behind (frontend
# unfocused / OS context switch), the pipe buffer fills and
# `_StdoutWriter.write` blocks the worker thread, stalling the whole analyze.
_LAST_PROGRESS_TIME = 0.0
_PROGRESS_MIN_INTERVAL_SEC = 0.05  # 20/sec cap
_PROGRESS_LOCK = threading.Lock()


def _reset_progress_throttle() -> None:
    """Clear the throttle clock so the first tick of a new long op always lands.

    `_LAST_PROGRESS_TIME` is module-global and shared across ops; without a
    reset, a long op that starts within 50 ms of the previous op's last tick has
    its own first progress frame swallowed by the rate limiter (cosmetic, but it
    makes back-to-back ops look momentarily stalled). Called once when a
    cancellable long op is reserved.
    """
    global _LAST_PROGRESS_TIME
    with _PROGRESS_LOCK:
        _LAST_PROGRESS_TIME = 0.0


def _emit_progress(current: int, total: int, message: str = "") -> None:
    """Send a progress notification, throttled to ~20/sec.

    The final tick (current == total) is always allowed through even if
    rate-limited, so the UI sees completion.
    """
    import time as _time
    global _LAST_PROGRESS_TIME
    now = _time.monotonic()
    is_final = total > 0 and current >= total
    with _PROGRESS_LOCK:
        if not is_final and (now - _LAST_PROGRESS_TIME) < _PROGRESS_MIN_INTERVAL_SEC:
            return  # rate-limited
        _LAST_PROGRESS_TIME = now

    params: dict[str, Any] = {"current": current, "total": total, "message": message}
    kind, op_id = cancellation.current_op()
    if kind is not None:
        params["kind"] = kind
    if op_id is not None:
        params["op_id"] = op_id
    _write_message({"jsonrpc": "2.0", "method": "progress", "params": params})


def _emit_track_analyzed(record: dict, current: int, total: int) -> None:
    """Stream a single track's ML record to the GUI as it completes.

    The frontend's LibraryBrowser subscribes to `sidecar:track_analyzed`
    events and merges incoming records into `useLibraryStore.tracks` in
    real time, so the user sees their library populate live instead of
    waiting for the whole analyze batch to finish. We deliberately do NOT
    rate-limit this — the per-track stream is naturally bounded (workers *
    track-rate ≈ a few events / sec on typical hardware), and each event
    is a unique record we'd lose to a "drop one per N" throttle. Progress
    bars stay throttled via _emit_progress; this channel is purely additive.
    """
    params: dict[str, Any] = {"current": current, "total": total, "track": record}
    kind, op_id = cancellation.current_op()
    if kind is not None:
        params["kind"] = kind
    if op_id is not None:
        params["op_id"] = op_id
    _write_message({"jsonrpc": "2.0", "method": "track_analyzed", "params": params})


def _ok(req_id: Any, result: Any) -> None:
    _write_message({"jsonrpc": "2.0", "id": req_id, "result": result})


def _err(req_id: Any, code: int, message: str, data: Any = None) -> None:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    _write_message({"jsonrpc": "2.0", "id": req_id, "error": error})


# ---------------------------------------------------------------------------
# Method handlers
# ---------------------------------------------------------------------------


def _ping(_params: dict) -> dict:
    return {"pong": True, "version": __version__}


def _version(_params: dict) -> dict:
    return {"version": __version__}


def _scan_directory(params: dict) -> dict:
    """List audio files under params['path']. No ML."""
    from vibechek.utils import find_audio_files

    path = Path(params["path"])
    recursive = bool(params.get("recursive", True))
    files = find_audio_files(path, recursive=recursive)
    return {
        "count": len(files),
        "files": [
            {
                "path": str(p),
                "filename": p.name,
                "extension": p.suffix.lower(),
                "size_mb": round(p.stat().st_size / (1024 * 1024), 2),
            }
            for p in files
        ],
    }


def _scan_only(params: dict) -> dict:
    """Lightweight library load: returns TrackAnalysis-shaped records with
    filename-parsed hints + existing tags, but no ML analysis.

    Lets the user see + browse their library instantly without waiting for
    the full ML run. They can analyze later — or never, and just use Vibechek
    for dedupe / organize / tag-backup workflows.
    """
    from dataclasses import asdict

    from vibechek.analyzer import analyze_track
    from vibechek.utils import find_audio_files

    path = Path(params["path"])
    recursive = bool(params.get("recursive", True))
    files = find_audio_files(path, recursive=recursive)
    total = len(files)
    tracks: list[dict] = []
    for i, fp in enumerate(files):
        _emit_progress(i + 1, total, fp.name)
        try:
            # Pass models=None so analyze_track only does the cheap pass
            tracks.append(asdict(analyze_track(fp, models=None)))
        except Exception as e:  # noqa: BLE001
            tracks.append({
                "path": str(fp),
                "filename": fp.name,
                "extension": fp.suffix.lower(),
                "size_mb": 0.0,
                "error": str(e),
            })
    return {
        "status": "complete",
        "summary": {"total_files": total, "analyzed": 0, "errors": sum(1 for t in tracks if t.get("error"))},
        "statistics": {},
        "tracks": tracks,
    }


def _analyze_directory(params: dict) -> dict:
    """Full ML analysis. Emits progress notifications.

    Supports incremental runs via `skip_paths` (list of absolute paths already
    analyzed). The GUI uses this for the 'Analyze new tracks only' button.

    Auto-persists the result to `<data_dir>/analyses/...` and updates the
    recent-libraries index unless `auto_save=False` is passed (e.g. for
    one-off CLI runs that already specify their own --output).
    """
    from vibechek import library_state
    from vibechek.analyzer import analyze_directory

    config = AnalysisConfig(
        # workers >= 0 (0 means "auto" downstream); a negative count is
        # undefined to the pool sizing math.
        workers=_nonneg_int(params.get("workers", 0), 0),
        use_gpu=str(params.get("use_gpu", "auto")),
        hybrid_cpu_gpu=bool(params.get("hybrid_cpu_gpu", True)),
        # Critical: route analyze to the SELECTED engine. Without this the
        # config defaults to essentia_tf, so the ONNX toggle is inert at
        # analyze time (and an onnx-only install fails: wrong venv). The GUI
        # sends this from the config store; default keeps the TF path.
        inference_engine=_valid_engine(params.get("inference_engine")),
        # Existing-tag vs ML genre reconciliation policy (see AnalysisConfig).
        # Validated so a bad value can't silently disable reconciliation.
        genre_source_policy=_valid_genre_policy(params.get("genre_source_policy")),
        # Genre classifier (discogs|clap) + optional online web-synthesis lookup.
        genre_classifier=_valid_genre_classifier(params.get("genre_classifier")),
        genre_web_lookup=bool(params.get("genre_web_lookup", False)),
        genre_llm_backend=_valid_llm_backend(params.get("genre_llm_backend")),
        # The ML-override floor for prefer_tag reconciliation. Previously this
        # documented config field never reached analyze (no param, no flag) —
        # the hard-coded dataclass default always won.
        genre_ml_override_confidence=_clamp01(
            params.get("genre_ml_override_confidence", 0.90), 0.90,
        ),
    )
    if "models_dir" in params and params["models_dir"]:
        config.models_dir = Path(params["models_dir"])

    skip_paths = params.get("skip_paths")
    skip_set: set[str] | None = set(skip_paths) if skip_paths else None
    library_path = Path(params["path"])

    report = analyze_directory(
        library_path,
        config=config,
        on_progress=_emit_progress,
        on_track=_emit_track_analyzed,
        output_path=Path(params["output_path"]) if params.get("output_path") else None,
        # skip/limit are counts — a negative value would slice unexpectedly.
        skip=_nonneg_int(params.get("skip", 0), 0),
        limit=_nonneg_int(params.get("limit") or 0, 0) or None,
        skip_paths=skip_set,
    )

    if bool(params.get("auto_save", True)):
        try:
            library_state.record_analysis(library_path, report)
        except Exception as e:  # noqa: BLE001
            log.warning("Could not auto-save analysis state: %s", e)

    return report


def _system_info(_params: dict) -> dict:
    """Report detected CPU / memory / GPU resources to the GUI.

    The base payload is the host-side view from `vibechek.resources.detect()`.
    On Windows where analyze will route through WSL, the GPU as seen by TF
    inside WSL is the ground truth — but probing it costs ~10s of TF import.
    We do NOT probe TF here so this RPC stays snappy. The GUI calls
    `engine_gpu_status` separately after the first render.
    """
    from vibechek.resources import detect, to_dict
    return to_dict(detect())


def _engine_gpu_status(params: dict) -> dict:
    """Report what the analyze engine actually sees for GPUs.

    On Linux/macOS (or Windows with native essentia) this just reuses the
    host-side detection. On Windows where analyze routes through WSL, this
    runs a TensorFlow probe *inside the WSL distro* — which is the truth
    for whether GPU acceleration will actually happen.

    Cached for 5 minutes — see vibechek.wsl._ENGINE_GPU_CACHE_TTL_SEC.
    """
    from vibechek.wsl import engine_gpu_info_to_dict, probe_engine_gpu

    distro = params.get("distro")
    force = bool(params.get("force", False))
    engine = _valid_engine(params.get("engine"))
    info = probe_engine_gpu(distro, force=force, engine=engine)
    return engine_gpu_info_to_dict(info)


def _preflight(params: dict) -> dict:
    """Verify essentia + models are ready for `analyze_directory`.

    `quick` (default True) skips per-distro WSL probes for sub-second response.
    Pass `quick=False` after running an install to get the accurate post-install
    state in one call — the GUI uses this in PreflightDialog's reCheck().
    """
    from vibechek.preflight import preflight, to_dict
    models_dir = Path(params["models_dir"]) if params.get("models_dir") else None
    quick = bool(params.get("quick", True))
    engine = _valid_engine(params.get("engine"))
    return to_dict(preflight(models_dir, quick_wsl=quick, engine=engine))


def _wsl_status(params: dict) -> dict:
    """Report WSL detection results.

    `quick=True` returns immediately without probing distros for
    vibechek/essentia (which can take 30+ sec). Default is full probe.
    """
    from vibechek.wsl import detect_wsl, to_dict
    quick = bool(params.get("quick", False))
    engine = _valid_engine(params.get("engine"))
    venv_subdir = "venv-onnx" if engine == "onnx" else "venv"
    return to_dict(detect_wsl(quick=quick, venv_subdir=venv_subdir))


def _install_wsl(params: dict) -> dict:
    """Install WSL + the named distro via elevated PowerShell. Triggers UAC."""
    from vibechek.wsl import install_wsl
    # Validate centrally at the RPC boundary: `distro` is assembled into a
    # PowerShell command string, so a quote/semicolon could escape the literal
    # and run arbitrary PowerShell behind the UAC prompt. Reject anything
    # outside the safe charset before it reaches the installer.
    distro = _validate_distro(params.get("distro"), "Ubuntu-24.04")
    return install_wsl(distro=distro, on_progress=_emit_progress)


def _valid_engine(value: Any, default: str = "essentia_tf") -> str:
    """Whitelist the inference-engine name before it steers install/venv logic."""
    return value if value in ("essentia_tf", "onnx") else default


def _valid_vibechek_source(params: dict) -> str | None:
    """Optional local-directory override for the managed venv's vibechek install.

    CI/dev plumbing: the native-smoke workflow passes the checked-out repo so
    the venv gets the commit under test instead of GitHub main (the hard-coded
    default in `native_install`). Restricted to an existing local directory so
    the RPC can't be steered into installing from an arbitrary URL. Raises
    ValueError (→ INVALID_PARAMS at the dispatch seam) on anything else.
    Linux/macOS only — the WSL install path doesn't honor it.
    """
    src = params.get("vibechek_source")
    if src is None or src == "":
        return None
    p = Path(str(src))
    if not p.is_dir():
        raise ValueError(
            f"vibechek_source must be an existing local directory, got {str(src)!r}"
        )
    return str(p)


def _valid_genre_policy(value: Any, default: str = "prefer_tag") -> str:
    """Whitelist the existing-tag-vs-ML genre reconciliation policy."""
    return value if value in ("prefer_tag", "prefer_ml", "tag_only", "ml_only") else default


def _valid_genre_classifier(value: Any, default: str = "discogs") -> str:
    """Whitelist the audio genre classifier (steers which model the worker loads)."""
    return value if value in ("discogs", "clap") else default


def _valid_llm_backend(value: Any, default: str = "ollama") -> str:
    """Whitelist the genre web-lookup LLM backend."""
    return value if value in ("ollama",) else default


def _install_vibechek_in_wsl(params: dict) -> dict:
    """Install vibechek + the ML stack + chromaprint inside a WSL distro.

    `engine` picks the stack/venv: "essentia_tf" (default; essentia-tensorflow
    in ~/.vibechek/venv) or "onnx" (plain essentia + onnxruntime in venv-onnx,
    the TF-free engine).
    """
    from vibechek.wsl import install_vibechek_in_wsl
    distro = str(params["distro"])
    engine = _valid_engine(params.get("engine"))
    return install_vibechek_in_wsl(distro, on_progress=_emit_progress, engine=engine)


def _upgrade_vibechek_in_wsl(params: dict) -> dict:
    """Fast-path repair when the WSL install has drifted behind the sidecar.

    Skips the apt + essentia steps (slow, unchanged between betas) and only
    re-installs the vibechek package itself. The analyzer's version-drift
    guard raises a RuntimeError pointing here, so the GUI can surface a
    one-click repair instead of forcing users into the 5-10 minute full
    `install_vibechek_in_wsl` path.
    """
    from vibechek.wsl import upgrade_vibechek_in_wsl
    distro = str(params["distro"])
    return upgrade_vibechek_in_wsl(distro, on_progress=_emit_progress)


def _install_essentia_native(params: dict) -> dict:
    """Install the ML stack + vibechek into ~/.vibechek/venv[-onnx]/.

    The Linux/macOS counterpart to install_vibechek_in_wsl. `engine` picks the
    stack/venv ("essentia_tf" default, or "onnx" → plain essentia + onnxruntime
    in venv-onnx). On Windows this short-circuits — Windows uses the WSL path.
    Optional `vibechek_source` (existing local directory only) overrides where
    pip installs vibechek from — CI/dev use; see _valid_vibechek_source.
    """
    from vibechek.native_install import install_essentia_native
    engine = _valid_engine(params.get("engine"))
    source = _valid_vibechek_source(params)
    return install_essentia_native(
        on_progress=_emit_progress, engine=engine, vibechek_source=source,
    )


def _native_venv_status(params: dict) -> dict:
    """Report what's installed in the managed venv for the selected engine."""
    from vibechek.native_install import probe_native_venv, to_dict
    engine = _valid_engine(params.get("engine"))
    return to_dict(probe_native_venv(engine))


def _repair_wsl_shim(params: dict) -> dict:
    """Strip the (broken) cuda-env.sh source line from the venv's vibechek shim.

    Diagnostic + repair tool for users whose install_cuda_libs_in_wsl ran on
    a pre-beta.10 build that incorrectly patched a Python entry point with
    bash. Safe to call repeatedly; no-op if already clean.
    """
    from vibechek.wsl import repair_wsl_shim
    distro = str(params["distro"])
    return repair_wsl_shim(distro)


def _install_cuda_libs_in_wsl(params: dict) -> dict:
    """Install missing CUDA runtime libs (libcublas/libcufft/libcudnn/...) in WSL.

    Called by the GUI's "Enable GPU" button after `engine_gpu_status` reports
    `gpu_hardware_visible=True` but `gpu_available=False` with a populated
    `missing_cuda_libs` list. Adds the NVIDIA apt repo + installs the runtime
    packages essentia's bundled TF needs to register the GPU.
    """
    from vibechek.wsl import install_cuda_libs_in_wsl
    distro = str(params["distro"])
    missing = list(params.get("missing_libs") or [])
    return install_cuda_libs_in_wsl(distro, missing, on_progress=_emit_progress)


def _find_duplicates(params: dict) -> dict:
    from vibechek.duplicates import find_duplicates

    # Accept the threshold under either key: the frontend now forwards
    # `chromaprint_similarity_threshold` (the config field name), older clients
    # send `threshold`. Clamp to [0, 1] — a value outside that range makes the
    # similarity comparison either match everything (0) or nothing (>1).
    threshold = _clamp01(
        params.get(
            "chromaprint_similarity_threshold", params.get("threshold", 0.95)
        ),
        0.95,
    )
    config = DuplicateConfig(
        use_md5=bool(params.get("use_md5", True)),
        use_chromaprint=bool(params.get("use_chromaprint", True)),
        chromaprint_similarity_threshold=threshold,
        action=params.get("action", "report"),
        review_folder=Path(params["review_folder"]) if params.get("review_folder") else None,
        # Variant awareness (see DuplicateConfig). Safe defaults: keep distinct
        # versions, collapse to the single best encoding within a version.
        keep_distinct_versions=bool(params.get("keep_distinct_versions", True)),
        keep_all_formats=bool(params.get("keep_all_formats", False)),
        # Duration tolerance for the "same version?" check (mislabeled-length
        # split). Clamped: 0 would split every variant pair, >1 would merge a
        # radio edit into an extended mix.
        version_duration_tolerance=_clamp01(
            params.get("version_duration_tolerance", 0.12), 0.12,
        ),
    )
    # Default True for safety — the GUI's auto-keeper rules need bitrate/duration.
    # The GUI can pass read_metadata=false for MD5-only dedup with default rules,
    # which saves a per-file mutagen probe (~30s on a 12k-track library).
    read_metadata = bool(params.get("read_metadata", True))
    report = find_duplicates(
        Path(params["path"]),
        config,
        on_progress=_emit_progress,
        read_metadata=read_metadata,
        # Forward explicitly so the configured chromaprint threshold (the
        # previously-dead Settings slider) actually reaches the matcher.
        similarity_threshold=threshold,
    )
    return report.to_dict()


def _handle_duplicates(params: dict) -> dict:
    from vibechek.duplicates import (
        handle_duplicates,
    )

    config = DuplicateConfig(
        action=params.get("action", "report"),
        review_folder=Path(params["review_folder"]) if params.get("review_folder") else None,
    )
    # Reconstruct the report from its dict form
    report = _rebuild_report(params["report"])
    summary = handle_duplicates(report, config, on_progress=_emit_progress)
    return summary


def _rebuild_report(d: dict) -> Any:
    # `report` is reconstructed from its dict wire form; a non-dict (string,
    # list, null) would blow up deep inside with an opaque AttributeError
    # ('str' object has no attribute 'get'). Validate at the seam so a
    # malformed report returns a clean INVALID_PARAMS instead.
    if not isinstance(d, dict):
        raise ValueError("'report' must be an object")
    from vibechek.duplicates import (
        DuplicateGroup,
        DuplicateReport,
        DuplicateSummary,
        FileInfo,
    )

    def _group(g: dict) -> DuplicateGroup:
        keep = FileInfo(**g["keep"])
        dupes = [FileInfo(**x) for x in g["duplicates"]]
        return DuplicateGroup(
            method=g["method"],
            key=g["key"],
            keep=keep,
            duplicates=dupes,
            recoverable_mb=g["recoverable_mb"],
        )

    # Filter the summary dict to fields the dataclass knows about — protects
    # against older clients that might send extra keys.
    raw_summary = d.get("summary", {})
    valid_summary_fields = {f.name for f in dataclasses.fields(DuplicateSummary)}
    summary = DuplicateSummary(**{k: v for k, v in raw_summary.items() if k in valid_summary_fields})

    return DuplicateReport(
        summary=summary,
        exact_duplicates=[_group(g) for g in d.get("exact_duplicates", [])],
        audio_duplicates=[_group(g) for g in d.get("audio_duplicates", [])],
    )


def _plan_organization(params: dict) -> dict:
    from vibechek.organizer import plan_organization

    config = OrganizationConfig(
        use_subgenres=bool(params.get("use_subgenres", True)),
        min_genre_size=int(params.get("min_genre_size", 10)),
        target_root=Path(params["target_root"]) if params.get("target_root") else None,
    )
    analysis_data = _load_analysis_payload(params)
    plan = plan_organization(analysis_data, config)
    return {
        "base_dir": str(plan.base_dir),
        "small_genres": sorted(plan.small_genres),
        "genre_counts": plan.genre_counts,
        "moves": [
            {
                "source": str(m.source),
                "destination": str(m.destination),
                "genre": m.genre,
                "subgenre": m.subgenre,
                "reason": m.reason,
            }
            for m in plan.moves
        ],
        "errors": plan.errors,
    }


def _organize(params: dict) -> dict:
    from vibechek.organizer import organize_from_analysis

    config = OrganizationConfig(
        use_subgenres=bool(params.get("use_subgenres", True)),
        min_genre_size=int(params.get("min_genre_size", 10)),
        target_root=Path(params["target_root"]) if params.get("target_root") else None,
    )
    analysis_data = _load_analysis_payload(params)
    try:
        stats = organize_from_analysis(
            analysis_data,
            config,
            on_progress=_emit_progress,
            dry_run=bool(params.get("dry_run", False)),
        )
    except cancellation.CancelledError as e:
        # A cancelled organize has usually already moved SOME files and written a
        # revert journal. Surface those partial stats (incl. journal_path) as a
        # normal result tagged cancelled=True, so the GUI can still offer "Undo
        # this organize" — instead of an APP_ERROR that drops the journal path
        # and strands the half-moved files with no one-click undo.
        partial = getattr(e, "partial_stats", None)
        if partial is not None:
            result = asdict(partial)
            result["cancelled"] = True
            return result
        raise
    return asdict(stats)


def _apply_ml_tags(params: dict) -> dict:
    from vibechek.tagger import apply_ml_tags

    # Per-field write toggles. `skip_bpm_and_key` is still accepted for
    # backward compatibility (older GUIs / scripts) and maps onto the new
    # write_bpm/write_key pair when the explicit toggles aren't provided.
    legacy_skip = bool(params.get("skip_bpm_and_key", True))

    # Confidence thresholds are probabilities — clamp to [0, 1] so a `0.0`/
    # negative value can't over-tag the entire library and a `>1` value can't
    # silently disable a head.
    genre_conf = _clamp01(params.get("confidence", 0.85), 0.85)
    parent_conf = _clamp01(
        params.get("parent_genre_confidence_threshold", 0.50), 0.50
    )
    # Vocal classification bands (voice probability 0..1). They MUST stay
    # ordered instrumental_max < full_min or every track is misclassified
    # (the "between" Light-Vocal window inverts). Clamp both, then reject an
    # inverted pair rather than silently mangling the whole library.
    vocal_inst_max = _clamp01(params.get("vocal_instrumental_max", 0.72), 0.72)
    vocal_full_min = _clamp01(params.get("vocal_full_min", 0.88), 0.88)
    if vocal_inst_max >= vocal_full_min:
        raise ValueError(
            "vocal_instrumental_max "
            f"({vocal_inst_max}) must be < vocal_full_min ({vocal_full_min})"
        )

    config = TaggingConfig(
        genre_confidence_threshold=genre_conf,
        parent_genre_confidence_threshold=parent_conf,
        write_genre=bool(params.get("write_genre", True)),
        write_bpm=bool(params.get("write_bpm", not legacy_skip)),
        write_key=bool(params.get("write_key", not legacy_skip)),
        write_energy=bool(params.get("write_energy", True)),
        write_mood=bool(params.get("write_mood", True)),
        write_timeslot=bool(params.get("write_timeslot", True)),
        write_direction=bool(params.get("write_direction", True)),
        write_vocal=bool(params.get("write_vocal", True)),
        vocal_instrumental_max=vocal_inst_max,
        vocal_full_min=vocal_full_min,
        preserve_rekordbox_frames=bool(params.get("preserve_rekordbox_frames", True)),
        # Forward the Settings toggles so they actually take effect. Both have
        # safe TaggingConfig defaults (True), so omitting them preserves prior
        # behaviour; the GUI's Settings switches were previously dead because
        # the values never reached the config.
        write_subgenre_as_main_genre=bool(
            params.get("write_subgenre_as_main_genre", True)
        ),
        backup_before_write=bool(params.get("backup_before_write", True)),
        # ID3 text-frame encoding: useApplyTags now forwards this from
        # cfg.tagging.id3_text_encoding (added in beta.9, wired in beta.11).
        # 3 = UTF-8 default; 1 = UTF-16 for Rekordbox 5 compat; 0 = ISO-8859-1.
        # Validated to {0,1,2,3} so a stray value can't write a corrupt
        # encoding byte into MP3 frames; anything else falls back to UTF-8 (3).
        id3_text_encoding=_valid_id3_encoding(params.get("id3_text_encoding", 3), 3),
    )
    analysis_data = _load_analysis_payload(params)
    dry = bool(params.get("dry_run", False))

    # "Backup before write" safety net (default ON). Snapshot the EXACT files
    # about to be tagged BEFORE mutating them, so a bad apply is recoverable via
    # restore-tags. Previously this toggle was dead — the value reached the
    # config but nothing acted on it, so users had a false sense of safety.
    backup_path = None
    if config.backup_before_write and not dry:
        backup_path = _auto_backup_before_apply(analysis_data)

    stats = apply_ml_tags(
        analysis_data, config,
        on_progress=_emit_progress,
        dry_run=dry,
    )
    result = asdict(stats)
    if backup_path is not None:
        result["backup_path"] = str(backup_path)
    return result


def _auto_backup_before_apply(analysis_data: dict) -> Path | None:
    """Snapshot the tags of every file in the apply set to a timestamped backup
    under the data dir, and record it in the user's backup history. Returns the
    backup path, or None when there is nothing to back up. Raises if the backup
    file itself can't be written — we must NOT proceed to mutate tags when the
    requested safety backup failed."""
    from datetime import datetime  # noqa: PLC0415

    from vibechek import backup_history  # noqa: PLC0415
    from vibechek.config import DATA_DIR  # noqa: PLC0415
    from vibechek.tagger import backup_tag_files  # noqa: PLC0415

    tracks = analysis_data.get("tracks") or []
    paths = [t.get("path") for t in tracks if isinstance(t, dict) and t.get("path")]
    if not paths:
        return None
    backups_dir = DATA_DIR / "backups"
    backups_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    output = backups_dir / f"pre-apply-{stamp}.json"
    stats = backup_tag_files(paths, output, on_progress=_emit_progress)
    try:
        common = os.path.commonpath([str(p) for p in paths])
        backup_history.record(common, output, stats.backed_up)
    except Exception as e:  # noqa: BLE001
        log.warning("Could not record pre-apply backup in history: %s", e)
    return output


def _backup_tags(params: dict) -> dict:
    from vibechek import backup_history
    from vibechek.tagger import backup_tags

    library = Path(params["path"])
    output = Path(params["output_path"])
    stats = backup_tags(library, output, on_progress=_emit_progress)

    # Record in the user's backup history so the Tags view can list it.
    try:
        backup_history.record(library, output, stats.backed_up)
    except Exception as e:  # noqa: BLE001
        log.warning("Could not record backup in history: %s", e)

    return asdict(stats)


def _restore_tags(params: dict) -> dict:
    from vibechek.tagger import restore_tags

    # Forward the user's tagging config so the plain restore path honours
    # id3_text_encoding too (mirrors _restore_tags_with_remap). Without it
    # restore_tags falls back to a fresh TaggingConfig() (UTF-8) and silently
    # re-encodes every MP3 text frame as UTF-8 — empty in Rekordbox 5 / UTF-16
    # setups — on the standard "Restore tags" button.
    cfg = VibechekConfig.load().tagging
    stats = restore_tags(
        Path(params["backup_path"]),
        on_progress=_emit_progress,
        config=cfg,
    )
    return asdict(stats)


def _restore_tags_with_remap(params: dict) -> dict:
    """Restore a tag backup with auto-remap for moved libraries.

    Params: {backup_path: str, library_root: str}. The Tags view exposes this
    as the "Restore (auto-detect moved files)" flow — the user picks both the
    backup JSON and the directory they want to restore *into*, and the tagger
    figures out which on-disk file matches each backup entry.
    """
    from vibechek.tagger import restore_tags_with_remap

    # Forward the user's tagging config so the remap-restore path honours
    # id3_text_encoding (otherwise it defaults to UTF-8 and silently re-encodes
    # MP3 text frames — empty in Rekordbox 5 / UTF-16 setups — on the exact
    # path used after a library move). Loaded from disk so the wired toggle
    # reaches the restorer.
    cfg = VibechekConfig.load().tagging
    stats = restore_tags_with_remap(
        Path(params["backup_path"]),
        Path(params["library_root"]),
        on_progress=_emit_progress,
        config=cfg,
    )
    return asdict(stats)


def _download_models(params: dict) -> dict:
    from vibechek.analyzer import download_models
    from vibechek.config import MODELS_DIR

    target = Path(params["models_dir"]) if params.get("models_dir") else MODELS_DIR
    # Engine-aware: "onnx" fetches the converted .onnx heads + backbone; default
    # essentia_tf fetches the .pb set. Without this the ONNX engine downloads
    # the wrong models and then can't find its .onnx files.
    engine = _valid_engine(params.get("engine"))
    descriptors = download_models(target, on_progress=_emit_progress, engine=engine)
    return {"models_dir": str(target), "models": list(descriptors.keys())}


def _setup_onnx_engine(params: dict) -> dict:
    """One-click, self-healing ONNX engine setup (offline-first).

    Does everything needed for `inference_engine="onnx"` to work, in order, with
    a `progress` notification at each stage so the GUI can show a live dialog:

      1. Stage the BUNDLED converted heads into ``<models>/onnx`` (their own
         subdir, so they never collide with the essentia ``.pb`` set). Cleans
         interrupted ``.partial`` leftovers + overwrites stale heads.
      2. Ensure the ONNX engine environment is installed — the WSL distro
         (Windows) or the native managed venv (Linux/macOS). Installs only if
         not already usable, so a re-click is a fast no-op.
      3. Fetch ONLY the EffNet backbone from essentia (the heads are bundled and
         NOT hosted upstream). The download never deletes a good cached file.
      4. Verify readiness via the ONNX preflight.

    Cancellable. Returns {ok, ready, staged, bundle_source, reasons_not_ready}.
    Optional `vibechek_source` (existing local directory only) overrides where
    the native venv install gets vibechek from — CI/dev use; WSL ignores it.
    """
    from vibechek import onnx_backend  # noqa: PLC0415
    from vibechek.analyzer import download_models  # noqa: PLC0415
    from vibechek.config import MODELS_DIR  # noqa: PLC0415
    from vibechek.platform import IS_WINDOWS  # noqa: PLC0415
    from vibechek.preflight import preflight  # noqa: PLC0415

    # Validate the optional CI/dev source override BEFORE staging starts so a
    # bad param fails fast as INVALID_PARAMS (native install branch only — the
    # WSL branch always installs from GitHub).
    source_override = _valid_vibechek_source(params)

    total = 4

    # 1. Stage bundled heads (local, fast).
    _emit_progress(1, total, "Staging bundled ONNX models…")
    staged = onnx_backend.stage_bundled_onnx_heads(
        MODELS_DIR, on_progress=lambda d, t, m: _emit_progress(1, total, m)
    )

    # 2. Ensure the ONNX engine environment (install only if not already usable).
    _emit_progress(2, total, "Checking the ONNX engine environment…")
    cancellation.check()
    if IS_WINDOWS:
        from vibechek.wsl import detect_wsl, install_vibechek_in_wsl  # noqa: PLC0415

        st = detect_wsl(quick=False, venv_subdir="venv-onnx")
        distro = _validate_distro(params.get("distro"), "") or st.usable_distro
        if not distro and st.distros:
            distro = st.distros[0].name
        if not distro:
            raise RuntimeError(
                "No WSL distro found. Run the standard WSL setup first, then set "
                "up the ONNX engine."
            )
        already = next(
            (d for d in st.distros
             if d.name == distro and d.vibechek_installed and d.essentia_installed),
            None,
        )
        if already is None:
            _emit_progress(2, total, f"Installing the ONNX engine in {distro} (one-time)…")
            res = install_vibechek_in_wsl(
                distro, engine="onnx",
                on_progress=lambda d, t, m: _emit_progress(2, total, m),
            )
            # install_vibechek_in_wsl RETURNS {ok: False, error: ...} on failure
            # (it does not raise). Without checking it we'd continue to the
            # download + preflight and replace the installer's specific, actionable
            # reason (network/disk/pip) with the generic preflight string. Capture
            # and short-circuit so the GUI can show the real cause.
            if isinstance(res, dict) and not res.get("ok"):
                pf = preflight(None, quick_wsl=True, engine="onnx")
                return {
                    "ok": False,
                    "ready": False,
                    "error": res.get("error"),
                    "cancelled": bool(res.get("cancelled")),
                    "staged": staged.get("staged", []),
                    "bundle_source": staged.get("source"),
                    "reasons_not_ready": pf.reasons_not_ready,
                    "analyze_via": pf.analyze_via,
                }
    else:
        from vibechek.native_install import (  # noqa: PLC0415
            install_essentia_native,
            probe_native_venv,
        )

        nv = probe_native_venv("onnx")
        if not (nv.essentia_installed and nv.vibechek_installed):
            _emit_progress(2, total, "Installing the ONNX engine (one-time)…")
            res = install_essentia_native(
                engine="onnx",
                on_progress=lambda d, t, m: _emit_progress(2, total, m),
                vibechek_source=source_override,
            )
            # install_essentia_native also RETURNS {ok: False, error: ...} on
            # failure rather than raising — same short-circuit as the WSL branch.
            if isinstance(res, dict) and not res.get("ok"):
                pf = preflight(None, quick_wsl=False, engine="onnx")
                return {
                    "ok": False,
                    "ready": False,
                    "error": res.get("error"),
                    "cancelled": bool(res.get("cancelled")),
                    "staged": staged.get("staged", []),
                    "bundle_source": staged.get("source"),
                    "reasons_not_ready": pf.reasons_not_ready,
                    "analyze_via": pf.analyze_via,
                }

    # 3. Fetch ONLY the EffNet backbone (heads are bundled/staged).
    _emit_progress(3, total, "Fetching the EffNet backbone…")
    cancellation.check()
    download_models(
        MODELS_DIR, engine="onnx",
        on_progress=lambda d, t, m: _emit_progress(3, total, m),
    )

    # 4. Verify the engine can actually analyze.
    _emit_progress(4, total, "Verifying ONNX engine…")
    pf = preflight(None, quick_wsl=False, engine="onnx")
    return {
        "ok": bool(pf.ready),
        "ready": bool(pf.ready),
        # `error` is part of the schema on every path: None on success, the
        # installer's specific reason on an install-step failure (set in the
        # short-circuit returns above). The frontend prefers res.error when set.
        "error": None,
        "cancelled": False,
        "staged": staged.get("staged", []),
        "bundle_source": staged.get("source"),
        "reasons_not_ready": pf.reasons_not_ready,
        "analyze_via": pf.analyze_via,
    }


def _active_engine(params: dict) -> str:
    """The inference engine whose venv the opt-in genre setup should target.

    An explicit request param wins: the UI sends its LIVE selection, which can
    lead the saved config by the 500 ms autosave debounce (and `VibechekConfig.
    load()` never raises, so a config-based fallback-on-exception was dead
    code that silently ignored the param)."""
    explicit = params.get("inference_engine")
    if explicit in ("essentia_tf", "onnx"):
        return explicit
    try:
        return _valid_engine(VibechekConfig.load().analysis.inference_engine)
    except Exception:  # noqa: BLE001
        return "essentia_tf"


def _setup_genre_engine(params: dict, *, kind: str) -> dict:
    """Shared one-click setup for the opt-in genre engines.

    `kind` is "clap" (pure-audio CLAP+kNN student: torch+laion-clap + 2.2 GB
    checkpoint) or "resolver" (online web-synthesis: ddgs + Ollama + 4.7 GB
    model). Both install into the ACTIVE analysis venv so one worker can run them
    alongside essentia/onnx. Cancellable; emits `progress`. Windows → WSL; native
    Linux/macOS is not yet wired for these heavy opt-in engines.
    """
    from vibechek.platform import IS_WINDOWS  # noqa: PLC0415

    engine = _active_engine(params)
    label = "CLAP genre engine" if kind == "clap" else "online genre resolver"
    if not IS_WINDOWS:
        return {"ok": False, "ready": False,
                "error": f"{label} setup is currently available on Windows/WSL only."}

    from vibechek.wsl import (  # noqa: PLC0415
        detect_wsl,
        setup_clap_in_wsl,
        setup_resolver_in_wsl,
    )

    venv_subdir = "venv-onnx" if engine == "onnx" else "venv"
    st = detect_wsl(quick=False, venv_subdir=venv_subdir)
    distro = _validate_distro(params.get("distro"), "") or st.usable_distro
    if not distro and st.distros:
        distro = st.distros[0].name
    if not distro:
        return {"ok": False, "ready": False,
                "error": "No WSL distro found. Run the standard WSL setup first."}

    fn = setup_clap_in_wsl if kind == "clap" else setup_resolver_in_wsl
    res = fn(distro, on_progress=_emit_progress, engine=engine)
    ok = bool(res.get("ok"))
    return {
        "ok": ok, "ready": ok,
        "error": res.get("error"),
        "cancelled": bool(res.get("cancelled")),
        "distro": distro,
        "tail": res.get("tail"),
    }


def _setup_clap_engine(params: dict) -> dict:
    """One-click setup for the pure-audio CLAP genre student (opt-in, ~2.2 GB)."""
    return _setup_genre_engine(params, kind="clap")


def _setup_genre_resolver(params: dict) -> dict:
    """One-click setup for the online web-synthesis genre resolver (opt-in)."""
    return _setup_genre_engine(params, kind="resolver")


def _get_config(_params: dict) -> dict:
    """Load config from disk (or defaults if no file exists yet)."""
    return _config_to_jsonable(VibechekConfig.load())


def _save_config(params: dict) -> dict:
    """Persist a VibechekConfig dict to disk. Returns the saved file path."""
    data = params.get("config", {})
    # Guard the payload type explicitly: a non-dict (list/str/number) would hit
    # `_from_dict`'s `.get(...)` and raise AttributeError, which the dispatcher
    # classifies as APP_ERROR (-32000) + a traceback. A malformed `config`
    # payload is a caller bug, so surface it as INVALID_PARAMS instead.
    if not isinstance(data, dict):
        raise ValueError("'config' must be an object")
    cfg = VibechekConfig._from_dict(data)
    path = cfg.save()
    return {"saved_to": str(path)}


def _restore_default_config(_params: dict) -> dict:
    """Reset config to defaults and save."""
    cfg = VibechekConfig()
    path = cfg.save()
    return {"saved_to": str(path), "config": _config_to_jsonable(cfg)}


def _cancel_operation(_params: dict) -> dict:
    """Request cancellation of the currently running long operation."""
    kind = cancellation.cancel()
    return {"cancelled": kind}


# ---------------------------------------------------------------------------
# Library state — recent libraries + auto-load last analysis
# ---------------------------------------------------------------------------


def _library_state(_params: dict) -> dict:
    """Return the recent-libraries list. Used by the GUI's startup screen."""
    from vibechek import library_state

    state = library_state.load_state()
    return {"recent": [asdict(r) for r in state.recent]}


def _forget_library(params: dict) -> dict:
    """Remove a library from the recent list."""
    from vibechek import library_state

    removed = library_state.forget(params["path"])
    return {"removed": removed}


def _rename_library(params: dict) -> dict:
    """Set a friendly display name on a recent library.

    Params: {path: str, name: str}. Returns {renamed: bool, record?: {...}}.
    A no-op `renamed=False` when the path isn't in the recent list — this
    is an instant non-cancellable op so the GUI can fire-and-forget after
    inline edits.
    """
    from vibechek import library_state

    path = params["path"]
    name = str(params.get("name") or "")
    record = library_state.rename_library(path, name)
    if record is None:
        return {"renamed": False}
    return {"renamed": True, "record": asdict(record)}


def _tag_library(params: dict) -> dict:
    """Replace the user-assigned tag list on a recent library.

    Params: {path: str, tags: list[str]}. Returns {tagged: bool, record?:
    {...}}. `tagged=False` when the path isn't in the recent list. Tags
    are de-duplicated and stripped server-side — callers can pass raw
    user input.
    """
    from vibechek import library_state

    path = params["path"]
    tags = params.get("tags") or []
    if not isinstance(tags, list):
        raise ValueError("tags must be a list of strings")
    record = library_state.tag_library(path, [str(t) for t in tags])
    if record is None:
        return {"tagged": False}
    return {"tagged": True, "record": asdict(record)}


def _doctor(_params: dict) -> dict:
    """Render the `vibechek doctor` diagnostic report as paste-friendly markdown.

    The UI's planned "Copy diagnostic" button calls this and writes the
    string straight to the clipboard. Schema: {"markdown": str}.
    """
    from vibechek.doctor import build_report, render_markdown
    return {"markdown": render_markdown(build_report())}


def _sha256_file(path: Path) -> str:
    """Stream a file's SHA256 in 1 MiB chunks (avoid slurping 100+ MB weights)."""
    import hashlib as _hashlib

    h = _hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _verify_models(params: dict) -> dict:
    """Verify the downloaded model files' SHA256 against the pinned table.

    Engine-aware: the ONNX engine stages its heads + backbone under
    ``<models>/onnx`` and never downloads the essentia ``.pb`` set, so verifying
    the flat ``.pb``/``.json`` files for an ONNX-only install would report every
    file "missing" (a false alarm) while never integrity-checking the ``.onnx``
    files the analyze pipeline actually loads. We pick the file set from the
    active engine (``params['engine']`` if given, else config.inference_engine).

    Returns {"results": [{name, suffix, ok, expected, computed}, ...], "engine": ...}.
    `ok=True` for files matching the pinned digest, `ok=None` for files we have
    no pin for yet (the essentia table is populated on the release build),
    `ok=False` for actual mismatches or missing required files.
    """
    from vibechek.config import MODELS_DIR

    engine = _valid_engine(
        params.get("engine"), default=VibechekConfig.load().analysis.inference_engine
    )

    results: list[dict] = []

    if engine == "onnx":
        from vibechek.analyzer import (
            _ONNX_HEAD_STEMS,
            _ONNX_SUBDIR,
            MODEL_SHA256_ONNX,
        )
        from vibechek.onnx_backend import BACKBONE_ONNX_FILENAME

        onnx_dir = MODELS_DIR / _ONNX_SUBDIR
        # Backbone (.onnx) + each converted head (.onnx + class-label .json).
        filenames = [BACKBONE_ONNX_FILENAME]
        for stem in _ONNX_HEAD_STEMS:
            filenames.append(f"{stem}.onnx")
            filenames.append(f"{stem}.json")

        for fname in filenames:
            path = onnx_dir / fname
            expected = MODEL_SHA256_ONNX.get(fname)
            if not path.exists():
                results.append({
                    "name": fname, "suffix": "onnx", "ok": False,
                    "reason": "missing",
                })
                continue
            computed = _sha256_file(path)
            if expected is None:
                # Backbone has no pin (fetched from essentia upstream); report
                # informational ok=None rather than a scary failure.
                results.append({
                    "name": fname, "suffix": "onnx", "ok": None,
                    "computed": computed,
                    "reason": "no pin (upstream backbone / unpinned head)",
                })
            else:
                results.append({
                    "name": fname, "suffix": "onnx",
                    "ok": computed.lower() == expected.lower(),
                    "expected": expected, "computed": computed,
                })
        return {"results": results, "engine": engine}

    # essentia_tf: the flat .pb / .json set in the models dir.
    from vibechek.analyzer import MODEL_SHA256, MODELS

    for name in MODELS:
        for suffix in ("pb", "json"):
            path = MODELS_DIR / f"{name}.{suffix}"
            if not path.exists():
                results.append({
                    "name": name, "suffix": suffix, "ok": False,
                    "reason": "missing",
                })
                continue
            computed = _sha256_file(path)
            expected = MODEL_SHA256.get(name, {}).get(suffix)
            if expected is None:
                results.append({
                    "name": name, "suffix": suffix, "ok": None,
                    "computed": computed,
                    "reason": "no pin (release-build pass populates these)",
                })
            else:
                results.append({
                    "name": name, "suffix": suffix,
                    "ok": computed.lower() == expected.lower(),
                    "expected": expected, "computed": computed,
                })
    return {"results": results, "engine": engine}


def _list_profiles(_params: dict) -> dict:
    """Return the built-in DJ profiles. Used by Settings to render a picker."""
    from vibechek.profiles import list_profiles
    return {"profiles": list_profiles()}


def _load_profile(params: dict) -> dict:
    """Apply a built-in profile to the user's config + save.

    Params: {name: str}. Returns the new config dict + the profile that was
    applied. Raises INVALID_PARAMS for unknown profile names.
    """
    from vibechek.profiles import load_profile
    try:
        result = load_profile(str(params["name"]))
    except KeyError as e:
        raise ValueError(f"Unknown profile: {e}") from e
    return result


def _count_new_tracks(params: dict) -> dict:
    """How many audio files on disk are *not* in the saved analysis?

    Params: {library_path: str}. Returns {new_count, total_count,
    analyzed_count}. Compares the current filesystem against the most
    recent saved analysis report so the GUI can show a "🆕 86 new tracks"
    banner without re-running ML.

    Cheap: just walks the filesystem and reads one JSON file — no decoding,
    no fingerprinting. Safe to call after every library load.
    """
    from vibechek import library_state
    from vibechek.utils import find_audio_files

    library_path = Path(params["library_path"])
    if not library_path.exists():
        # Don't raise — the GUI calls this opportunistically after every
        # library load and we shouldn't blow up if the user just unplugged
        # an external drive.
        return {"new_count": 0, "total_count": 0, "analyzed_count": 0,
                "reason": "library path missing"}

    state = library_state.load_state()
    record = next((r for r in state.recent if r.path == str(library_path)), None)

    # Walk the filesystem first — that's the "total" we're comparing against.
    try:
        on_disk = find_audio_files(library_path)
    except (OSError, FileNotFoundError) as e:
        return {"new_count": 0, "total_count": 0, "analyzed_count": 0,
                "reason": str(e)}
    total = len(on_disk)
    # Compare by basename rather than full path. A library the user MOVED
    # (D:/Music → E:/Music, or renamed the drive) has different absolute paths
    # for the same files, so full-path equality would report every track as
    # "new" the moment the folder moves. Basenames survive the move. Trade-off:
    # two distinct files with the same name in different subfolders collapse to
    # one key — acceptable for a cheap "🆕 N new" banner (the incremental
    # analyze itself still keys on full path).
    on_disk_names = {p.name for p in on_disk}

    if record is None:
        # No prior analysis means every file is "new" — but emitting that as
        # a banner is misleading (the user hasn't analyzed anything yet, so
        # there's nothing to be "new" relative to). Surface zero new so the
        # banner stays hidden; the empty-state UI already handles the "no
        # analysis yet" case.
        return {"new_count": 0, "total_count": total, "analyzed_count": 0,
                "reason": "no prior analysis"}

    report = library_state.load_analysis(record)
    if not report:
        return {"new_count": 0, "total_count": total, "analyzed_count": 0,
                "reason": "analysis file missing"}

    analyzed_names = {
        Path(t["path"]).name
        for t in (report.get("tracks") or [])
        if t.get("path")
    }
    # "New" = on disk now AND not in the saved analysis (by basename, so a
    # moved library doesn't read as all-new). We don't count tracks that
    # vanished from disk as "new" (a negative number would be silly); the GUI's
    # incremental analyze handles ghost pruning.
    new_count = len(on_disk_names - analyzed_names)
    return {
        "new_count": new_count,
        "total_count": total,
        "analyzed_count": len(analyzed_names & on_disk_names),
    }


def _load_recent_analysis(params: dict) -> dict:
    """Load a saved analysis JSON by library path, or by analysis_path."""
    from vibechek import library_state

    if "library_path" in params:
        state = library_state.load_state()
        record = next(
            (r for r in state.recent if r.path == params["library_path"]),
            None,
        )
        if not record:
            return {"loaded": False, "reason": "library not in recents"}
        report = library_state.load_analysis(record)
    elif "analysis_path" in params:
        path = Path(params["analysis_path"])
        if not path.exists():
            return {"loaded": False, "reason": "file not found"}
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            return {"loaded": False, "reason": str(e)}
    else:
        return {"loaded": False, "reason": "missing library_path or analysis_path"}

    if report is None:
        return {"loaded": False, "reason": "analysis file missing or corrupt"}
    return {"loaded": True, "report": report}


# ---------------------------------------------------------------------------
# Undo journals (organize / dedupe-move / dedupe-trash)
# ---------------------------------------------------------------------------


def _list_journals(params: dict) -> dict:
    """List recent operation journals so the GUI can offer 'Undo'.

    Returns {"journals": [{path, kind, started_at, root, move_count,
    trash_count}, ...]}, most recent first.
    """
    from vibechek import journal
    limit = int(params.get("limit", 50))
    return {"journals": journal.list_journals(limit=limit)}


def _revert_journal(params: dict) -> dict:
    """Reverse the moves recorded in a journal. Cancellable.

    Params: {journal_path: str}. Moves files back to their original locations
    (skips entries whose destination is gone or whose origin is now occupied).
    Trash entries can't be auto-restored and are reported in
    `trashed_not_reverted`. Returns the revert summary.
    """
    from vibechek import journal
    return journal.revert_journal(
        Path(params["journal_path"]),
        on_progress=_emit_progress,
    )


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def _get_log_tail(params: dict) -> dict:
    """Return the most recent log lines so the GUI can show them after an error."""
    from vibechek import logging_setup

    n = int(params.get("n", 200))
    return {
        "log_file": str(logging_setup.LOG_FILE),
        "lines": logging_setup.tail(n),
    }


def _backup_history(_params: dict) -> dict:
    """List every tag backup the user has made via Vibechek."""
    from vibechek import backup_history

    return backup_history.to_dict(backup_history.load())


def _forget_backup(params: dict) -> dict:
    """Drop a backup from the history (does not delete the file itself)."""
    from vibechek import backup_history

    removed = backup_history.forget(params["backup_path"])
    return {"removed": removed}


def _config_to_jsonable(cfg: VibechekConfig) -> dict:
    """asdict + stringify Paths so the GUI gets pure JSON."""
    from vibechek.config import _stringify_paths
    return _stringify_paths(asdict(cfg))


def _load_analysis_payload(params: dict) -> dict:
    """Either inline `analysis` or path to JSON file."""
    if "analysis" in params:
        return params["analysis"]
    if "analysis_path" in params:
        # Guard the client-supplied path: a missing file or a directory would
        # otherwise surface as an opaque OSError/IsADirectoryError → APP_ERROR.
        # Classify it as a caller error (ValueError → INVALID_PARAMS) with a
        # clear message instead.
        ap = Path(params["analysis_path"])
        if not ap.exists():
            raise ValueError(f"analysis_path does not exist: {ap}")
        if not ap.is_file():
            raise ValueError(f"analysis_path is not a file: {ap}")
        return json.loads(ap.read_text(encoding="utf-8"))
    raise ValueError("params must include 'analysis' (object) or 'analysis_path' (string)")


METHODS: dict[str, Callable[[dict], Any]] = {
    "ping": _ping,
    "version": _version,
    "system_info": _system_info,
    "engine_gpu_status": _engine_gpu_status,
    "preflight": _preflight,
    "wsl_status": _wsl_status,
    "install_wsl": _install_wsl,
    "install_vibechek_in_wsl": _install_vibechek_in_wsl,
    "upgrade_vibechek_in_wsl": _upgrade_vibechek_in_wsl,
    "install_cuda_libs_in_wsl": _install_cuda_libs_in_wsl,
    "install_essentia_native": _install_essentia_native,
    "native_venv_status": _native_venv_status,
    "repair_wsl_shim": _repair_wsl_shim,
    "scan_directory": _scan_directory,
    "scan_only": _scan_only,
    "analyze_directory": _analyze_directory,
    "find_duplicates": _find_duplicates,
    "handle_duplicates": _handle_duplicates,
    "plan_organization": _plan_organization,
    "organize": _organize,
    "apply_ml_tags": _apply_ml_tags,
    "backup_tags": _backup_tags,
    "restore_tags": _restore_tags,
    "restore_tags_with_remap": _restore_tags_with_remap,
    "download_models": _download_models,
    "setup_onnx_engine": _setup_onnx_engine,
    "setup_clap_engine": _setup_clap_engine,
    "setup_genre_resolver": _setup_genre_resolver,
    "get_config": _get_config,
    "save_config": _save_config,
    "restore_default_config": _restore_default_config,
    "cancel_operation": _cancel_operation,
    "library_state": _library_state,
    "forget_library": _forget_library,
    "load_recent_analysis": _load_recent_analysis,
    "rename_library": _rename_library,
    "tag_library": _tag_library,
    "count_new_tracks": _count_new_tracks,
    "get_log_tail": _get_log_tail,
    "backup_history": _backup_history,
    "forget_backup": _forget_backup,
    "doctor": _doctor,
    "verify_models": _verify_models,
    "list_profiles": _list_profiles,
    "load_profile": _load_profile,
    "list_journals": _list_journals,
    "revert_journal": _revert_journal,
}

# Methods that run long ops and should be cancellable.
_CANCELLABLE_METHODS = {
    "analyze_directory": "analyze",
    "scan_only": "analyze",
    "find_duplicates": "dedupe",
    # handle_duplicates physically MOVES/TRASHES files and its loops call
    # cancellation.check() (vibechek.duplicates). Those checks only go live when
    # the dispatcher has begun a cancellation kind for the method — otherwise the
    # Cancel button is inert and a destructive batch can't be stopped. Use a kind
    # distinct from find_duplicates' 'dedupe' so the busy-rejection messaging is
    # accurate and the move/trash op is gated by _LONG_OP_LOCK.
    "handle_duplicates": "dedupe-handle",
    "organize": "organize",
    "apply_ml_tags": "tag",
    "backup_tags": "backup",
    "restore_tags": "restore",
    "restore_tags_with_remap": "restore",
    "download_models": "download-models",
    "install_wsl": "install-wsl",
    "install_vibechek_in_wsl": "install-essentia",
    "upgrade_vibechek_in_wsl": "install-essentia",
    "install_cuda_libs_in_wsl": "install-cuda",
    "install_essentia_native": "install-essentia",
    "setup_onnx_engine": "install-essentia",
    "setup_clap_engine": "install-essentia",
    "setup_genre_resolver": "install-essentia",
    "revert_journal": "revert",
}

# JSON-RPC error codes
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
APP_ERROR = -32000


def _dispatch(request: dict[str, Any]) -> None:
    """Run a single request handler. Called from the thread pool.

    Writes the response (or error) directly via _write_message. Long ops
    register with the cancellation module so the user-facing Cancel button
    works.
    """
    req_id = request.get("id")
    method = request.get("method")
    params = request.get("params") or {}

    if not method:
        # Per JSON-RPC 2.0 the server must NOT reply to a notification (no id),
        # even on error. The handler-exception branches already guard this; the
        # early returns must too, or an id-less malformed request gets an illegal
        # {"id": null, ...} error frame.
        if req_id is not None:
            _err(req_id, INVALID_REQUEST, "Missing 'method'")
        return

    handler = METHODS.get(method)
    if handler is None:
        if req_id is not None:
            _err(req_id, METHOD_NOT_FOUND, f"Method not found: {method}")
        return

    # Validate params SHAPE once, before toggling any cancellation state. Every
    # handler immediately does `params.get(...)`; a non-object params (JSON
    # array/string/number) raises AttributeError, which is NOT in the
    # (TypeError, KeyError, ValueError) branch below and would otherwise leak a
    # full traceback as APP_ERROR (-32000) instead of a clean INVALID_PARAMS.
    # Running this before the begin()/end() block keeps cancellation state from
    # being touched for a rejected request.
    if not isinstance(params, dict):
        if req_id is not None:
            _err(req_id, INVALID_PARAMS, "'params' must be an object")
        return

    # Protocol-level correlation id (optional). The GUI stamps long-op requests
    # with a client-generated `op_id`; every progress / track_analyzed
    # notification emitted while that op runs echoes it back (_emit_progress),
    # so each dialog can filter the shared event stream down to ITS op instead
    # of leaning on the single-long-op invariant alone. Popped for EVERY method
    # so handlers never see a protocol-level key; recorded only for cancellable
    # ops — short ops interleave on the thread pool and must not claim the
    # process-wide singleton.
    op_id = params.pop("op_id", None)
    if op_id is not None and not isinstance(op_id, str):
        op_id = str(op_id)  # forgiving — a buggy client shouldn't kill its op
    if op_id:
        op_id = op_id[:128]  # defensive cap: it rides on every progress frame
    else:
        op_id = None

    kind = _CANCELLABLE_METHODS.get(method)
    if kind is not None:
        # The cancellation module is a process-wide singleton (one flag, one
        # `_current_kind`). If a second cancellable op tries to run while one
        # is already in progress, `begin()` would clobber the first's kind and
        # then `end()` from op 1 would clear the flag for op 2. Reject the
        # second op cleanly instead.
        with _LONG_OP_LOCK:
            existing = cancellation.current_kind()
            if existing is not None:
                # Same notification guard as the rest of _dispatch — never reply
                # to an id-less request, even with a busy error.
                if req_id is not None:
                    _err(
                        req_id,
                        INVALID_REQUEST,
                        f"Another long-running operation ({existing!r}) is already "
                        f"in progress. Cancel it before starting {kind!r}.",
                        data={"busy": True, "running": existing},
                    )
                return
            cancellation.begin(kind, op_id)
            # Fresh op → fresh throttle clock, so its first progress tick isn't
            # suppressed by the previous op's last-emit timestamp.
            _reset_progress_throttle()

    try:
        result = handler(params)
    except cancellation.CancelledError as e:
        # Per JSON-RPC 2.0 the server must not reply to a notification (no id),
        # even on failure. The success path already guards this; mirror it on
        # every error branch so a failed notification stays silent.
        if req_id is not None:
            _err(req_id, APP_ERROR, str(e), data={"cancelled": True})
    except (FileNotFoundError, NotADirectoryError) as e:
        # A missing folder / file or unmounted drive is user input, not a
        # server fault — surface a clean param error, not an APP_ERROR carrying
        # a scary traceback. Common in the GUI when a recent library lives on a
        # removed USB / unmounted network share, or a saved tag-backup file was
        # deleted. (FileNotFoundError/NotADirectoryError are OSError subclasses,
        # so they bypass the ValueError branch below and would otherwise hit the
        # generic Exception handler.)
        log.info("Path not found in method %s: %s", method, e)
        if req_id is not None:
            _err(req_id, INVALID_PARAMS, str(e))
    except (TypeError, KeyError, ValueError) as e:
        log.exception("Invalid params to method %s", method)
        if req_id is not None:
            _err(req_id, INVALID_PARAMS, f"Invalid params: {e}")
    except Exception as e:  # noqa: BLE001
        log.exception("Handler raised for method %s", method)
        if req_id is not None:
            _err(req_id, APP_ERROR, str(e), data={"traceback": traceback.format_exc()})
    else:
        if req_id is not None:
            _ok(req_id, result)
    finally:
        if kind is not None:
            cancellation.end()


# ---------------------------------------------------------------------------
# Startup hygiene: install-path probe
# ---------------------------------------------------------------------------


# Substrings (case-insensitive) that almost always mean the install lives on a
# virtual filesystem with no mmap support — PyInstaller bootloader can hang
# or crash on first launch loading shared libs from these paths.
_RISKY_PATH_SUBSTRINGS: tuple[str, ...] = (
    "my drive",   # Google Drive File Stream
    "googledrive",
    "onedrive",
    "icloud",
    "dropbox",    # also virtual under "Online-only" mode
)

# Heuristic thresholds. > 200 chars is a real Windows MAX_PATH ceiling concern
# (CreateProcess / dlopen quirks); > 2 spaces is anecdotal but correlates
# strongly with bizarre quoting bugs in downstream Popen invocations.
_RISKY_PATH_MAX_LEN: int = 200
_RISKY_PATH_MAX_SPACES: int = 2


_DEFAULT_EXECUTABLE: Any = object()  # sentinel — see _probe_install_path docstring


def _probe_install_path(executable: Any = _DEFAULT_EXECUTABLE) -> dict | None:
    """Inspect `sys.executable` for known-problematic install locations.

    Returns a `notify`-shaped params dict (without the JSON-RPC envelope), or
    `None` if the path looks fine. Caller wraps in `{"jsonrpc":..., "method":
    "notify", "params": ...}` and writes to stdout.

    The probe is intentionally cheap and conservative — false positives here
    are mildly annoying (a banner appears that the user dismisses), but a
    missed positive means the sidecar may hang on first launch with no
    explanation.

    Pass `None` (or `""`) explicitly to mean "no resolvable path" — that's
    the PyInstaller-frozen edge case where `sys.executable` is falsy and
    we have nothing to inspect.
    """
    if executable is _DEFAULT_EXECUTABLE:
        path = sys.executable
    else:
        path = executable
    if not path:
        return None
    # We compare against a normalized form so e.g. "C:/Users/dj/My Drive"
    # and "C:\\Users\\dj\\My Drive" both match the substring rule.
    norm = path.replace("\\", "/")
    norm_lower = norm.lower()

    reasons: list[str] = []
    for needle in _RISKY_PATH_SUBSTRINGS:
        if needle in norm_lower:
            # Capitalize for the human-readable detail. We don't need to
            # preserve the exact original casing — the path itself is in the
            # `path` field already.
            reasons.append(f"contains '{needle}'")
            break  # one match is enough; further substrings would be redundant
    if path.count(" ") > _RISKY_PATH_MAX_SPACES:
        reasons.append(f"has {path.count(' ')} spaces in the path")
    if len(path) > _RISKY_PATH_MAX_LEN:
        reasons.append(f"is {len(path)} characters long (>{_RISKY_PATH_MAX_LEN})")

    if not reasons:
        return None

    return {
        "level": "warning",
        "message": (
            "Vibechek is installed in a location that may cause launch "
            "issues. If the app behaves erratically, try reinstalling to "
            "a simple path like C:\\Vibechek\\."
        ),
        "detail": "Install path " + ", ".join(reasons) + ".",
        "path": path,
        "reasons": reasons,
    }


def _silence_native_logs() -> None:
    """Reduce native stdout noise that breaks JSON-RPC framing.

    TensorFlow, CUDA, and essentia C++ code write directly to fd 1 via
    `printf()` / `fprintf(stdout, ...)`, bypassing Python's `sys.stdout`
    and our `_StdoutWriter` lock. When that happens mid-frame, the Rust
    side's line-delimited JSON parser sees a malformed line and (until
    the Rust-side fix) loses the response that follows.

    These env vars only suppress the loudest sources — they MUST be set
    before any of the offending libraries import. Setting them here in
    `serve()` is early enough because we only import `tensorflow` /
    `essentia` lazily inside the method handlers.
    """
    # 0=all, 1=info, 2=warning, 3=error+. 3 silences the noisy device-init
    # and op-placement logs TF emits to stdout on startup.
    os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "3")
    # NVIDIA cuDNN's own logger. 0=disable, 3=error+.
    os.environ.setdefault("CUDNN_LOGLEVEL_DBG", "0")
    # CUDA driver runtime: suppress non-fatal log messages.
    os.environ.setdefault("CUDA_MODULE_LOADING", "LAZY")


def _cleanup_stale_tempfiles() -> None:
    """Best-effort sweep of `vibechek-wsl-*` files older than 24h.

    `_stage_script_for_wsl` and `run_vibechek_in_wsl` write tempfiles
    that are normally `unlink`'d in a `finally` block, but a hard kill (Tauri
    crash, OS reboot, force-quit) leaks them. Sweeps `%TEMP%` so they don't
    accumulate over months.
    """
    import tempfile as _tempfile
    import time as _time
    try:
        tmp = Path(_tempfile.gettempdir())
        cutoff = _time.time() - 24 * 3600
        for pattern in ("vibechek-wsl-*.sh", "vibechek-wsl-pid-*.txt",
                        "vibechek-wsl-*.json"):
            for f in tmp.glob(pattern):
                try:
                    if f.stat().st_mtime < cutoff:
                        f.unlink()
                except OSError:
                    pass
    except Exception as e:  # noqa: BLE001
        log.debug("Tempfile sweep failed (non-fatal): %s", e)


def serve(stdin=None, stdout=None) -> None:
    """Read JSON-RPC requests from stdin and write responses to stdout.

    The dispatch loop is single-threaded (reads from stdin) but each request
    is handed to a thread pool, so a long operation (analyze, dedupe) doesn't
    block fast requests (config, system_info, preflight). The stdout writer
    is mutex-protected so concurrent threads never tear each other's frames.

    Blocks until EOF on stdin (i.e. parent process closes our stdin).
    """
    # Silence native (TF/CUDA/essentia) printf-to-stdout noise BEFORE any
    # of those libraries get a chance to import. Setting these
    # after first import is a no-op — the libs cache the value.
    _silence_native_logs()

    # Configure logging early so anything emitted during startup goes to file
    try:
        from vibechek import logging_setup
        logging_setup.configure()
    except Exception:  # noqa: BLE001
        # Logging is nice-to-have; don't take down the sidecar over it
        pass

    # Sweep stale tempfiles from previous sidecar runs. Cheap;
    # bounded by `vibechek-wsl-*` glob + 24h age cutoff. If sidecar was
    # force-killed mid-WSL-install, these accumulate forever.
    _cleanup_stale_tempfiles()

    stdin = stdin or sys.stdin
    out_stream = stdout or sys.stdout

    # Force UTF-8 on the JSON-RPC channel — do NOT rely on PYTHONUTF8 reaching us.
    # In the packaged desktop app the sidecar is launched WITHOUT a console; in
    # that console-less/frozen configuration Python can still pick the legacy
    # ANSI code page (cp1252 on Windows) for sys.stdin even though the Rust shell
    # sets PYTHONUTF8=1. Non-ASCII track paths the GUI sends back over stdin
    # (accented artist names — "Tiësto", "Ultra Naté", "Années 90") then decode
    # as mojibake ("TiÃ«sto"), so Path.exists() fails and organize/tag report
    # every such file "not found" and silently skip it. cli.py already forces
    # UTF-8 on stdout/stderr but not stdin; pin both here so the channel is
    # UTF-8 regardless of entry point. A test-supplied StringIO/queue stub has no
    # reconfigure() (or is already text) — leave it alone.
    for _stream in (stdin, out_stream):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    global _writer
    _writer = _StdoutWriter(out_stream)

    # Announce ourselves so the host knows the sidecar is alive
    _write_message({
        "jsonrpc": "2.0",
        "method": "ready",
        "params": {"version": __version__, "methods": sorted(METHODS.keys())},
    })

    # Probe our own install path for known-bad locations. If
    # PyInstaller's bootloader is going to hang or crash on first launch
    # (My Drive, OneDrive, iCloud, > 200 char paths, etc.), warn the user
    # so they can move the install before hitting it. Non-blocking: this is
    # a `notify` event the frontend can route to its notification store.
    warning = _probe_install_path()
    if warning is not None:
        _write_message({
            "jsonrpc": "2.0",
            "method": "notify",
            "params": warning,
        })

    log.info("Sidecar serving on stdin/stdout (workers=%d, methods=%d)",
             _DISPATCH_WORKERS, len(METHODS))

    pool = ThreadPoolExecutor(max_workers=_DISPATCH_WORKERS, thread_name_prefix="rpc")
    try:
        for line in stdin:
            line = line.strip()
            if not line:
                continue

            try:
                request = json.loads(line)
            except json.JSONDecodeError as e:
                _err(None, PARSE_ERROR, f"Parse error: {e}")
                continue

            if not isinstance(request, dict) or request.get("jsonrpc") != "2.0":
                _err(request.get("id") if isinstance(request, dict) else None,
                     INVALID_REQUEST, "Invalid request (must be JSON-RPC 2.0)")
                continue

            pool.submit(_dispatch, request)
    finally:
        log.info("Sidecar shutting down (stdin closed)")
        # Signal the cooperative cancellation flag BEFORE tearing down the
        # pool. `cancel_futures=True` only drops QUEUED futures, not the one
        # already executing — an in-flight analyze / WSL install would
        # otherwise keep running (and its spawned subprocesses orphaned) after
        # the sidecar's stdin closed. Setting the cancel flag gives the running
        # long-op loop (which checks cancellation.is_cancelled()) a chance to
        # unwind and kill its children.
        try:
            cancellation.cancel()
        except Exception:  # noqa: BLE001
            pass
        pool.shutdown(wait=False, cancel_futures=True)


__all__ = ["serve", "METHODS"]
