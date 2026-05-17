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
import traceback
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Callable

from vibechek import __version__
from vibechek.config import (
    AnalysisConfig,
    DuplicateConfig,
    OrganizationConfig,
    TaggingConfig,
    VibechekConfig,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Wire format
# ---------------------------------------------------------------------------


def _write_message(msg: dict[str, Any]) -> None:
    """Write a single JSON-RPC message to stdout, flushed immediately."""
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


def _analyze_directory(params: dict) -> dict:
    """Full ML analysis. Emits progress notifications."""
    from vibechek.analyzer import analyze_directory

    config = AnalysisConfig(workers=int(params.get("workers", 1)))
    if "models_dir" in params and params["models_dir"]:
        config.models_dir = Path(params["models_dir"])

    return analyze_directory(
        Path(params["path"]),
        config=config,
        on_progress=_emit_progress,
        output_path=Path(params["output_path"]) if params.get("output_path") else None,
        skip=int(params.get("skip", 0)),
        limit=int(params.get("limit") or 0) or None,
    )


def _find_duplicates(params: dict) -> dict:
    from vibechek.duplicates import find_duplicates

    config = DuplicateConfig(
        use_md5=bool(params.get("use_md5", True)),
        use_chromaprint=bool(params.get("use_chromaprint", True)),
        chromaprint_similarity_threshold=float(params.get("threshold", 0.95)),
        action=params.get("action", "report"),
        review_folder=Path(params["review_folder"]) if params.get("review_folder") else None,
    )
    report = find_duplicates(Path(params["path"]), config, on_progress=_emit_progress)
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
    from vibechek.duplicates import DuplicateGroup, DuplicateReport, FileInfo

    def _group(g: dict) -> DuplicateGroup:
        keeper = FileInfo(**g["keep"])
        dupes = [FileInfo(**x) for x in g["duplicates"]]
        return DuplicateGroup(
            method=g["method"],
            key=g["key"],
            keeper=keeper,
            duplicates=dupes,
            recoverable_mb=g["recoverable_mb"],
        )

    return DuplicateReport(
        total_files=d.get("summary", {}).get("total_files", 0),
        exact_groups=[_group(g) for g in d.get("exact_duplicates", [])],
        audio_groups=[_group(g) for g in d.get("audio_duplicates", [])],
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

    stats = backup_tags(
        Path(params["path"]),
        Path(params["output_path"]),
        on_progress=_emit_progress,
    )
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
    """Return the default config — for now the GUI is the source of truth."""
    return asdict(VibechekConfig())


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
    "scan_directory": _scan_directory,
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
}

# JSON-RPC error codes
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
APP_ERROR = -32000


def serve(stdin=None, stdout=None) -> None:
    """Read JSON-RPC requests from stdin and write responses to stdout.

    Blocks until EOF on stdin (i.e. parent process closes our stdin).
    """
    stdin = stdin or sys.stdin
    # Re-route global writers if the caller passed a different stdout (for tests)
    if stdout is not None:
        global _write_message

        def _write(msg: dict[str, Any]) -> None:
            stdout.write(json.dumps(msg, default=_json_default) + "\n")
            stdout.flush()

        _write_message = _write  # type: ignore[assignment]

    # Announce ourselves so the host knows the sidecar is alive
    _write_message({
        "jsonrpc": "2.0",
        "method": "ready",
        "params": {"version": __version__, "methods": sorted(METHODS.keys())},
    })

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

        req_id = request.get("id")
        method = request.get("method")
        params = request.get("params") or {}

        if not method:
            _err(req_id, INVALID_REQUEST, "Missing 'method'")
            continue

        handler = METHODS.get(method)
        if handler is None:
            _err(req_id, METHOD_NOT_FOUND, f"Method not found: {method}")
            continue

        try:
            result = handler(params)
        except (TypeError, KeyError, ValueError) as e:
            _err(req_id, INVALID_PARAMS, f"Invalid params: {e}")
        except Exception as e:  # noqa: BLE001
            log.exception("Handler raised for method %s", method)
            _err(req_id, APP_ERROR, str(e), data={"traceback": traceback.format_exc()})
        else:
            if req_id is not None:
                _ok(req_id, result)


__all__ = ["serve", "METHODS"]
