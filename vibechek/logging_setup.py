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

import logging
import logging.handlers
import sys
from pathlib import Path

from vibechek.config import DATA_DIR

LOG_DIR = DATA_DIR / "logs"
LOG_FILE = LOG_DIR / "vibechek.log"

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
        return [l.rstrip("\n") for l in lines[-n:]]
    except OSError:
        return []


__all__ = ["configure", "tail", "LOG_FILE"]
