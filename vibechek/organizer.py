"""Folder organization — move analyzed tracks into a genre/subgenre tree.

Two modes:
- `organize_from_analysis`: move an analyzed library into a new genre/subgenre tree.
- `route_new_tracks`: copy manually-tagged tracks from a staging folder into the
  matching genre folder of an already-organized library (by reading existing tags).

Source: ports of `legacy/organize_by_genre.py` and `legacy/copy_to_genre_folders.py`.
"""

from __future__ import annotations

import logging
import os
import shutil
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mutagen.aiff import AIFF
from mutagen.flac import FLAC
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4

from vibechek.config import OrganizationConfig
from vibechek.utils import (
    ProgressCallback,
    find_audio_files,
    report_progress,
    sanitize_folder_name,
)

log = logging.getLogger(__name__)


@dataclass
class PlannedMove:
    source: Path
    destination: Path
    genre: str
    subgenre: str
    reason: str  # e.g. "rare genre → Other/", "match by ML genre"
    # Destination expressed relative to `OrganizePlan.base_dir`. The UI uses
    # this for folder grouping in the plan preview + result panel so it never
    # has to do path arithmetic (which was wrong for Windows casing / sep
    # mismatch / paths outside base_dir — see audit #9). Falls back to the
    # absolute destination if `os.path.relpath` raises (e.g. destination on a
    # different Windows drive than base_dir).
    relative_destination: str = ""


@dataclass
class OrganizePlan:
    base_dir: Path
    moves: list[PlannedMove] = field(default_factory=list)
    small_genres: set[str] = field(default_factory=set)
    genre_counts: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


@dataclass
class OrganizeStats:
    planned: int = 0
    moved: int = 0
    errors: list[str] = field(default_factory=list)
    # Path to the undo journal written for this run (None if nothing moved or
    # the journal couldn't be opened). The GUI uses it to offer "Undo".
    journal_path: str | None = None


# Substrings (case-insensitive) that almost always mean the chosen target is
# inside a cloud-sync virtual filesystem. shutil.move into one of these has
# unpredictable behavior (placeholder files, sync conflicts, no atomic rename
# across fs boundaries), so the UI surfaces a warning before letting Execute
# fire. Kept in sync with rpc._RISKY_PATH_SUBSTRINGS in spirit, but lives here
# so organizer.py stays self-contained for tests + library use.
_RISKY_TARGET_SUBSTRINGS: tuple[str, ...] = (
    "my drive",     # Google Drive File Stream
    "googledrive",
    "onedrive",
    "icloud",
    "dropbox",
)


# ---------------------------------------------------------------------------
# Pre-flight validation (cheap, runs before plan_organization)
# ---------------------------------------------------------------------------


def validate_organize_target(
    target_root: str | Path | None,
    *,
    source_library: str | Path | None = None,
) -> dict[str, Any]:
    """Check that `target_root` is safe to write into. Pure read-only inspect
    plus a single tempfile probe.

    Returns a dict with keys:
      - `ok` (bool): True if the path is usable. False blocks the UI.
      - `error` (str | None): user-facing reason `ok` is False.
      - `warnings` (list[str]): non-blocking concerns (cloud-sync paths etc.).
      - `resolved` (str | None): the absolute path we actually probed.

    The UI calls this on blur / before Preview. The sidecar can also call it
    inside `_plan_organization` to fail-fast before any planning work. Cheap
    enough (one mkdir + touch + unlink) to run synchronously on input change.
    """
    result: dict[str, Any] = {
        "ok": True,
        "error": None,
        "warnings": [],
        "resolved": None,
    }

    # An empty / None target_root means "default — same parent as analyzed
    # tracks". That's fine; planning resolves it later.
    if target_root is None or (isinstance(target_root, str) and not target_root.strip()):
        return result

    raw = str(target_root).strip()
    candidate = Path(raw)
    if not candidate.is_absolute():
        result["ok"] = False
        result["error"] = (
            f"Target root must be an absolute path. Got: {raw!r}. "
            "Type the full path (e.g. C:\\Music\\sorted on Windows, "
            "/Users/you/Music/sorted on macOS) or click the folder picker."
        )
        return result

    resolved = candidate.resolve()
    result["resolved"] = str(resolved)

    if source_library is not None:
        try:
            src = Path(source_library).resolve()
        except OSError:
            src = None
        if src is not None and src == resolved:
            result["ok"] = False
            result["error"] = (
                "Target root is the same folder as the source library. "
                "Pick a different folder (or leave the field blank to use "
                "the default)."
            )
            return result

    # Writability probe: ensure we can mkdir + create a file. Don't actually
    # require the folder to pre-exist — many users will type a new path.
    try:
        resolved.mkdir(parents=True, exist_ok=True)
        probe = resolved / ".vibechek_write_probe"
        probe.touch()
        probe.unlink()
    except OSError as e:
        result["ok"] = False
        result["error"] = (
            f"Target root is not writable: {e}. Pick a folder you have "
            "permission to write to."
        )
        return result

    # Warn — don't block — if the path is inside a cloud-sync virtual FS.
    # Some users intentionally organize into Google Drive; we just want them
    # to know what they're signing up for (slow moves, possible conflicts).
    norm_lower = str(resolved).replace("\\", "/").lower()
    for needle in _RISKY_TARGET_SUBSTRINGS:
        if needle in norm_lower:
            result["warnings"].append(
                f"Target root is inside a cloud-sync folder ('{needle}'). "
                "Moves may be slow and may conflict with online sync. "
                "Consider organizing to a local folder first, then syncing."
            )
            break

    return result


def ml_genre_coverage(analysis_data: dict[str, Any]) -> dict[str, int]:
    """Count tracks with usable `ml_genre` data.

    Returned dict: {`total`, `with_ml_genre`}. The UI uses this to warn the
    user before they organize a scan-only library (everything would route to
    Unknown/).
    """
    tracks = analysis_data.get("tracks", [])
    total = len(tracks)
    with_ml_genre = 0
    for track in tracks:
        ml = track.get("ml_analysis") or {}
        if ml.get("ml_genre"):
            with_ml_genre += 1
    return {"total": total, "with_ml_genre": with_ml_genre}


# ---------------------------------------------------------------------------
# Planning (read-only — no filesystem changes)
# ---------------------------------------------------------------------------


def plan_organization(
    analysis_data: dict[str, Any],
    config: OrganizationConfig,
    base_dir: Path | None = None,
) -> OrganizePlan:
    """Compute the file moves `organize_from_analysis` would perform.

    Resolution order for `base_dir`:
      1. Explicit argument (test / GUI override).
      2. `config.target_root`.
      3. Parent of the first track's path in the analysis.
    """
    tracks = analysis_data.get("tracks", [])

    if base_dir is None:
        if config.target_root is not None:
            base_dir = Path(config.target_root)
        elif tracks:
            base_dir = Path(tracks[0]["path"]).parent
        else:
            raise ValueError("No tracks in analysis and no base_dir / target_root set")

    base_dir = Path(base_dir)

    # First pass: count genres so we know which ones are "small"
    genre_counts: dict[str, int] = defaultdict(int)
    for track in tracks:
        ml = track.get("ml_analysis", {})
        genre = sanitize_folder_name(ml.get("ml_genre"))
        genre_counts[genre] += 1

    small_genres = {
        g for g, count in genre_counts.items()
        if count < config.min_genre_size
    }

    plan = OrganizePlan(
        base_dir=base_dir,
        small_genres=small_genres,
        genre_counts=dict(genre_counts),
    )

    # Destinations already assigned in THIS plan. Prevents two source files
    # with the same basename + genre from both planning onto the same dest
    # (neither exists on disk yet at plan time, so the on-disk check alone
    # can't catch it).
    claimed: set[Path] = set()

    for track in tracks:
        source = Path(track["path"])
        if not source.exists():
            plan.errors.append(f"File not found: {source}")
            continue

        ml = track.get("ml_analysis", {})
        genre = sanitize_folder_name(ml.get("ml_genre"))
        subgenre = sanitize_folder_name(ml.get("ml_subgenre"))

        if genre in small_genres:
            dest_folder = base_dir / "Other" / genre
            reason = f"rare genre (<{config.min_genre_size} tracks) → Other/"
        elif config.use_subgenres and subgenre and subgenre != genre:
            dest_folder = base_dir / genre / subgenre
            reason = "ML genre + subgenre"
        else:
            dest_folder = base_dir / genre
            reason = "ML genre"

        dest = dest_folder / source.name
        if source == dest:
            continue  # Already in the right place

        # Uniquify against BOTH on-disk state and destinations already claimed
        # by earlier moves in this batch (intra-batch basename collisions).
        if dest in claimed or (dest.exists() and source != dest):
            dest = _unique_destination(dest, claimed)
        claimed.add(dest)

        # Compute the relative destination once, here, so the UI never has to.
        # On Windows, destinations on a different drive than base_dir raise
        # ValueError from os.path.relpath — fall back to the absolute path.
        try:
            rel_dest = os.path.relpath(str(dest), str(base_dir))
        except ValueError:
            rel_dest = str(dest)

        plan.moves.append(PlannedMove(
            source=source,
            destination=dest,
            genre=genre,
            subgenre=subgenre,
            reason=reason,
            relative_destination=rel_dest,
        ))

    return plan


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


def organize_from_analysis(
    analysis_data: dict[str, Any],
    config: OrganizationConfig,
    on_progress: ProgressCallback | None = None,
    dry_run: bool = False,
    base_dir: Path | None = None,
) -> OrganizeStats:
    """Plan and execute the genre-folder reorganization."""
    plan = plan_organization(analysis_data, config, base_dir=base_dir)
    stats = OrganizeStats(planned=len(plan.moves))

    if dry_run:
        return stats

    # Import here so we don't make cancellation a hard dep of organizer when
    # someone uses it as a library outside the sidecar.
    from vibechek import cancellation
    from vibechek import journal as _journal

    # Append-only undo journal: records each completed move BEFORE the next so
    # a partial run (disk full, crash) is recoverable and a finished organize
    # can be reverted. A failure to open the journal degrades to a no-op
    # writer — the organize still runs, just without undo.
    jrnl = _journal.start_journal(_journal.KIND_ORGANIZE, root=plan.base_dir)
    try:
        for i, move in enumerate(plan.moves):
            # Check before each move — user clicking Cancel mid-organize must
            # actually stop, not just stop SHOWING progress. Audit found that
            # without this, ~12k file moves would continue after cancel.
            cancellation.check()
            report_progress(on_progress, i + 1, len(plan.moves), move.source.name)
            try:
                if not move.source.exists():
                    # Vanished between plan and execute (user deleted it, an
                    # earlier move with a colliding source name already took it).
                    stats.errors.append(f"{move.source.name}: source no longer exists")
                    continue
                dest = move.destination
                # Re-check at execute time: the plan uniquified against a disk
                # snapshot, but time has passed (and earlier moves in this batch
                # created folders). shutil.move OVERWRITES an existing destination
                # file — a silent data-loss bug. Re-uniquify so we never clobber.
                if dest.exists():
                    dest = _unique_destination(dest)
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(move.source), str(dest))
                # Record AFTER the move succeeds, BEFORE the next iteration.
                jrnl.record_move(move.source, dest)
                stats.moved += 1
            except OSError as e:
                stats.errors.append(f"{move.source.name}: {e}")
    finally:
        jrnl.close()

    # Expose the journal path so the GUI can offer "Undo this organize".
    stats.journal_path = str(jrnl.path) if jrnl.entries > 0 else None

    return stats


def route_new_tracks(
    staging_dir: Path,
    library_root: Path,
    on_progress: ProgressCallback | None = None,
    dry_run: bool = False,
) -> dict[str, int]:
    """Copy each tagged track from `staging_dir` into a `library_root/<Genre>/` folder.

    Genre is read from the file's existing tag. Tracks without a genre tag are
    skipped (with a log entry). File-name collisions are silently skipped to avoid
    overwriting whatever is already there.
    """
    staging_dir = Path(staging_dir)
    library_root = Path(library_root)
    files = find_audio_files(staging_dir)
    summary = {"copied": 0, "skipped_no_genre": 0, "skipped_exists": 0, "errors": 0}

    from vibechek import cancellation

    for i, fp in enumerate(files):
        # Honor Cancel mid-copy (the trash/move loops in duplicates do too).
        cancellation.check()
        report_progress(on_progress, i + 1, len(files), fp.name)

        genre = _read_genre_tag(fp)
        if not genre:
            summary["skipped_no_genre"] += 1
            log.info("No genre tag, skipping: %s", fp.name)
            continue

        dest_folder = library_root / sanitize_folder_name(genre)
        dest_file = dest_folder / fp.name
        if dest_file.exists():
            summary["skipped_exists"] += 1
            continue

        if dry_run:
            summary["copied"] += 1
            continue

        try:
            dest_folder.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(fp), str(dest_file))
            summary["copied"] += 1
        except OSError as e:
            log.warning("Copy failed for %s: %s", fp, e)
            summary["errors"] += 1

    return summary


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read_genre_tag(filepath: Path) -> str | None:
    """Return the main genre tag from a file, or None if missing/unreadable."""
    ext = filepath.suffix.lower()
    try:
        if ext == ".mp3":
            audio = MP3(filepath)
            if audio.tags and "TCON" in audio.tags:
                return str(audio.tags["TCON"])
        elif ext == ".flac":
            audio = FLAC(filepath)
            if audio.tags and "genre" in audio.tags:
                return audio.tags["genre"][0]
        elif ext == ".m4a":
            audio = MP4(filepath)
            if audio.tags and "\xa9gen" in audio.tags:
                return audio.tags["\xa9gen"][0]
        elif ext in (".aiff", ".aif"):
            audio = AIFF(filepath)
            if audio.tags and "TCON" in audio.tags:
                return str(audio.tags["TCON"])
    except Exception as e:  # noqa: BLE001
        log.debug("Could not read genre from %s: %s", filepath, e)
    return None


def _unique_destination(path: Path, claimed: set[Path] | None = None) -> Path:
    """Append _1, _2, ... until `path` is free.

    "Free" means it neither exists on disk NOR has already been claimed by an
    earlier planned move in this same batch. The `claimed` set closes the
    intra-batch collision hole: two source files with the same basename
    routing to the same genre folder are BOTH absent from disk at plan time
    (they're still at their source locations), so without `claimed` they'd
    both plan to the identical destination and the second `shutil.move` would
    silently overwrite the first — data loss.
    """
    if path not in (claimed or ()) and not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    counter = 1
    while True:
        candidate = path.with_name(f"{stem}_{counter}{suffix}")
        if candidate not in (claimed or ()) and not candidate.exists():
            return candidate
        counter += 1


__all__ = [
    "PlannedMove",
    "OrganizePlan",
    "OrganizeStats",
    "plan_organization",
    "organize_from_analysis",
    "route_new_tracks",
    "validate_organize_target",
    "ml_genre_coverage",
]
