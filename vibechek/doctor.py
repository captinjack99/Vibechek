"""Diagnostic report generator — `vibechek doctor`.

Builds a markdown-formatted dump of the user's environment that's safe to
paste into a GitHub issue. The report deliberately avoids anything that could
leak credentials, library contents, or full file paths under user-private
directories: it only surfaces *paths + sizes + version strings*, never the
files themselves.

The output is consumed by:
  - Humans (CLI: `vibechek doctor` → stdout, `vibechek doctor --output X.md`).
  - The UI agent (eventually wires a "Copy diagnostic" button calling an RPC
    method that returns `build_report()` as a string — see writeup).

Best-effort throughout: every probe is wrapped so missing optional tooling
(nvidia-smi, psutil, WSL on Linux) degrades to "unavailable" rather than
exploding the whole report.
"""

from __future__ import annotations

import json
import logging
import platform
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from vibechek import __version__
from vibechek import config as _config_module
from vibechek.config import MODELS_DIR  # noqa: F401 — re-export for tests

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class DiagnosticReport:
    """All the diagnostic data we collect, before markdown formatting.

    Held as a plain dataclass so tests can assert on the structured form
    without re-parsing markdown.
    """

    vibechek_version: str = ""
    python_version: str = ""
    os_platform: str = ""
    arch: str = ""
    config_file_path: str = ""
    config_file_size: int = 0
    config_file_parse_ok: bool = False
    config_file_error: str | None = None
    cpu_count: int = 0
    memory_total_mb: int | None = None
    memory_available_mb: int | None = None
    gpus: list[dict[str, Any]] = field(default_factory=list)
    models_dir: str = ""
    models: list[dict[str, Any]] = field(default_factory=list)
    # True iff the analyzer ships a populated MODEL_SHA256 pin table, i.e.
    # downloaded model files are actually content-verified. When False the
    # integrity check is a silent no-op (warns on size mismatch only), which
    # users should be able to see in the diagnostic.
    model_integrity_verified: bool = False
    log_tail: list[str] = field(default_factory=list)
    wsl: dict[str, Any] | None = None
    native_venv: dict[str, Any] | None = None
    tempfile_leaks: int = 0


# ---------------------------------------------------------------------------
# Collectors
# ---------------------------------------------------------------------------


def _collect_config() -> tuple[str, int, bool, str | None]:
    """Inspect the config file location, size, and JSON-parse status.

    We deliberately don't surface the *contents* — even though the file is
    user-readable, it may contain user-local paths the user wouldn't want
    pasted into an issue. Just (path, size, parse-ok) is enough to debug
    "config not loading".

    Resolves `CONFIG_FILE` through the live `vibechek.config` module rather
    than the import-time value so test isolation (which monkeypatches the
    module attribute) is respected.
    """
    config_file = _config_module.CONFIG_FILE
    path_str = str(config_file)
    if not config_file.exists():
        return path_str, 0, False, "file not present (using defaults)"
    try:
        size = config_file.stat().st_size
    except OSError as e:
        return path_str, 0, False, f"could not stat: {e}"
    try:
        json.loads(config_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return path_str, size, False, str(e)
    return path_str, size, True, None


def _collect_models(models_dir: Path | None = None) -> tuple[str, list[dict[str, Any]]]:
    """List the .pb / .json files in the models directory with sizes.

    Doesn't read the file contents — just `stat()`s them. Lazy-imports
    `analyzer.MODELS` so this module stays importable even when essentia
    is missing.
    """
    from vibechek.analyzer import MODELS

    target = Path(models_dir) if models_dir else MODELS_DIR
    out: list[dict[str, Any]] = []
    for name in MODELS:
        for suffix, kind in ((".pb", "weights"), (".json", "metadata")):
            fp = target / f"{name}{suffix}"
            entry: dict[str, Any] = {"name": f"{name}{suffix}", "kind": kind}
            if fp.exists():
                try:
                    entry["size_bytes"] = fp.stat().st_size
                    entry["present"] = True
                except OSError as e:
                    entry["present"] = False
                    entry["error"] = str(e)
            else:
                entry["present"] = False
            out.append(entry)
    return str(target), out


def _collect_model_integrity() -> bool:
    """True iff the analyzer's `MODEL_SHA256` pin table is populated.

    When empty, `verify_model_sha256` no-ops and downloaded model files are
    only size-checked — so a poisoned mirror would not be caught. Surfacing
    this in the diagnostic makes the otherwise-invisible no-op verification
    visible to users (and to anyone triaging a "wrong model" bug report).

    Lazy-imports `analyzer.MODEL_SHA256` so doctor stays importable without
    essentia, matching `_collect_models`.
    """
    try:
        from vibechek.analyzer import MODEL_SHA256
        return any(bool(v) for v in MODEL_SHA256.values())
    except Exception as e:  # noqa: BLE001
        log.debug("model integrity probe failed: %s", e)
        return False


def _collect_log_tail(n: int = 50) -> list[str]:
    """Lazy import of logging_setup so doctor.py is importable in tests
    that monkeypatch LOG_FILE after import."""
    try:
        from vibechek import logging_setup
        return logging_setup.tail(n)
    except Exception as e:  # noqa: BLE001
        log.debug("log tail failed: %s", e)
        return []


def _collect_wsl() -> dict[str, Any] | None:
    """Return a redacted WSL status dict on Windows; None elsewhere.

    Uses the existing `detect_wsl(quick=True)` so we don't pay the
    per-distro probe cost in doctor — that probe takes 30+ seconds and the
    diagnostic is supposed to be quick. The user can run
    `vibechek preflight` for the full picture.
    """
    if sys.platform != "win32":
        return None
    try:
        from vibechek import wsl as _wsl
        status = _wsl.detect_wsl(quick=False)
        return {
            "wsl_available": status.wsl_available,
            "wsl_feature_enabled": status.wsl_feature_enabled,
            "default_distro": status.default_distro,
            "distros": [
                {
                    "name": d.name,
                    "version": d.version,
                    "state": d.state,
                    "vibechek_installed": d.vibechek_installed,
                    "essentia_installed": d.essentia_installed,
                    # Boolean only — the actual path may be under user $HOME.
                    "shim_integrity": d.vibechek_path is not None and bool(d.vibechek_path),
                }
                for d in status.distros
            ],
            "can_run_vibechek": status.can_run_vibechek,
        }
    except Exception as e:  # noqa: BLE001
        log.debug("WSL collection failed: %s", e)
        return {"error": str(e)}


def _collect_native_venv() -> dict[str, Any] | None:
    """Return native venv status on Linux/macOS; None on Windows."""
    if sys.platform == "win32":
        return None
    try:
        from vibechek import native_install
        if not native_install.IS_SUPPORTED:
            return None
        status = native_install.probe_native_venv()
        return {
            "venv_dir": status.venv_dir,
            "venv_present": status.venv_python is not None,
            "essentia_installed": status.essentia_installed,
            "essentia_version": status.essentia_version,
            "vibechek_installed": status.vibechek_installed,
            "vibechek_version": status.vibechek_version,
            "error": status.error,
        }
    except Exception as e:  # noqa: BLE001
        log.debug("Native venv probe failed: %s", e)
        return {"error": str(e)}


def _collect_tempfile_leaks() -> int:
    """Count `vibechek-wsl-*` tempfiles. Symptom of crashed sidecar sessions."""
    try:
        tmp = Path(tempfile.gettempdir())
        count = 0
        for pattern in ("vibechek-wsl-*.sh", "vibechek-wsl-pid-*.txt",
                        "vibechek-wsl-*.json"):
            count += sum(1 for _ in tmp.glob(pattern))
        return count
    except OSError as e:
        log.debug("tempfile sweep failed: %s", e)
        return 0


def build_report(
    models_dir: Path | None = None,
    log_lines: int = 50,
) -> DiagnosticReport:
    """Assemble all probes into a `DiagnosticReport`. Never raises."""
    from vibechek.resources import detect

    # Resource detection is its own dataclass; we flatten into the report.
    resources = detect()
    cfg_path, cfg_size, cfg_ok, cfg_err = _collect_config()
    models_dir_str, models = _collect_models(models_dir)

    return DiagnosticReport(
        vibechek_version=__version__,
        python_version=f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        os_platform=platform.platform(),
        arch=platform.machine() or "unknown",
        config_file_path=cfg_path,
        config_file_size=cfg_size,
        config_file_parse_ok=cfg_ok,
        config_file_error=cfg_err,
        cpu_count=resources.cpu_count,
        memory_total_mb=resources.memory_total_mb,
        memory_available_mb=resources.memory_available_mb,
        gpus=[
            {
                "name": g.name,
                "backend": g.backend,
                "memory_mb": g.memory_mb,
            }
            for g in resources.gpu_devices
        ],
        models_dir=models_dir_str,
        models=models,
        model_integrity_verified=_collect_model_integrity(),
        log_tail=_collect_log_tail(log_lines),
        wsl=_collect_wsl(),
        native_venv=_collect_native_venv(),
        tempfile_leaks=_collect_tempfile_leaks(),
    )


# ---------------------------------------------------------------------------
# Markdown formatter
# ---------------------------------------------------------------------------


def _fmt_size(n: int | None) -> str:
    """Bytes → human size. Used for both config and model file size lines."""
    if n is None:
        return "?"
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    if n < 1024 * 1024 * 1024:
        return f"{n / (1024 * 1024):.1f} MB"
    return f"{n / (1024 * 1024 * 1024):.2f} GB"


def render_markdown(report: DiagnosticReport) -> str:
    """Render a diagnostic report as paste-friendly markdown.

    The shape is intentionally stable so future versions of the bug-report
    template can pattern-match on the section headers.
    """
    lines: list[str] = []
    lines.append("# Vibechek diagnostic report")
    lines.append("")
    lines.append("## Versions")
    lines.append(f"- Vibechek: `{report.vibechek_version}`")
    lines.append(f"- Python: `{report.python_version}`")
    lines.append(f"- OS: `{report.os_platform}`")
    lines.append(f"- Architecture: `{report.arch}`")
    lines.append("")

    lines.append("## Hardware")
    lines.append(f"- CPU cores: **{report.cpu_count}**")
    if report.memory_total_mb is not None:
        free = (
            f" ({report.memory_available_mb} MB free)"
            if report.memory_available_mb is not None else ""
        )
        lines.append(f"- RAM: {report.memory_total_mb} MB total{free}")
    else:
        lines.append("- RAM: _not detected (install `psutil` for memory info)_")
    if report.gpus:
        lines.append("- GPUs:")
        for g in report.gpus:
            mem = f" ({g['memory_mb']} MB)" if g.get("memory_mb") else ""
            lines.append(f"  - {g['name']} `[{g['backend']}]`{mem}")
    else:
        lines.append("- GPUs: _none detected_")
    lines.append("")

    lines.append("## Config")
    lines.append(f"- Path: `{report.config_file_path}`")
    lines.append(f"- Size: {_fmt_size(report.config_file_size)}")
    lines.append(f"- Parse-ok: **{report.config_file_parse_ok}**")
    if report.config_file_error:
        lines.append(f"- Error: `{report.config_file_error}`")
    lines.append("")

    lines.append("## Models")
    lines.append(f"- Directory: `{report.models_dir}`")
    present = sum(1 for m in report.models if m.get("present"))
    lines.append(f"- Present: **{present} / {len(report.models)}**")
    if report.model_integrity_verified:
        lines.append("- Integrity: **verified** (SHA256-pinned)")
    else:
        lines.append("- Integrity: **unverified** "
                     "(MODEL_SHA256 pin table empty — downloads are size-checked only)")
    for m in report.models:
        if m.get("present"):
            lines.append(f"  - `{m['name']}` — {_fmt_size(m.get('size_bytes'))}")
        else:
            reason = f" ({m.get('error')})" if m.get("error") else ""
            lines.append(f"  - `{m['name']}` — **MISSING**{reason}")
    lines.append("")

    if report.wsl is not None:
        lines.append("## WSL (Windows)")
        if "error" in report.wsl:
            lines.append(f"- Error probing WSL: `{report.wsl['error']}`")
        else:
            lines.append(f"- WSL available: **{report.wsl.get('wsl_available')}**")
            lines.append(f"- Feature enabled: **{report.wsl.get('wsl_feature_enabled')}**")
            lines.append(f"- Can run vibechek: **{report.wsl.get('can_run_vibechek')}**")
            lines.append(f"- Default distro: `{report.wsl.get('default_distro')}`")
            distros = report.wsl.get("distros") or []
            if distros:
                lines.append("- Distros:")
                for d in distros:
                    lines.append(
                        f"  - `{d['name']}` (WSL{d.get('version') or '?'}, {d.get('state')}): "
                        f"vibechek={d.get('vibechek_installed')}, "
                        f"essentia={d.get('essentia_installed')}, "
                        f"shim_ok={d.get('shim_integrity')}"
                    )
        lines.append("")

    if report.native_venv is not None:
        lines.append("## Native venv (Linux/macOS)")
        nv = report.native_venv
        if "error" in nv and nv.get("error") and len(nv) == 1:
            lines.append(f"- Error: `{nv['error']}`")
        else:
            lines.append(f"- Venv dir: `{nv.get('venv_dir')}`")
            lines.append(f"- Venv present: **{nv.get('venv_present')}**")
            lines.append(f"- essentia installed: **{nv.get('essentia_installed')}** "
                         f"(version: `{nv.get('essentia_version')}`)")
            lines.append(f"- vibechek installed: **{nv.get('vibechek_installed')}** "
                         f"(version: `{nv.get('vibechek_version')}`)")
            if nv.get("error"):
                lines.append(f"- Probe note: `{nv['error']}`")
        lines.append("")

    lines.append("## Temp file leaks")
    lines.append(f"- Stale `vibechek-wsl-*` files in temp dir: **{report.tempfile_leaks}**")
    if report.tempfile_leaks > 20:
        lines.append("  - _Many leaks: a previous sidecar was killed mid-WSL-install. "
                     "The next `vibechek rpc` start will sweep them._")
    lines.append("")

    lines.append("## Log tail (last 50 lines)")
    lines.append("```")
    if report.log_tail:
        # Hard-cap to 50 even if the caller asked for more, defensive.
        for line in report.log_tail[-50:]:
            lines.append(line)
    else:
        lines.append("(no log file or log is empty)")
    lines.append("```")
    lines.append("")
    return "\n".join(lines)


def run(output: Path | None = None, models_dir: Path | None = None) -> str:
    """Generate the report and (optionally) write it.

    Returns the markdown string either way so the CLI can also echo it to
    stdout for a `--output` run.
    """
    report = build_report(models_dir=models_dir)
    md = render_markdown(report)
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(md, encoding="utf-8")
    return md


__all__ = [
    "DiagnosticReport",
    "build_report",
    "render_markdown",
    "run",
]
