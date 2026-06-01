"""Cooperative cancellation for long-running sidecar operations.

The sidecar processes JSON-RPC requests serially, but long ops (analyze,
dedupe, organize, install) run in background-friendly loops that check this
module's flag periodically. Calling `cancel()` from the RPC handler sets the
flag; calling `is_cancelled()` from inside a loop returns True; callers raise
`CancelledError` to unwind.

Each operation `begin()`s a new run, which clears the flag from the previous
one. Cancellation is best-effort: subprocesses (PyInstaller, multiprocessing
pools, WSL spawns) need to be terminated by their managers, not just by
checking this flag.
"""

from __future__ import annotations

import threading


class CancelledError(RuntimeError):
    """Raised by an operation when it detects a cancel request."""


_flag = threading.Event()
_current_kind: str | None = None
_lock = threading.Lock()


def begin(kind: str) -> None:
    """Start a new cancellable operation. Clears any prior cancel state."""
    global _current_kind
    with _lock:
        _flag.clear()
        _current_kind = kind


def end() -> None:
    """Mark the current operation as finished. Clears the cancel flag."""
    global _current_kind
    with _lock:
        _flag.clear()
        _current_kind = None


def cancel() -> str | None:
    """Request cancellation. Returns the kind of op that was running, or None."""
    with _lock:
        if _current_kind is None:
            return None
        _flag.set()
        return _current_kind


def is_cancelled() -> bool:
    return _flag.is_set()


def current_kind() -> str | None:
    with _lock:
        return _current_kind


def check() -> None:
    """Raise CancelledError if a cancel has been requested."""
    if _flag.is_set():
        raise CancelledError(f"Operation '{_current_kind}' cancelled by user")


__all__ = [
    "CancelledError",
    "begin",
    "end",
    "cancel",
    "is_cancelled",
    "current_kind",
    "check",
]
