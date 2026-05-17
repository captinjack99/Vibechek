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
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from vibechek.config import CONFIG_DIR, DATA_DIR

log = logging.getLogger(__name__)

MAX_RECENT = 10
STATE_FILE = CONFIG_DIR / "library_state.json"
ANALYSES_DIR = DATA_DIR / "analyses"


@dataclass
class LibraryRecord:
    """One row in the recent-libraries list."""

    path: str                # The library folder
    analysis_path: str       # Where the saved analysis.json lives
    track_count: int = 0     # Total tracks scanned
    analyzed_count: int = 0  # Tracks that have ml_analysis
    last_opened: float = 0.0      # epoch seconds
    last_analyzed: float = 0.0    # epoch seconds; 0 if never analyzed


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
        recent = [LibraryRecord(**r) for r in raw.get("recent", [])]
        return LibraryState(recent=recent)
    except (OSError, json.JSONDecodeError, TypeError) as e:
        log.warning("Could not load library state: %s — starting fresh", e)
        return LibraryState()


def save_state(state: LibraryState) -> None:
    """Write the index to disk. Creates the config dir as needed."""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {"recent": [asdict(r) for r in state.recent]}
    STATE_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Update operations — call these instead of mutating state directly
# ---------------------------------------------------------------------------


def record_open(library_path: Path | str) -> LibraryRecord:
    """Note that the user opened this library. Bumps it to the top of recent."""
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
    analysis_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

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
    """Drop a library from the recent list. Returns True if it was there."""
    state = load_state()
    path_str = str(library_path)
    before = len(state.recent)
    state.recent = [r for r in state.recent if r.path != path_str]
    save_state(state)
    return len(state.recent) < before


def load_analysis(record: LibraryRecord) -> dict[str, Any] | None:
    """Read the analysis JSON for a library record, or None if missing."""
    p = Path(record.analysis_path)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        log.warning("Could not load analysis at %s: %s", p, e)
        return None


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
    "LibraryRecord",
    "LibraryState",
    "load_state",
    "save_state",
    "record_open",
    "record_analysis",
    "forget",
    "load_analysis",
]
