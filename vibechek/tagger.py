"""Tag reading, backup, and writing.

Three responsibilities:

1. **Snapshot** every ID3/Vorbis frame on every file before any write — including
   Rekordbox-specific GEOB and PRIV frames (cue points, beat grids, memory cues).
   Binary frames are base64-encoded for JSON safety.
2. **Restore** the snapshot exactly, frame-for-frame, with no loss.
3. **Apply** ML analysis results back to files with confidence filtering,
   while preserving any Rekordbox binary frames that already exist.

ID3 text-frame encoding for writes is controlled by
`TaggingConfig.id3_text_encoding` (0 = ISO-8859-1, 1 = UTF-16, 3 = UTF-8).
Modern players read UTF-8 fine, but Rekordbox 5 and some older DJ software
only recognize encoding 0 or 1 — users on old Rekordbox can switch to UTF-16
so genre/subgenre changes show up in the app.

Source: ports of `legacy/backup_tags.py` and `legacy/apply_tags_filtered.py`.
"""

from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mutagen.flac import FLAC
from mutagen.id3 import (
    GEOB,
    TALB,
    TBPM,
    TCON,
    TIT1,
    TIT2,
    TKEY,
    TPE1,
    TXXX,
    PRIV,
)
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4

from vibechek.config import TaggingConfig
from vibechek.io import atomic_write_json
from vibechek.utils import (
    SUPPORTED_EXTENSIONS,
    ProgressCallback,
    find_audio_files,
    report_progress,
)

log = logging.getLogger(__name__)

BACKUP_VERSION = "1.0"

# Custom (non-standard) tag names written/read on every format.
CUSTOM_TAGS = ("energy", "mood", "timeslot", "direction", "vocal")


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


@dataclass
class BackupStats:
    total: int = 0
    backed_up: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass
class RestoreStats:
    total: int = 0
    restored: int = 0
    skipped_missing: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass
class ApplyStats:
    total: int = 0
    genre_applied: int = 0
    # Bumped when the strict subgenre threshold fails but the parent-genre
    # fallback hits (`parent_genre_confidence_threshold`). The track gets
    # tagged with the parent genre and no subgenre; we count it separately
    # so the UI can show "X confident, Y parent-only, Z unconfident" instead
    # of conflating fallback-tagged with strictly-tagged.
    genre_applied_parent_only: int = 0
    genre_skipped_low_confidence: int = 0
    other_tags_applied: int = 0
    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Read tags — extract a JSON-safe snapshot from any supported file
# ---------------------------------------------------------------------------


def read_all_tags(filepath: Path) -> dict[str, Any]:
    """Extract every relevant tag from `filepath` as a JSON-safe dict.

    For MP3, this includes Rekordbox-specific GEOB and PRIV binary frames,
    base64-encoded for serialization safety. For FLAC/M4A, only the standard
    text frames are captured (Rekordbox stores its data in MP3 GEOB anyway).
    """
    ext = filepath.suffix.lower()
    tags: dict[str, Any] = {}
    try:
        if ext == ".mp3":
            tags = _read_mp3_tags(filepath)
        elif ext == ".flac":
            tags = _read_flac_tags(filepath)
        elif ext == ".m4a":
            tags = _read_m4a_tags(filepath)
        else:
            tags["_unsupported"] = True
    except Exception as e:  # noqa: BLE001
        tags["_error"] = str(e)
    return {k: v for k, v in tags.items() if v is not None}


def _read_mp3_tags(filepath: Path) -> dict[str, Any]:
    audio = MP3(filepath)
    out: dict[str, Any] = {}
    if not audio.tags:
        return out

    text_frame_map = {
        "TIT2": "title", "TPE1": "artist", "TALB": "album", "TCON": "genre",
        "TBPM": "bpm", "TKEY": "key", "TIT1": "subgenre",
    }
    for frame_id, field_name in text_frame_map.items():
        if frame_id in audio.tags:
            out[field_name] = str(audio.tags[frame_id])

    for key in audio.tags:
        if key.startswith("TXXX:"):
            desc = key.split(":", 1)[1]
            out[f"txxx_{desc.lower()}"] = str(audio.tags[key])
        elif key.startswith("GEOB:"):
            frame = audio.tags[key]
            out[f"geob_{key}"] = {
                "encoding": frame.encoding,
                "mime": frame.mime,
                "filename": frame.filename,
                "desc": frame.desc,
                "data": base64.b64encode(frame.data).decode("ascii"),
            }
        elif key.startswith("PRIV:"):
            frame = audio.tags[key]
            out[f"priv_{key}"] = {
                "owner": frame.owner,
                "data": base64.b64encode(frame.data).decode("ascii"),
            }
    return out


def _read_flac_tags(filepath: Path) -> dict[str, Any]:
    audio = FLAC(filepath)
    if not audio.tags:
        return {}
    out: dict[str, Any] = {
        "title": _first(audio.tags.get("title")),
        "artist": _first(audio.tags.get("artist")),
        "album": _first(audio.tags.get("album")),
        "genre": _first(audio.tags.get("genre")),
        "bpm": _first(audio.tags.get("bpm")),
        "key": _first(audio.tags.get("key")),
        "subgenre": _first(audio.tags.get("grouping")),
    }
    for custom in CUSTOM_TAGS:
        val = _first(audio.tags.get(custom))
        if val is not None:
            out[f"txxx_{custom}"] = val
    return out


def _read_m4a_tags(filepath: Path) -> dict[str, Any]:
    audio = MP4(filepath)
    if not audio.tags:
        return {}
    out: dict[str, Any] = {
        "title": _first(audio.tags.get("\xa9nam")),
        "artist": _first(audio.tags.get("\xa9ART")),
        "album": _first(audio.tags.get("\xa9alb")),
        "genre": _first(audio.tags.get("\xa9gen")),
    }
    if "tmpo" in audio.tags:
        out["bpm"] = str(audio.tags["tmpo"][0])
    return out


def _first(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, list):
        return value[0] if value else None
    return value


# ---------------------------------------------------------------------------
# Write tags — restore a snapshot to disk
# ---------------------------------------------------------------------------


def write_all_tags(
    filepath: Path,
    tags: dict[str, Any],
    config: TaggingConfig | None = None,
) -> tuple[bool, str | None]:
    """Restore tag snapshot from `tags` onto `filepath`. Returns (success, error).

    `config` controls the ID3 text-frame encoding used for MP3 writes; when
    omitted we use a fresh `TaggingConfig()` (UTF-8). FLAC/M4A ignore it.
    """
    ext = filepath.suffix.lower()
    cfg = config or TaggingConfig()
    try:
        if ext == ".mp3":
            _write_mp3_tags(filepath, tags, cfg)
        elif ext == ".flac":
            _write_flac_tags(filepath, tags)
        elif ext == ".m4a":
            _write_m4a_tags(filepath, tags)
        else:
            return False, f"Unsupported format: {ext}"
        return True, None
    except Exception as e:  # noqa: BLE001
        return False, str(e)


def _write_mp3_tags(filepath: Path, tags: dict[str, Any], config: TaggingConfig) -> None:
    audio = MP3(filepath)
    if audio.tags is None:
        audio.add_tags()

    enc = config.id3_text_encoding

    text_frame_writers = {
        "title": (TIT2, "TIT2"),
        "artist": (TPE1, "TPE1"),
        "album": (TALB, "TALB"),
        "genre": (TCON, "TCON"),
        "bpm": (TBPM, "TBPM"),
        "key": (TKEY, "TKEY"),
        "subgenre": (TIT1, "TIT1"),
    }
    for field_name, (frame_cls, frame_id) in text_frame_writers.items():
        if field_name in tags:
            audio.tags[frame_id] = frame_cls(encoding=enc, text=str(tags[field_name]))

    for key, val in tags.items():
        if key.startswith("txxx_"):
            tag_name = key[5:].upper()
            audio.tags.add(TXXX(encoding=enc, desc=tag_name, text=str(val)))
        elif key.startswith("geob_"):
            audio.tags.add(GEOB(
                encoding=val["encoding"],
                mime=val["mime"],
                filename=val["filename"],
                desc=val["desc"],
                data=base64.b64decode(val["data"]),
            ))
        elif key.startswith("priv_"):
            audio.tags.add(PRIV(
                owner=val["owner"],
                data=base64.b64decode(val["data"]),
            ))

    audio.save()


def _write_flac_tags(filepath: Path, tags: dict[str, Any]) -> None:
    audio = FLAC(filepath)
    field_map = {
        "title": "title", "artist": "artist", "album": "album", "genre": "genre",
        "bpm": "bpm", "key": "key", "subgenre": "grouping",
    }
    for field_name, vorbis_key in field_map.items():
        if field_name in tags:
            audio[vorbis_key] = str(tags[field_name])
    for key, val in tags.items():
        if key.startswith("txxx_"):
            audio[key[5:]] = str(val)
    audio.save()


def _write_m4a_tags(filepath: Path, tags: dict[str, Any]) -> None:
    audio = MP4(filepath)
    if "title" in tags:
        audio["\xa9nam"] = [tags["title"]]
    if "artist" in tags:
        audio["\xa9ART"] = [tags["artist"]]
    if "album" in tags:
        audio["\xa9alb"] = [tags["album"]]
    if "genre" in tags:
        audio["\xa9gen"] = [tags["genre"]]
    if "bpm" in tags:
        # `tmpo` is an int atom. A backup value like "128 BPM" or "" would
        # raise ValueError and fail the whole file restore — skip a
        # non-numeric BPM rather than abort.
        try:
            audio["tmpo"] = [int(float(str(tags["bpm"]).split()[0]))]
        except (ValueError, IndexError):
            log.debug("Skipping non-numeric M4A bpm %r", tags.get("bpm"))
    audio.save()


# ---------------------------------------------------------------------------
# Batch operations
# ---------------------------------------------------------------------------


def backup_tags(
    library_path: Path,
    output_path: Path,
    on_progress: ProgressCallback | None = None,
) -> BackupStats:
    """Snapshot every supported audio file under `library_path` to a JSON file."""
    files = find_audio_files(library_path)
    stats = BackupStats(total=len(files))

    backup = {
        "version": BACKUP_VERSION,
        "source_directory": str(library_path),
        "total_files": stats.total,
        "files": {},
    }

    # Lazy import so tagger stays usable as a library outside the sidecar.
    from vibechek import cancellation

    for i, filepath in enumerate(files):
        # Cancel must actually stop backing up — without this, a Cancel click
        # on a 12k-file backup runs to completion regardless.
        cancellation.check()
        report_progress(on_progress, i + 1, stats.total, filepath.name)
        try:
            backup["files"][str(filepath)] = read_all_tags(filepath)
            stats.backed_up += 1
        except Exception as e:  # noqa: BLE001
            stats.errors.append(f"{filepath.name}: {e}")

    # Atomic write: a kill-during-write of a tag backup leaves the user
    # unable to restore their original tags (audit Tags#1). The history-record
    # only fires after we're back from this function, so partial backups
    # never get indexed. Uses the shared `atomic_write_json` helper so the
    # crash-safety pattern lives in exactly one place across the codebase.
    output_path = Path(output_path)
    atomic_write_json(output_path, backup, indent=2, ensure_ascii=False)
    return stats


def _load_backup_files(backup_path: Path) -> dict[str, Any]:
    """Load + validate a backup JSON, returning its `files` mapping.

    Raises FileNotFoundError / ValueError with user-friendly messages on a
    missing / empty / non-JSON / wrong-shape backup, instead of letting a raw
    JSONDecodeError or KeyError leak to the GUI. Shared by both restore paths
    (`restore_tags` and `restore_tags_with_remap`) so neither can regress the
    audit Tags#2 hardening independently.
    """
    path = Path(backup_path)
    if not path.exists():
        raise FileNotFoundError(f"Backup file not found: {path}")
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        raise ValueError(f"Backup file is empty: {path}")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(
            f"Backup file at {path} is not valid JSON ({e}). "
            f"It may be truncated or corrupted; check the original "
            f"backup attempt's log for write errors."
        ) from e
    if not isinstance(data, dict) or "files" not in data:
        raise ValueError(
            f"Backup file at {path} is not in vibechek backup format "
            f"(missing 'files' key). Got top-level keys: "
            f"{list(data.keys()) if isinstance(data, dict) else type(data).__name__}"
        )
    files = data["files"]
    if not isinstance(files, dict):
        raise ValueError(
            f"Backup file at {path} has a 'files' entry that isn't an object "
            f"(got {type(files).__name__})."
        )
    return files


def restore_tags(
    backup_path: Path,
    on_progress: ProgressCallback | None = None,
    config: TaggingConfig | None = None,
) -> RestoreStats:
    """Restore tags from a backup created by `backup_tags`.

    `config` is forwarded to `write_all_tags` so MP3 restores honor the user's
    chosen ID3 text-frame encoding. Defaults to a fresh `TaggingConfig()`.

    Raises `ValueError` (with a user-friendly message) on corrupt / wrong-shape
    backup files, rather than letting `json.loads` or `data["files"]` leak a
    raw KeyError to the GUI (audit Tags#2).
    """
    from vibechek import cancellation

    files = _load_backup_files(backup_path)
    stats = RestoreStats(total=len(files))

    for i, (filepath_str, tags) in enumerate(files.items()):
        cancellation.check()
        report_progress(on_progress, i + 1, stats.total, Path(filepath_str).name)

        if "_error" in tags:
            continue  # File had a backup error — nothing to restore

        path = Path(filepath_str)
        if not path.exists():
            stats.skipped_missing += 1
            continue

        success, error = write_all_tags(path, tags, config)
        if success:
            stats.restored += 1
        else:
            stats.errors.append(f"{path.name}: {error}")

    return stats


# ---------------------------------------------------------------------------
# Apply ML analysis as tags
# ---------------------------------------------------------------------------


def apply_ml_tags(
    analysis_data: dict[str, Any],
    config: TaggingConfig,
    on_progress: ProgressCallback | None = None,
    dry_run: bool = False,
) -> ApplyStats:
    """Apply ML analysis results to files.

    Behavior matches `legacy/apply_tags_filtered.py`:
    - Genre/subgenre written only when ml confidence >= `genre_confidence_threshold`.
    - When `write_subgenre_as_main_genre`, subgenre goes into the main genre frame
      (Rekordbox can only sort by main genre).
    - BPM and key are skipped unless `skip_bpm_and_key` is False — Rekordbox's own
      detection is more reliable for these.
    - GEOB / PRIV frames are always preserved on MP3 when
      `preserve_rekordbox_frames` is True.
    """
    from vibechek import cancellation

    tracks = analysis_data.get("tracks", [])
    stats = ApplyStats(total=len(tracks))

    for i, track in enumerate(tracks):
        cancellation.check()
        report_progress(on_progress, i + 1, stats.total, Path(track.get("path", "")).name)

        filepath = Path(track["path"])
        if not filepath.exists():
            stats.errors.append(f"Not found: {filepath}")
            continue

        ml = track.get("ml_analysis", {})
        if not ml:
            continue

        # Two-stage genre confidence.
        #
        # Stage 1 (strict): If the *subgenre* (raw, single-class) confidence
        # clears `genre_confidence_threshold` (default 0.85), write the
        # subgenre as the genre. This is the legacy behaviour.
        #
        # Stage 2 (parent fallback): If stage 1 fails BUT the parent-family
        # confidence (`ml_genre_confidence` — the summed score across related
        # subgenres) clears `parent_genre_confidence_threshold` (default 0.50),
        # write the PARENT genre into the genre field with no subgenre. This
        # is the case where the model knows the track is unambiguously House
        # but can't decide Deep House vs. Tech House — historically that
        # left the track tagless (~47% of a typical library). Now those land
        # under their parent, dramatically improving coverage.
        #
        # `ml_genre_raw_confidence` is set by analyzer.py from
        # `GenreResult.raw_confidence` (top single Discogs class). It may be
        # absent on older analysis reports — in that case we fall back to
        # `ml_genre_confidence` for both stages, which yields the legacy
        # behaviour exactly.
        family_conf = ml.get("ml_genre_confidence") or 0.0
        subgenre_conf = ml.get("ml_genre_raw_confidence")
        is_legacy_report = subgenre_conf is None
        if is_legacy_report:
            # Backward-compat for analysis reports written before raw_confidence
            # was plumbed. Use family confidence as the stage-1 input so we
            # don't accidentally over-tag.
            subgenre_conf = family_conf

        subgenre = ml.get("ml_subgenre", "") or ""
        parent_genre = ml.get("ml_genre", "") or ""

        apply_subgenre = (
            subgenre_conf >= config.genre_confidence_threshold
            and bool(subgenre)
        )
        # Stage 2 (parent-only) must NOT fire on legacy reports. Without
        # `ml_genre_raw_confidence` we can't tell a genuine high-confidence
        # parent from a subgenre prediction, so `family_conf` here is really
        # the old single-stage confidence. Applying the 0.50 parent gate to it
        # would tag ~30% more tracks than the user saw when that report was
        # the live behaviour ("tagless below 0.85"), silently writing genres
        # to files on a simple re-apply. Re-analyze to get the two-stage
        # behaviour on these tracks. (Matches the docstring's "yields the
        # legacy behaviour exactly".)
        apply_parent_only = (
            not apply_subgenre
            and not is_legacy_report
            and family_conf >= config.parent_genre_confidence_threshold
            and bool(parent_genre)
        )

        # `genre_to_write` is what actually lands in the TCON / GENRE frame.
        # For stage 2 we want the parent genre (no subgenre) so Rekordbox sees
        # "House" rather than "Deep House" — the user can still browse the
        # parent bucket while tracks the model couldn't subgenre-classify
        # don't pollute it.
        if apply_subgenre:
            genre_to_write = subgenre
            stats.genre_applied += 1
        elif apply_parent_only:
            genre_to_write = parent_genre
            stats.genre_applied_parent_only += 1
        else:
            genre_to_write = ""
            stats.genre_skipped_low_confidence += 1

        apply_genre = apply_subgenre or apply_parent_only

        if dry_run:
            continue

        ext = filepath.suffix.lower()
        try:
            if ext == ".mp3":
                _apply_mp3(filepath, ml, apply_genre, genre_to_write, config)
                stats.other_tags_applied += 1
            elif ext == ".flac":
                _apply_flac(filepath, ml, apply_genre, genre_to_write, config)
                stats.other_tags_applied += 1
            else:
                stats.errors.append(f"{filepath.name}: unsupported format {ext}")
        except Exception as e:  # noqa: BLE001
            stats.errors.append(f"{filepath.name}: {e}")

    return stats


def _apply_mp3(
    filepath: Path,
    ml: dict[str, Any],
    apply_genre: bool,
    genre_value: str,
    config: TaggingConfig,
) -> None:
    audio = MP3(filepath)
    if audio.tags is None:
        audio.add_tags()

    enc = config.id3_text_encoding

    # Snapshot binary frames to restore after destructive writes
    preserved: list[tuple[str, Any]] = []
    if config.preserve_rekordbox_frames:
        preserved = [
            (k, audio.tags[k])
            for k in list(audio.tags.keys())
            if k.startswith("GEOB:") or k.startswith("PRIV:")
        ]

    if apply_genre:
        audio.tags.delall("TCON")
        audio.tags.add(TCON(encoding=enc, text=[genre_value]))
        # Only write TIT1 (subgenre frame) when the value we're writing is
        # genuinely a subgenre — i.e. when stage-1 (strict subgenre conf)
        # passed. When we fall back to the parent genre under stage 2 we
        # leave TIT1 untouched so we don't mislabel an unclear track with
        # a confident-looking subgenre tag.
        ml_subgenre = ml.get("ml_subgenre", "") or ""
        if genre_value == ml_subgenre:
            audio.tags.delall("TIT1")
            audio.tags.add(TIT1(encoding=enc, text=[genre_value]))

    if not config.skip_bpm_and_key:
        if ml.get("ml_bpm"):
            audio.tags.delall("TBPM")
            audio.tags.add(TBPM(encoding=enc, text=[str(int(round(ml["ml_bpm"])))]))
        if ml.get("ml_key"):
            audio.tags.delall("TKEY")
            audio.tags.add(TKEY(encoding=enc, text=[ml["ml_key"]]))

    for tag_name in ("ENERGY", "MOOD", "TIMESLOT", "DIRECTION", "VOCAL"):
        val = ml.get(f"ml_{tag_name.lower()}")
        if val is not None:
            audio.tags.delall(f"TXXX:{tag_name}")
            audio.tags.add(TXXX(encoding=enc, desc=tag_name, text=[str(val)]))

    for key, frame in preserved:
        if key not in audio.tags:
            audio.tags.add(frame)

    audio.save()


def _apply_flac(
    filepath: Path,
    ml: dict[str, Any],
    apply_genre: bool,
    genre_value: str,
    config: TaggingConfig,
) -> None:
    audio = FLAC(filepath)
    if apply_genre:
        audio["GENRE"] = genre_value
        # CONTENTGROUP is the FLAC analog of MP3's TIT1 (subgenre) — only
        # populate it when the value we're writing is genuinely a subgenre.
        # Stage-2 parent-only fallback should NOT pollute CONTENTGROUP.
        ml_subgenre = ml.get("ml_subgenre", "") or ""
        if genre_value == ml_subgenre:
            audio["CONTENTGROUP"] = genre_value

    if not config.skip_bpm_and_key:
        if ml.get("ml_bpm"):
            audio["BPM"] = str(int(round(ml["ml_bpm"])))
        if ml.get("ml_key"):
            audio["INITIALKEY"] = ml["ml_key"]

    for tag_name in ("ENERGY", "MOOD", "TIMESLOT", "DIRECTION", "VOCAL"):
        val = ml.get(f"ml_{tag_name.lower()}")
        if val is not None:
            audio[tag_name] = str(val)

    audio.save()


@dataclass
class RemapRestoreStats:
    """Result of restore_tags_with_remap.

    Adds per-strategy match counts on top of `RestoreStats`, plus a per-file
    breakdown so the UI can show which files needed remapping (and why others
    were skipped).
    """

    total: int = 0
    restored: int = 0
    skipped_missing: int = 0
    skipped_size_mismatch: int = 0
    matched_exact: int = 0
    matched_filename_size: int = 0
    matched_filename: int = 0
    errors: list[str] = field(default_factory=list)
    # One entry per backup file: {"original": str, "matched": str|None,
    # "strategy": "exact"|"filename_size"|"filename"|"missing"|"size_mismatch"
    #              |"ambiguous"|"backup_error"|"write_error",
    # "error": str|None}.
    matches: list[dict[str, Any]] = field(default_factory=list)


def restore_tags_with_remap(
    backup_path: Path,
    library_root: Path,
    on_progress: ProgressCallback | None = None,
) -> RemapRestoreStats:
    """Restore a tag backup with automatic remap for moved libraries.

    Audit #19: `restore_tags` keys exclusively on the original absolute path
    captured at backup time. If the user backed up `D:\\Music\\foo.mp3` then
    renamed the drive to `E:\\` (or moved the whole library), restore skips
    every file. This function walks `library_root`, then for each backup entry
    attempts three strategies in order:

    1. **Exact path** — the backup's original path exists on disk verbatim.
    2. **Filename + size** — a file under `library_root` has the same name
       AND the same byte size as the backup entry. This covers moved-but-
       unmodified libraries (drive rename, folder relocation).
    3. **Filename alone** — a single file under `library_root` has the same
       name (no other file in the library shares the name). Last-resort
       fallback for libraries where file sizes have drifted (e.g., a tagger
       changed bytes after the backup).

    A backup entry whose `filename + size` matches *multiple* files in
    `library_root` is marked `ambiguous` (skipped) — restoring to the wrong
    file would silently corrupt tags.
    """
    files = _load_backup_files(backup_path)
    stats = RemapRestoreStats(total=len(files))

    # Build an index of the destination library so we can resolve moved files.
    # `find_audio_files` returns sorted Paths; we group by basename so both the
    # filename+size and filename-only strategies are O(1) per backup entry.
    library_root = Path(library_root)
    library_files = find_audio_files(library_root)
    by_name: dict[str, list[Path]] = {}
    for p in library_files:
        by_name.setdefault(p.name.lower(), []).append(p)

    # Lazy size lookup — only `stat()` files we actually consider. On a 12k
    # library that's the difference between 12k unconditional stats and ~N
    # stats where N is the number of backup entries we couldn't exact-match.
    sizes: dict[Path, int] = {}

    def size_of(path: Path) -> int | None:
        if path not in sizes:
            try:
                sizes[path] = path.stat().st_size
            except OSError:
                return None
        return sizes[path]

    for i, (filepath_str, tags) in enumerate(files.items()):
        original = Path(filepath_str)
        report_progress(on_progress, i + 1, stats.total, original.name)

        match_record: dict[str, Any] = {
            "original": filepath_str,
            "matched": None,
            "strategy": None,
            "error": None,
        }

        if "_error" in tags:
            match_record["strategy"] = "backup_error"
            stats.matches.append(match_record)
            continue

        # Strategy 1: exact path
        target: Path | None = None
        strategy: str = ""
        if original.exists():
            target = original
            strategy = "exact"
        else:
            candidates = by_name.get(original.name.lower(), [])
            backup_size = _backup_entry_size(tags)

            # Strategy 2: filename + size (only when backup recorded a size).
            # The current backup format doesn't store size per entry, so we
            # tolerate either a missing `_size` (skip strategy 2) or a real one.
            if backup_size is not None:
                size_matches = [p for p in candidates if size_of(p) == backup_size]
                if len(size_matches) == 1:
                    target = size_matches[0]
                    strategy = "filename_size"
                elif len(size_matches) > 1:
                    # Ambiguous on (name, size). Don't gamble — the user can
                    # rerun against a smaller library_root, or recreate the
                    # backup. Falling back to filename-alone here would be
                    # even less safe.
                    match_record["strategy"] = "ambiguous"
                    stats.skipped_size_mismatch += 1
                    stats.matches.append(match_record)
                    continue

            # Strategy 3: filename alone — only safe when unique in the library.
            if target is None:
                if len(candidates) == 1:
                    target = candidates[0]
                    strategy = "filename"
                elif len(candidates) > 1:
                    match_record["strategy"] = "ambiguous"
                    stats.matches.append(match_record)
                    # Treat as skip_missing for top-line stat (no write happened)
                    stats.skipped_missing += 1
                    continue

        if target is None:
            match_record["strategy"] = "missing"
            stats.matches.append(match_record)
            stats.skipped_missing += 1
            continue

        match_record["matched"] = str(target)
        match_record["strategy"] = strategy

        # Tally the *match* strategy regardless of whether the write succeeds —
        # the user wants to know "12 files were remapped via filename" even if
        # one of them then failed to write. Write failures are reported
        # separately via `errors` + a `substrategy=write_error` on the record.
        if strategy == "exact":
            stats.matched_exact += 1
        elif strategy == "filename_size":
            stats.matched_filename_size += 1
        elif strategy == "filename":
            stats.matched_filename += 1

        success, error = write_all_tags(target, tags)
        if success:
            stats.restored += 1
        else:
            match_record["substrategy"] = "write_error"
            match_record["error"] = error
            stats.errors.append(f"{target.name}: {error}")

        stats.matches.append(match_record)

    return stats


def _backup_entry_size(tags: dict[str, Any]) -> int | None:
    """Return the backup-recorded byte size of a file, if present.

    The current `read_all_tags` format does not capture file size, so this
    almost always returns None today. Plumbing it through here means a future
    bump to the backup format (adding `_size`) lights up the strategy 2 path
    automatically — no further code changes needed.
    """
    raw = tags.get("_size")
    if isinstance(raw, int) and raw > 0:
        return raw
    return None


__all__ = [
    "BackupStats",
    "RestoreStats",
    "RemapRestoreStats",
    "ApplyStats",
    "read_all_tags",
    "write_all_tags",
    "backup_tags",
    "restore_tags",
    "restore_tags_with_remap",
    "apply_ml_tags",
    "SUPPORTED_EXTENSIONS",
]
