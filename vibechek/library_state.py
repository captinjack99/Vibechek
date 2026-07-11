"""Per-user persistence of the last library Vibechek opened/analyzed.

Solves the biggest UX hole: if you analyze 12k tracks and close the app,
your work shouldn't vanish. After every analyze the sidecar writes the
report to a stable location and updates a small JSON index. On next launch
the GUI offers to reload the most recent.

State layout:
    <config_dir>/library_state.json   ← index of recent libraries
    <data_dir>/analyses/<hash>.json   ← one analysis JSON per library

The index keeps at most MAX_RECENT entries, sorted most-recent first.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

from vibechek.config import CONFIG_DIR, DATA_DIR
from vibechek.io import atomic_write_json

log = logging.getLogger(__name__)

MAX_RECENT = 10
STATE_FILE = CONFIG_DIR / "library_state.json"
ANALYSES_DIR = DATA_DIR / "analyses"


class AnalysisUnreadable(Exception):
    """The saved analysis file EXISTS but could not be read/parsed.

    Deliberately distinct from load_analysis() returning None (the file is
    genuinely absent). The difference is load-bearing for incremental analyze:
    if a transient read failure (an antivirus / OneDrive-Google-Drive lock, a
    crash-truncated 32 MB JSON, a hand-edit) is treated as "never analyzed",
    _reattach_skipped_records re-attaches nothing and record_analysis() then
    overwrites the good-but-unreadable file with ONLY the newly scanned tracks
    — a silent, whole-library data loss. Callers that would otherwise conflate
    "absent" with "unreadable" must abort loudly instead of proceeding empty.
    """

    def __init__(self, path: Path | str, cause: Exception | str) -> None:
        self.path = str(path)
        self.cause = cause
        super().__init__(f"Could not read the saved analysis at {self.path}: {cause}")

# Serializes every load-modify-save of the index. The RPC dispatch pool runs
# several mutators (record_open, record_analysis, forget, rename_library,
# tag_library) concurrently, each doing an unsynchronized read-then-write — a
# classic lost-update race (two writers both load the old index, each adds its
# own change, and whichever saves last wins, silently dropping the other's
# mutation). atomic_write_json already prevents a *torn* file via its unique
# temp suffix; this lock prevents the lost *update*. It's re-entrant so a
# mutator can call save_state() (which would otherwise want the lock too)
# without deadlocking.
_STATE_LOCK = threading.RLock()


@dataclass
class LibraryRecord:
    """One row in the recent-libraries list.

    Has a friendly `name` (defaults to the folder's basename via
    `display_name()`) and a list of user-assigned `tags` ("Friday Set",
    "Brunch", "Wedding") so DJs can organise multiple gigging libraries.
    Both are pure metadata — the rest of the engine still keys off `path`.
    Optional fields are last so older state files that lack them deserialize
    fine via the constructor defaults.
    """

    path: str                # The library folder
    analysis_path: str       # Where the saved analysis.json lives
    track_count: int = 0     # Total tracks scanned
    analyzed_count: int = 0  # Tracks that have ml_analysis
    last_opened: float = 0.0      # epoch seconds
    last_analyzed: float = 0.0    # epoch seconds; 0 if never analyzed
    name: str = ""                # Friendly display name; "" → use basename(path)
    tags: list[str] = field(default_factory=list)  # User-assigned: "Brunch", "Wedding"

    def display_name(self) -> str:
        """Friendly label for the UI. Falls back to the folder basename."""
        if self.name:
            return self.name
        return Path(self.path).name or self.path


@dataclass
class LibraryState:
    """The whole index."""

    recent: list[LibraryRecord] = field(default_factory=list)

    def most_recent(self) -> LibraryRecord | None:
        return self.recent[0] if self.recent else None


# ---------------------------------------------------------------------------
# Load / save the index itself
# ---------------------------------------------------------------------------


def load_state() -> LibraryState:
    """Read the index from disk. Returns empty state on any error."""
    if not STATE_FILE.exists():
        return LibraryState()
    try:
        raw = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        log.warning("Could not load library state: %s — starting fresh", e)
        return LibraryState()

    # Parse each record defensively, mirroring config._subset: a single bad
    # row (an unexpected forward-compat key from a newer build, a missing
    # required field from a partial write/hand-edit) must NOT nuke the whole
    # list. Without this, one bad row strands every other valid analysis JSON
    # on disk — the user sees "no recent libraries" though the 32MB reports
    # are intact (see save_state's note). Skip-and-log instead.
    raw_recent = raw.get("recent", []) if isinstance(raw, dict) else []
    if not isinstance(raw_recent, list):
        raw_recent = []
    valid = {f.name for f in fields(LibraryRecord)}
    recent: list[LibraryRecord] = []
    for r in raw_recent:
        if not isinstance(r, dict):
            log.warning("Skipping non-object library record %r", r)
            continue
        try:
            rec = LibraryRecord(**{k: v for k, v in r.items() if k in valid})
        except TypeError as e:
            log.warning("Skipping bad library record %r: %s", r, e)
            continue
        # `tags` is metadata the UI iterates; a hand-edited/old state file may
        # store it as a bare string ("Brunch") which would mis-iterate as
        # characters downstream. Coerce to a clean list[str].
        if isinstance(rec.tags, str):
            rec.tags = [rec.tags] if rec.tags else []
        elif not isinstance(rec.tags, list):
            rec.tags = []
        recent.append(rec)
    return LibraryState(recent=recent)


def save_state(state: LibraryState) -> None:
    """Write the index to disk. Creates the config dir as needed."""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {"recent": [asdict(r) for r in state.recent]}
    # Atomic write: this index points at the saved analysis files, so a
    # corrupted index strands every prior analysis (the user sees "no recent
    # libraries" even though the 32MB analysis JSONs are still on disk).
    atomic_write_json(STATE_FILE, payload, indent=2)


# ---------------------------------------------------------------------------
# Update operations — call these instead of mutating state directly
# ---------------------------------------------------------------------------


def record_open(library_path: Path | str) -> LibraryRecord:
    """Note that the user opened this library. Bumps it to the top of recent."""
    with _STATE_LOCK:
        state = load_state()
        path_str = str(library_path)
        existing = _find(state, path_str)
        if existing:
            existing.last_opened = time.time()
            _bump_to_front(state, existing)
        else:
            existing = LibraryRecord(
                path=path_str,
                analysis_path=str(_analysis_path_for(path_str)),
                last_opened=time.time(),
            )
            state.recent.insert(0, existing)
        _truncate(state)
        save_state(state)
        return existing


def record_analysis(library_path: Path | str, report: dict[str, Any]) -> LibraryRecord:
    """Persist an analysis report and update the index."""
    path_str = str(library_path)
    analysis_path = _analysis_path_for(path_str)
    analysis_path.parent.mkdir(parents=True, exist_ok=True)
    # Atomic write: this is the THE file we most cannot afford to corrupt —
    # it represents 30+ minutes of GPU time on a 12k-track library, and a
    # truncated 32 MB JSON makes the entire library "lost" on next launch.
    atomic_write_json(analysis_path, report, indent=2, ensure_ascii=False)

    # Only the index load-modify-save needs serializing; the (large) analysis
    # JSON above is keyed by a unique per-library path so it never races.
    with _STATE_LOCK:
        state = load_state()
        existing = _find(state, path_str)
        if not existing:
            existing = LibraryRecord(path=path_str, analysis_path=str(analysis_path))
            state.recent.insert(0, existing)
        else:
            _bump_to_front(state, existing)

        summary = report.get("summary", {}) or {}
        existing.analysis_path = str(analysis_path)
        existing.track_count = int(summary.get("total_files", 0))
        existing.analyzed_count = int(summary.get("analyzed", 0))
        existing.last_opened = time.time()
        existing.last_analyzed = time.time()

        _truncate(state)
        save_state(state)
        return existing


def forget(library_path: Path | str) -> bool:
    """Drop a library from the recent list. Returns True if it was there.

    Also deletes the saved analysis JSON and its tag-priors sidecar: the
    analysis path is a pure hash of the library path, so a later re-open of
    the same folder would otherwise silently resurrect a months-old analysis
    AND a Rekordbox-priors import the user believed was discarded (the
    sidecar is re-merged into every future analyze).
    """
    with _STATE_LOCK:
        state = load_state()
        path_str = str(library_path)
        before = len(state.recent)
        removed = [r for r in state.recent if r.path == path_str]
        state.recent = [r for r in state.recent if r.path != path_str]
        save_state(state)
        for rec in removed:
            try:
                from vibechek.tag_priors import priors_path_for  # noqa: PLC0415

                Path(rec.analysis_path).unlink(missing_ok=True)
                priors_path_for(rec.analysis_path).unlink(missing_ok=True)
            except OSError as e:
                log.warning("Could not remove a forgotten library's files: %s", e)
        return len(state.recent) < before


def rename_library(library_path: Path | str, new_name: str) -> LibraryRecord | None:
    """Set the friendly display name for a library.

    An empty `new_name` ("" or whitespace-only) clears the override so the
    UI falls back to the folder basename. Returns the updated record, or
    `None` if the library isn't in the recent list (don't promote an
    unknown library just because the user renamed it).
    """
    with _STATE_LOCK:
        state = load_state()
        path_str = str(library_path)
        record = _find(state, path_str)
        if record is None:
            return None
        # Strip whitespace — leading/trailing spaces in a UI text field are
        # almost always typos, never intentional. Empty string is the sentinel
        # for "use the basename" (see LibraryRecord.display_name).
        record.name = (new_name or "").strip()
        save_state(state)
        return record


def tag_library(library_path: Path | str, tags: list[str]) -> LibraryRecord | None:
    """Replace the tag list for a library.

    Tags are deduplicated (preserving first-occurrence order so the UI
    doesn't reshuffle the user's order), stripped, and empty entries are
    dropped. Returns the updated record, or `None` if the library isn't
    in the recent list.
    """
    with _STATE_LOCK:
        state = load_state()
        path_str = str(library_path)
        record = _find(state, path_str)
        if record is None:
            return None
        cleaned: list[str] = []
        seen: set[str] = set()
        for t in tags or []:
            s = str(t).strip()
            if not s:
                continue
            # Case-insensitive dedupe — "Friday Set" and "friday set" are the
            # same gig in the user's head; collapse them.
            key = s.casefold()
            if key in seen:
                continue
            seen.add(key)
            cleaned.append(s)
        record.tags = cleaned
        save_state(state)
        return record


def load_analysis(record: LibraryRecord) -> dict[str, Any] | None:
    """Read the analysis JSON for a library record.

    Returns None only when the file is genuinely ABSENT (never analyzed, or the
    saved report was removed). Raises `AnalysisUnreadable` when the file EXISTS
    but can't be read/parsed — see that exception for why the two cases must not
    be conflated (a transient lock treated as "never analyzed" silently
    destroys the whole library on the next incremental analyze). Read-only
    callers that only need "is there a usable report" can catch
    AnalysisUnreadable and fall back to their missing-file branch.
    """
    p = Path(record.analysis_path)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        log.warning("Could not load analysis at %s: %s", p, e)
        raise AnalysisUnreadable(p, e) from e


def save_analysis(record: LibraryRecord, report: dict[str, Any]) -> None:
    """Re-persist an already-recorded analysis in place (atomically).

    For mutations of an existing analysis — e.g. the user approving/reverting
    genre conflicts from the review queue — that must survive a reload. Unlike
    record_analysis it does NOT touch the recents index (no count/last-analyzed
    changes); it only rewrites the analysis JSON the load path reads back.
    """
    atomic_write_json(Path(record.analysis_path), report, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _analysis_path_for(library_path: str) -> Path:
    """Stable filename for a given library path. Uses a short hash to keep it
    portable and to avoid filesystem-illegal characters in the original path.
    """
    digest = hashlib.sha1(library_path.encode("utf-8")).hexdigest()[:12]
    safe_name = "".join(c if c.isalnum() else "_" for c in Path(library_path).name)[:48]
    return ANALYSES_DIR / f"{safe_name}-{digest}.json"


def _find(state: LibraryState, path: str) -> LibraryRecord | None:
    for r in state.recent:
        if r.path == path:
            return r
    return None


def _bump_to_front(state: LibraryState, record: LibraryRecord) -> None:
    if state.recent and state.recent[0] is record:
        return
    state.recent = [r for r in state.recent if r is not record]
    state.recent.insert(0, record)


def _truncate(state: LibraryState) -> None:
    if len(state.recent) > MAX_RECENT:
        state.recent = state.recent[:MAX_RECENT]


__all__ = [
    "AnalysisUnreadable",
    "LibraryRecord",
    "LibraryState",
    "load_state",
    "save_state",
    "record_open",
    "record_analysis",
    "forget",
    "load_analysis",
    "save_analysis",
    "rename_library",
    "tag_library",
]
