"""JSON-RPC server for the desktop sidecar.

The Tauri shell spawns `vibechek rpc` as a child process and communicates with
it via line-delimited JSON-RPC 2.0 over stdin/stdout. Long-running operations
emit `progress` notifications during execution.

Protocol (one JSON object per line):

    Request:       {"jsonrpc":"2.0","id":1,"method":"dedupe","params":{"path":"..."}}
    Response:      {"jsonrpc":"2.0","id":1,"result":{...}}
    Error:         {"jsonrpc":"2.0","id":1,"error":{"code":-32000,"message":"..."}}
    Notification:  {"jsonrpc":"2.0","method":"progress","params":{"current":50,"total":100,"message":"..."}}

stdout is reserved for protocol traffic only. All logging goes to stderr.
"""

from __future__ import annotations

import json
import logging
import sys
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
import dataclasses
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Callable

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


def _emit_progress(current: int, total: int, message: str = "") -> None:
    _write_message({
        "jsonrpc": "2.0",
        "method": "progress",
        "params": {"current": current, "total": total, "message": message},
    })


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
    from vibechek.analyzer import analyze_directory
    from vibechek import library_state

    config = AnalysisConfig(
        workers=int(params.get("workers", 0)),
        use_gpu=str(params.get("use_gpu", "auto")),
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
        output_path=Path(params["output_path"]) if params.get("output_path") else None,
        skip=int(params.get("skip", 0)),
        limit=int(params.get("limit") or 0) or None,
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
    info = probe_engine_gpu(distro, force=force)
    return engine_gpu_info_to_dict(info)


def _preflight(params: dict) -> dict:
    """Verify essentia + models are ready for `analyze_directory`."""
    from vibechek.preflight import preflight, to_dict
    models_dir = Path(params["models_dir"]) if params.get("models_dir") else None
    return to_dict(preflight(models_dir))


def _wsl_status(params: dict) -> dict:
    """Report WSL detection results.

    `quick=True` returns immediately without probing distros for
    vibechek/essentia (which can take 30+ sec). Default is full probe.
    """
    from vibechek.wsl import detect_wsl, to_dict
    quick = bool(params.get("quick", False))
    return to_dict(detect_wsl(quick=quick))


def _install_wsl(params: dict) -> dict:
    """Install WSL + the named distro via elevated PowerShell. Triggers UAC."""
    from vibechek.wsl import install_wsl
    distro = str(params.get("distro", "Ubuntu-24.04"))
    return install_wsl(distro=distro, on_progress=_emit_progress)


def _install_vibechek_in_wsl(params: dict) -> dict:
    """Install vibechek + essentia-tensorflow + chromaprint inside a WSL distro."""
    from vibechek.wsl import install_vibechek_in_wsl
    distro = str(params["distro"])
    return install_vibechek_in_wsl(distro, on_progress=_emit_progress)


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

    config = DuplicateConfig(
        use_md5=bool(params.get("use_md5", True)),
        use_chromaprint=bool(params.get("use_chromaprint", True)),
        chromaprint_similarity_threshold=float(params.get("threshold", 0.95)),
        action=params.get("action", "report"),
        review_folder=Path(params["review_folder"]) if params.get("review_folder") else None,
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
    )
    return report.to_dict()


def _handle_duplicates(params: dict) -> dict:
    from vibechek.duplicates import (
        DuplicateGroup,
        DuplicateReport,
        FileInfo,
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
    from vibechek.duplicates import (
        DuplicateGroup, DuplicateReport, DuplicateSummary, FileInfo,
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
    stats = organize_from_analysis(
        analysis_data,
        config,
        on_progress=_emit_progress,
        dry_run=bool(params.get("dry_run", False)),
    )
    return asdict(stats)


def _apply_ml_tags(params: dict) -> dict:
    from vibechek.tagger import apply_ml_tags

    config = TaggingConfig(
        genre_confidence_threshold=float(params.get("confidence", 0.85)),
        skip_bpm_and_key=bool(params.get("skip_bpm_and_key", True)),
        preserve_rekordbox_frames=bool(params.get("preserve_rekordbox_frames", True)),
    )
    analysis_data = _load_analysis_payload(params)
    stats = apply_ml_tags(
        analysis_data, config,
        on_progress=_emit_progress,
        dry_run=bool(params.get("dry_run", False)),
    )
    return asdict(stats)


def _backup_tags(params: dict) -> dict:
    from vibechek.tagger import backup_tags
    from vibechek import backup_history

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

    stats = restore_tags(Path(params["backup_path"]), on_progress=_emit_progress)
    return asdict(stats)


def _download_models(params: dict) -> dict:
    from vibechek.analyzer import download_models
    from vibechek.config import MODELS_DIR

    target = Path(params["models_dir"]) if params.get("models_dir") else MODELS_DIR
    descriptors = download_models(target, on_progress=_emit_progress)
    return {"models_dir": str(target), "models": list(descriptors.keys())}


def _get_config(_params: dict) -> dict:
    """Load config from disk (or defaults if no file exists yet)."""
    return _config_to_jsonable(VibechekConfig.load())


def _save_config(params: dict) -> dict:
    """Persist a VibechekConfig dict to disk. Returns the saved file path."""
    data = params.get("config", {})
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
        return json.loads(Path(params["analysis_path"]).read_text(encoding="utf-8"))
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
    "install_cuda_libs_in_wsl": _install_cuda_libs_in_wsl,
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
    "download_models": _download_models,
    "get_config": _get_config,
    "save_config": _save_config,
    "restore_default_config": _restore_default_config,
    "cancel_operation": _cancel_operation,
    "library_state": _library_state,
    "forget_library": _forget_library,
    "load_recent_analysis": _load_recent_analysis,
    "get_log_tail": _get_log_tail,
    "backup_history": _backup_history,
    "forget_backup": _forget_backup,
}

# Methods that run long ops and should be cancellable.
_CANCELLABLE_METHODS = {
    "analyze_directory": "analyze",
    "scan_only": "analyze",
    "find_duplicates": "dedupe",
    "organize": "organize",
    "apply_ml_tags": "tag",
    "backup_tags": "backup",
    "restore_tags": "restore",
    "download_models": "download-models",
    "install_wsl": "install-wsl",
    "install_vibechek_in_wsl": "install-essentia",
    "install_cuda_libs_in_wsl": "install-cuda",
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
        _err(req_id, INVALID_REQUEST, "Missing 'method'")
        return

    handler = METHODS.get(method)
    if handler is None:
        _err(req_id, METHOD_NOT_FOUND, f"Method not found: {method}")
        return

    kind = _CANCELLABLE_METHODS.get(method)
    if kind is not None:
        cancellation.begin(kind)

    try:
        result = handler(params)
    except cancellation.CancelledError as e:
        _err(req_id, APP_ERROR, str(e), data={"cancelled": True})
    except (TypeError, KeyError, ValueError) as e:
        log.exception("Invalid params to method %s", method)
        _err(req_id, INVALID_PARAMS, f"Invalid params: {e}")
    except Exception as e:  # noqa: BLE001
        log.exception("Handler raised for method %s", method)
        _err(req_id, APP_ERROR, str(e), data={"traceback": traceback.format_exc()})
    else:
        if req_id is not None:
            _ok(req_id, result)
    finally:
        if kind is not None:
            cancellation.end()


def serve(stdin=None, stdout=None) -> None:
    """Read JSON-RPC requests from stdin and write responses to stdout.

    The dispatch loop is single-threaded (reads from stdin) but each request
    is handed to a thread pool, so a long operation (analyze, dedupe) doesn't
    block fast requests (config, system_info, preflight). The stdout writer
    is mutex-protected so concurrent threads never tear each other's frames.

    Blocks until EOF on stdin (i.e. parent process closes our stdin).
    """
    # Configure logging early so anything emitted during startup goes to file
    try:
        from vibechek import logging_setup
        logging_setup.configure()
    except Exception:  # noqa: BLE001
        # Logging is nice-to-have; don't take down the sidecar over it
        pass

    stdin = stdin or sys.stdin

    global _writer
    _writer = _StdoutWriter(stdout or sys.stdout)

    # Announce ourselves so the host knows the sidecar is alive
    _write_message({
        "jsonrpc": "2.0",
        "method": "ready",
        "params": {"version": __version__, "methods": sorted(METHODS.keys())},
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
        pool.shutdown(wait=False, cancel_futures=True)


__all__ = ["serve", "METHODS"]
