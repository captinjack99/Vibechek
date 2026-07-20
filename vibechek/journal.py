"""Operation journal — crash-safe, append-only record of destructive file ops.

`organize` (genre-folder moves) and `dedupe` (move-to-review / trash) both
relocate or remove the user's audio files in bulk. Two failure modes made
those scary:

  1. A partial run (disk full at file 500/1000, a kill, a power loss) left the
     library half-reorganized with NO record of what already moved — the user
     couldn't tell which files were where, let alone put them back.
  2. Even a *successful* organize was effectively irreversible: there was no
     "undo" once the user saw they didn't like the layout.

This module fixes both with an append-only JSONL journal. Each completed
operation is written as its own line and flushed BEFORE the next op runs, so:

  - A crash leaves a valid journal of everything done up to that point.
  - `revert_journal()` reads the journal and reverses the moves (newest first,
    so nested-directory moves unwind cleanly).

Layout:
    <data_dir>/journals/<timestamp>-<kind>.jsonl

File format (one JSON object per line):
    line 1  (header): {"v": 1, "kind": "...", "started_at": ..., "root": "..."}
    line N+ (entry):  {"action": "move", "src": "...", "dst": "..."}
                      {"action": "trash", "src": "..."}

`trash` entries are recorded for transparency but are NOT auto-revertible:
send2trash moves to the OS recycle bin, which has no reliable cross-platform
restore API. `revert_journal` reports them so the user can restore manually.

Why JSONL + append (not `atomic_write_json` of the whole journal):
    Rewriting the entire journal after every entry is O(n^2) on a 12k-move
    organize. An append-only log is O(n) total, naturally crash-safe (a kill
    truncates at most the last partial line, which the reader skips), and the
    canonical shape for this kind of forward-recovery log.
"""

from __future__ import annotations

import json
import logging
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from vibechek.config import DATA_DIR

log = logging.getLogger(__name__)

JOURNALS_DIR = DATA_DIR / "journals"
JOURNAL_VERSION = 1

# Journal kinds — kept as plain strings (not an enum) so they serialize
# trivially and the GUI can switch on them without importing this module.
KIND_ORGANIZE = "organize"
KIND_DEDUPE_MOVE = "dedupe_move"
KIND_DEDUPE_TRASH = "dedupe_trash"

ProgressCallback = Callable[[int, int, str], None]


@dataclass
class JournalWriter:
    """Append-only writer for one destructive operation.

    Open via `start_journal()`, call `record_move()` / `record_trash()` after
    each completed filesystem op, then `close()` in a finally block. Each
    record is flushed immediately so a crash can't lose already-done work.
    """

    path: Path
    kind: str
    _fh: Any = field(default=None, repr=False)
    entries: int = 0

    def record_move(self, src: Path | str, dst: Path | str) -> None:
        self._write({"action": "move", "src": str(src), "dst": str(dst)})

    def record_trash(self, src: Path | str) -> None:
        self._write({"action": "trash", "src": str(src)})

    def _write(self, obj: dict[str, Any]) -> None:
        if self._fh is None:
            return
        try:
            self._fh.write(json.dumps(obj, ensure_ascii=False) + "\n")
            # flush (not fsync) per entry: survives a process kill / crash,
            # which is the common case. The shutil.move that precedes each
            # record is far slower than this write, so the overhead is noise.
            self._fh.flush()
            self.entries += 1
        except OSError as e:
            # A journal-write failure must NOT abort the operation it's
            # recording — the move already happened. Log and carry on; worst
            # case that one entry is missing from the undo log.
            log.warning("Journal write failed (continuing): %s", e)

    def close(self) -> None:
        if self._fh is not None:
            try:
                self._fh.write(
                    json.dumps({"action": "completed", "at": time.time()}) + "\n"
                )
                self._fh.flush()
            except OSError:
                pass
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None

    # Context-manager sugar so callers can `with start_journal(...) as j:`.
    def __enter__(self) -> JournalWriter:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def start_journal(kind: str, root: Path | str | None = None) -> JournalWriter:
    """Open a new journal for a destructive operation.

    On any failure to create the file (read-only data dir, etc.) returns a
    writer with `_fh=None` — a no-op journal. The operation it records still
    runs; it just won't be revertible. We never let journaling block the
    actual work.
    """
    try:
        JOURNALS_DIR.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%d-%H%M%S")
        path = JOURNALS_DIR / f"{ts}-{kind}.jsonl"
        # Avoid clobbering a journal written in the same second.
        n = 1
        while path.exists():
            path = JOURNALS_DIR / f"{ts}-{kind}-{n}.jsonl"
            n += 1
        fh = open(path, "w", encoding="utf-8", newline="\n")
        fh.write(json.dumps({
            "v": JOURNAL_VERSION,
            "kind": kind,
            "started_at": time.time(),
            "root": str(root) if root is not None else None,
        }) + "\n")
        fh.flush()
        return JournalWriter(path=path, kind=kind, _fh=fh)
    except OSError as e:
        log.warning("Could not open journal for %s (proceeding without undo): %s", kind, e)
        return JournalWriter(path=Path(), kind=kind, _fh=None)


def _read_journal(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Parse a journal file into (header, entries). Skips malformed/partial
    lines (e.g. a final truncated line from a crash) rather than raising."""
    header: dict[str, Any] = {}
    entries: list[dict[str, Any]] = []
    with open(path, encoding="utf-8") as f:
        for i, raw in enumerate(f):
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                # Truncated last line from a crash mid-write — safe to skip.
                log.debug("Skipping malformed journal line %d in %s", i, path)
                continue
            if not isinstance(obj, dict):
                # Valid JSON that isn't an object (a stray array/scalar from a
                # partial or hand-mangled write). `"kind" in obj` / `obj.get(...)`
                # below assume a dict — skip rather than raise, matching the
                # documented "skip malformed lines" contract.
                log.debug("Skipping non-object journal line %d in %s", i, path)
                continue
            if i == 0 and "kind" in obj:
                header = obj
            elif obj.get("action") in ("move", "trash"):
                entries.append(obj)
    return header, entries


def list_journals(limit: int = 50) -> list[dict[str, Any]]:
    """Return recent journals (most recent first) for the GUI's undo list.

    Each item: {path, kind, started_at, root, move_count, trash_count}.
    Cheap — reads each journal once but only tallies counts.
    """
    if not JOURNALS_DIR.exists():
        return []
    out: list[dict[str, Any]] = []
    files = sorted(JOURNALS_DIR.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in files[:limit]:
        try:
            header, entries = _read_journal(path)
        except OSError:
            continue
        moves = sum(1 for e in entries if e.get("action") == "move")
        trashes = sum(1 for e in entries if e.get("action") == "trash")
        out.append({
            "path": str(path),
            "kind": header.get("kind", "unknown"),
            "started_at": header.get("started_at"),
            "root": header.get("root"),
            "move_count": moves,
            "trash_count": trashes,
        })
    return out


def revert_journal(
    journal_path: Path | str,
    on_progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    """Reverse the moves recorded in a journal (newest entry first).

    For each `move` entry we move `dst` back to `src`:
      - skip if `dst` no longer exists (already moved/deleted since)
      - skip if `src` already exists (don't clobber whatever's there now)
      - recreate `src`'s parent directory as needed

    `trash` entries can't be auto-restored (send2trash → OS recycle bin with no
    reliable restore API); they're counted and returned so the GUI can tell the
    user to restore them manually from the recycle bin.

    Returns a summary: {reverted, skipped, errors, trashed_not_reverted,
    error_messages, reverted_pairs}. `reverted_pairs` is [(current, restored)]
    — i.e. (dst, src) — for every move actually undone, so the GUI can rewrite
    its in-memory track paths back (mirroring organize's moved_pairs; without
    it every post-undo Preview / tag-apply targeted paths that no longer
    exist). Honors cancellation between entries.
    """
    from vibechek import cancellation

    path = Path(journal_path)
    if not path.exists():
        raise FileNotFoundError(f"Journal not found: {path}")

    _header, entries = _read_journal(path)
    # Reject a file that isn't a Vibechek operation journal at all (wrong file,
    # corrupt JSON, mangled header). Every real journal — even an empty or
    # fully-reverted one — carries a valid header written by start_journal, so
    # this only false-rejects genuine non-journals, not legitimately-empty runs.
    if _header.get("kind") not in (KIND_ORGANIZE, KIND_DEDUPE_MOVE, KIND_DEDUPE_TRASH):
        # Plain, CLI-free headline (voice-guide rule 4): the GUI shows this
        # verbatim and has no terminal. The path is DEMOTED to the log.
        log.warning("Not a Vibechek operation journal: %s", path)
        raise ValueError(
            "This file isn't a Vibechek undo record, so it can't be reverted."
        )
    moves = [e for e in entries if e.get("action") == "move"]
    trashed = [e for e in entries if e.get("action") == "trash"]

    error_messages: list[str] = []
    reverted_pairs: list[tuple[str, str]] = []
    summary: dict[str, Any] = {
        "reverted": 0,
        "skipped": 0,
        "errors": 0,
        "trashed_not_reverted": len(trashed),
        "error_messages": error_messages,
        "reverted_pairs": reverted_pairs,
    }

    # Reverse order: if organize created nested folders (Genre/Subgenre/x.mp3),
    # unwinding newest-first keeps each move's destination parent valid.
    total = len(moves)
    for i, entry in enumerate(reversed(moves)):
        cancellation.check()
        src = Path(entry["src"])   # original location (where we move back TO)
        dst = Path(entry["dst"])   # current location (where the file IS now)
        if on_progress:
            on_progress(i + 1, total, dst.name)

        if not dst.exists():
            summary["skipped"] += 1
            continue
        if src.exists():
            # Something is already at the original path — don't overwrite it.
            summary["skipped"] += 1
            error_messages.append(f"{src}: original path occupied, left {dst} in place")
            continue
        try:
            src.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(dst), str(src))
            summary["reverted"] += 1
            reverted_pairs.append((str(dst), str(src)))
        except OSError as e:
            summary["errors"] += 1
            error_messages.append(f"{dst} -> {src}: {e}")

    return summary


__all__ = [
    "JournalWriter",
    "start_journal",
    "list_journals",
    "revert_journal",
    "KIND_ORGANIZE",
    "KIND_DEDUPE_MOVE",
    "KIND_DEDUPE_TRASH",
    "JOURNALS_DIR",
]
