"""History of tag backups the user has made through Vibechek.

Lets the Tags view show "you have N past backups, here they are" — so users
don't have to remember filenames. We just store metadata about backups
(path, size, created time, what was backed up); the backup files themselves
live wherever the user saved them.

Why a separate index file rather than scanning a folder:
  - The user picks the backup destination via the save dialog. They might
    save to a thumb drive, a network share, a temp folder. We can't know.
  - The index lives in <config_dir>/backup_history.json and just records
    pointers. Missing files (e.g. moved by the user) are pruned on read.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from vibechek.config import CONFIG_DIR

log = logging.getLogger(__name__)

MAX_RECORDS = 50  # Plenty of history without bloating the index
HISTORY_FILE = CONFIG_DIR / "backup_history.json"


@dataclass
class BackupRecord:
    backup_path: str       # Where the JSON lives
    library_path: str      # The folder that was backed up
    file_count: int        # How many tracks were backed up
    created_at: float      # epoch seconds
    size_bytes: int = 0    # Size of the backup JSON itself
    # When True, the backup file no longer exists on disk (pruned at read time)
    missing: bool = False


@dataclass
class BackupHistory:
    records: list[BackupRecord] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def load() -> BackupHistory:
    """Read the index. Prunes records whose files have gone missing."""
    if not HISTORY_FILE.exists():
        return BackupHistory()

    try:
        raw = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        log.warning("Could not load backup history: %s", e)
        return BackupHistory()

    records = []
    for r in raw.get("records", []):
        try:
            rec = BackupRecord(**r)
        except TypeError as e:
            log.debug("Skipping unparseable backup record: %s", e)
            continue
        # Mark missing files so the UI can grey them out
        rec.missing = not Path(rec.backup_path).exists()
        records.append(rec)
    return BackupHistory(records=records)


def save(history: BackupHistory) -> None:
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {"records": [asdict(r) for r in history.records]}
    HISTORY_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Mutators
# ---------------------------------------------------------------------------


def record(library_path: str | Path, backup_path: str | Path, file_count: int) -> BackupRecord:
    """Remember a backup that just got created. Bumps to the top."""
    bp = str(backup_path)
    lp = str(library_path)
    history = load()
    # If we already have this exact backup path, replace it
    history.records = [r for r in history.records if r.backup_path != bp]
    size = 0
    try:
        size = Path(bp).stat().st_size
    except OSError:
        pass
    rec = BackupRecord(
        backup_path=bp,
        library_path=lp,
        file_count=file_count,
        created_at=time.time(),
        size_bytes=size,
    )
    history.records.insert(0, rec)
    if len(history.records) > MAX_RECORDS:
        history.records = history.records[:MAX_RECORDS]
    save(history)
    return rec


def forget(backup_path: str | Path) -> bool:
    """Drop a record from the index. The backup file itself is NOT deleted."""
    bp = str(backup_path)
    history = load()
    before = len(history.records)
    history.records = [r for r in history.records if r.backup_path != bp]
    if len(history.records) < before:
        save(history)
        return True
    return False


def to_dict(history: BackupHistory) -> dict[str, Any]:
    return {"records": [asdict(r) for r in history.records]}


__all__ = [
    "BackupRecord",
    "BackupHistory",
    "load",
    "save",
    "record",
    "forget",
    "to_dict",
    "HISTORY_FILE",
    "MAX_RECORDS",
]
