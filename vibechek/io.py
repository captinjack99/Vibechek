"""Atomic file I/O helpers used across persistence call sites.

Why this exists: Vibechek writes a handful of JSON files that the user really
cannot afford to lose:

  - `config.json` (their settings — easy enough to recreate, but annoying)
  - `library_state.json` (their recent-libraries index)
  - `backup_history.json` (pointers to past tag backups)
  - **`analyses/<hash>.json`** (the 32 MB ML report for a 12k-track library;
    this represents 30+ minutes of GPU time the user does NOT want to redo)
  - `<user>/backup.json` (tag backup — losing this means losing the user's
    original tags before a destructive write)

A naive `path.write_text(json.dumps(...))` is NOT crash-safe. If the process is
killed, the disk fills up, or the machine loses power mid-write, the destination
file is truncated or zero-byte. The next launch then reads a corrupt JSON and
either crashes the GUI on parse or — worse — silently drops the user's state.

`atomic_write_json` writes to a sibling `.partial` file, fsyncs to make sure
the bytes are on disk, then `Path.replace()`s into place (POSIX atomic rename;
Windows uses MoveFileExW which is also atomic for same-volume renames). On any
exception the partial file is unlinked so we don't leave junk behind.

The temp name carries a per-writer suffix (`.<pid>.<tid>.partial`) so two
threads/processes writing the SAME destination at the same time don't truncate
each other's bytes or race on a shared temp file. Each writer builds its own
partial and the final `replace()` is the only contended step — and that step is
itself atomic, so the loser's bytes simply lose the race cleanly rather than
producing a torn file. (Concurrent *load-modify-save* on a single index file is
serialized separately by the caller via a module-level lock; this suffix closes
the lower-level torn-write window for everything else.)

Why we don't just use `tempfile.NamedTemporaryFile`:
  - It creates the temp file in the system temp dir by default, which can be on
    a different volume — `Path.replace()` would then fall back to non-atomic
    copy+delete and lose the crash-safety guarantee.

The sibling-partial pattern keeps the temp file on the SAME volume as the
destination, guaranteeing the rename is atomic.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


def atomic_write_json(
    path: Path,
    data: Any,
    indent: int = 2,
    *,
    ensure_ascii: bool = False,
    sort_keys: bool = False,
) -> None:
    """Serialize `data` to `path` as JSON, crash-safely.

    Writes to `<path>.partial`, fsyncs, then renames into place. On any
    exception during write or rename the partial file is unlinked so a
    retry doesn't see a half-written file.

    Args:
        path: Destination file. Parent directory must already exist; we don't
            create it here so callers can keep their own mkdir semantics.
        data: Any json-serializable object.
        indent: `json.dumps` indent (default 2 — matches existing call sites).
        ensure_ascii: Forwarded to `json.dumps`. Default False so non-ASCII
            characters round-trip verbatim (existing analyzer / tagger code
            already used False — keep parity).
        sort_keys: Forwarded to `json.dumps`. Default False, but config.save
            passes True (so it can diff cleanly across versions).

    Raises:
        OSError: If the write or rename fails (disk full, permission denied).
            The partial file is cleaned up before re-raising.
        TypeError / ValueError: From `json.dumps` if `data` isn't serializable.
            No partial file is created in this case.
    """
    path = Path(path)
    # Build the payload first — if json.dumps raises, we want no side effects
    # on disk (no zero-byte .partial dangling around).
    payload = json.dumps(
        data, indent=indent, ensure_ascii=ensure_ascii, sort_keys=sort_keys
    )

    # Unique-per-writer `.partial` suffix so concurrent writers (the dispatch
    # thread pool can run rename_library / tag_library / forget_backup at once)
    # each get their OWN temp file instead of truncating a shared
    # `<name>.partial`. Including pid+tid keeps it obvious from `ls` what file
    # the partial belongs to while still being collision-free across
    # threads/processes. Same volume as the destination, so the rename below is
    # guaranteed atomic.
    tmp = path.with_suffix(
        path.suffix + f".{os.getpid()}.{threading.get_ident()}.partial"
    )
    try:
        # Open + write + fsync. fsync forces the bytes to physical disk so a
        # power loss between write and rename doesn't leave a renamed-but-
        # empty file. The fsync cost (~1-10ms) is negligible vs. the 30+ min
        # of work the analysis report represents.
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            f.write(payload)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                # fsync can fail on some filesystems (e.g. some network mounts);
                # the write itself succeeded, so swallow and proceed. Worst case
                # the OS write-back cache loses unflushed bytes on a crash, which
                # is no worse than the pre-atomic implementation.
                pass
        # Atomic on POSIX; on Windows MoveFileExW is atomic for same-volume
        # renames, which is what we have here.
        tmp.replace(path)
    except Exception:
        # Clean up the partial so the next attempt isn't confused by stale
        # bytes. missing_ok=True for the rare case the partial was never
        # created (json.dumps after partial-open failure path).
        try:
            tmp.unlink(missing_ok=True)
        except OSError as cleanup_err:
            # Don't mask the original failure with a cleanup failure — log it.
            log.warning("Could not remove partial file %s: %s", tmp, cleanup_err)
        raise


__all__ = ["atomic_write_json"]
