"""Project-wide logging configuration.

One call to `configure()` sets up:
  - File log: `<data_dir>/logs/vibechek.log`, rotating at 10 MB, 5 backups.
  - Stderr log: warnings and above (matches what was already going to stderr).

Called at the entry of the CLI and the RPC sidecar. Idempotent.

`tail()` returns the last N lines from the current log file — used by the
RPC `get_log_tail` method so the GUI can show what just happened without
asking the user to find the file.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import sys
from typing import Any

from vibechek.config import DATA_DIR

log = logging.getLogger(__name__)

LOG_DIR = DATA_DIR / "logs"
LOG_FILE = LOG_DIR / "vibechek.log"

# Durable, compact history of completed analyze runs — one JSON object per line.
# The rolling app log (above) rotates on size and gets buried under per-track
# chatter, so a "what did my last run actually decide?" question (engine, worker
# split, GPU fallback reason, error count) had nowhere to be read back from.
# `doctor`'s last-run section reads this file; it's capped to the last N entries
# so it never grows without bound.
RUN_HISTORY_FILE = LOG_DIR / "run_history.jsonl"
_RUN_HISTORY_CAP = 50

_configured = False

_FORMATTER = logging.Formatter(
    fmt="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


def configure(level: str = "INFO") -> None:
    """Set up root logging. Safe to call multiple times — second call is a no-op."""
    global _configured
    if _configured:
        return

    LOG_DIR.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Drop any pre-existing handlers (uvicorn/pytest can leave noise)
    for h in list(root.handlers):
        root.removeHandler(h)

    # Rotating file handler — survives across runs
    fh = logging.handlers.RotatingFileHandler(
        LOG_FILE,
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    fh.setFormatter(_FORMATTER)
    fh.setLevel(logging.DEBUG)
    root.addHandler(fh)

    # Console for warnings and above. The RPC sidecar reserves stdout for
    # JSON-RPC traffic, so we use stderr.
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(_FORMATTER)
    sh.setLevel(logging.WARNING)
    root.addHandler(sh)

    _configured = True
    logging.getLogger(__name__).info("Logging configured at level %s; file=%s", level, LOG_FILE)


def tail(n: int = 200) -> list[str]:
    """Return the last `n` lines of the current log file (most recent last)."""
    if not LOG_FILE.exists():
        return []
    try:
        # Simple O(n) read of the tail — log file is bounded by rotation
        with open(LOG_FILE, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        return [line.rstrip("\n") for line in lines[-n:]]
    except OSError:
        return []


def append_run_summary(entry: dict[str, Any], *, cap: int = _RUN_HISTORY_CAP) -> None:
    """Append one analyze-run summary line to `RUN_HISTORY_FILE`, keeping the
    last `cap` entries.

    Best-effort by design: a write failure here must NEVER affect the analyze
    result the user just waited on, so every error is swallowed to the log. We
    rewrite the whole (small, capped) file each call rather than open-append so
    the rotation stays atomic — analyze runs are serialized in the sidecar, so
    there's no concurrent writer to race.
    """
    try:
        RUN_HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        lines: list[str] = []
        if RUN_HISTORY_FILE.exists():
            with open(RUN_HISTORY_FILE, encoding="utf-8", errors="replace") as f:
                lines = [ln for ln in f.read().splitlines() if ln.strip()]
        # default=str so a stray Path/enum never turns a diagnostics write into
        # an exception on the analyze completion path.
        lines.append(json.dumps(entry, default=str))
        lines = lines[-cap:]
        tmp = RUN_HISTORY_FILE.with_name(RUN_HISTORY_FILE.name + ".tmp")
        tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        tmp.replace(RUN_HISTORY_FILE)
    except Exception as e:  # noqa: BLE001 — diagnostics must not break analyze
        log.warning("Could not append run-history summary: %s", e)


def read_run_history(n: int = _RUN_HISTORY_CAP) -> list[dict[str, Any]]:
    """Return up to the last `n` run summaries (oldest first). Never raises."""
    if not RUN_HISTORY_FILE.exists():
        return []
    try:
        with open(RUN_HISTORY_FILE, encoding="utf-8", errors="replace") as f:
            raw = [ln for ln in f.read().splitlines() if ln.strip()]
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for ln in raw[-n:]:
        try:
            obj = json.loads(ln)
        except json.JSONDecodeError:
            continue  # skip a partially-written / corrupt line, keep the rest
        if isinstance(obj, dict):
            out.append(obj)
    return out


def last_run_summary() -> dict[str, Any] | None:
    """The most recent run summary, or None if no run has been recorded."""
    hist = read_run_history(1)
    return hist[-1] if hist else None


__all__ = [
    "configure",
    "tail",
    "LOG_FILE",
    "RUN_HISTORY_FILE",
    "append_run_summary",
    "read_run_history",
    "last_run_summary",
]
