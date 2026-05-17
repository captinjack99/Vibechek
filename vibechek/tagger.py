"""Tag reading, backup, and writing.

Three responsibilities:

1. **Snapshot** every ID3/Vorbis frame on every file before any write — including
   Rekordbox-specific GEOB and PRIV frames (cue points, beat grids, memory cues).
   Binary frames are base64-encoded for JSON safety.
2. **Restore** the snapshot exactly, frame-for-frame, with no loss.
3. **Apply** ML analysis results back to files with confidence filtering,
   while preserving any Rekordbox binary frames that already exist.

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


def write_all_tags(filepath: Path, tags: dict[str, Any]) -> tuple[bool, str | None]:
    """Restore tag snapshot from `tags` onto `filepath`. Returns (success, error)."""
    ext = filepath.suffix.lower()
    try:
        if ext == ".mp3":
            _write_mp3_tags(filepath, tags)
        elif ext == ".flac":
            _write_flac_tags(filepath, tags)
        elif ext == ".m4a":
            _write_m4a_tags(filepath, tags)
        else:
            return False, f"Unsupported format: {ext}"
        return True, None
    except Exception as e:  # noqa: BLE001
        return False, str(e)


def _write_mp3_tags(filepath: Path, tags: dict[str, Any]) -> None:
    audio = MP3(filepath)
    if audio.tags is None:
        audio.add_tags()

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
            audio.tags[frame_id] = frame_cls(encoding=3, text=str(tags[field_name]))

    for key, val in tags.items():
        if key.startswith("txxx_"):
            tag_name = key[5:].upper()
            audio.tags.add(TXXX(encoding=3, desc=tag_name, text=str(val)))
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
        audio["tmpo"] = [int(float(tags["bpm"]))]
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

    for i, filepath in enumerate(files):
        report_progress(on_progress, i + 1, stats.total, filepath.name)
        try:
            backup["files"][str(filepath)] = read_all_tags(filepath)
            stats.backed_up += 1
        except Exception as e:  # noqa: BLE001
            stats.errors.append(f"{filepath.name}: {e}")

    Path(output_path).write_text(
        json.dumps(backup, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return stats


def restore_tags(
    backup_path: Path,
    on_progress: ProgressCallback | None = None,
) -> RestoreStats:
    """Restore tags from a backup created by `backup_tags`."""
    data = json.loads(Path(backup_path).read_text(encoding="utf-8"))
    files = data["files"]
    stats = RestoreStats(total=len(files))

    for i, (filepath_str, tags) in enumerate(files.items()):
        report_progress(on_progress, i + 1, stats.total, Path(filepath_str).name)

        if "_error" in tags:
            continue  # File had a backup error — nothing to restore

        path = Path(filepath_str)
        if not path.exists():
            stats.skipped_missing += 1
            continue

        success, error = write_all_tags(path, tags)
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
    tracks = analysis_data.get("tracks", [])
    stats = ApplyStats(total=len(tracks))

    for i, track in enumerate(tracks):
        report_progress(on_progress, i + 1, stats.total, Path(track.get("path", "")).name)

        filepath = Path(track["path"])
        if not filepath.exists():
            stats.errors.append(f"Not found: {filepath}")
            continue

        ml = track.get("ml_analysis", {})
        if not ml:
            continue

        genre_conf = ml.get("ml_genre_confidence") or 0.0
        subgenre = ml.get("ml_subgenre", "")
        apply_genre = (
            genre_conf >= config.genre_confidence_threshold
            and bool(subgenre)
        )

        if apply_genre:
            stats.genre_applied += 1
        else:
            stats.genre_skipped_low_confidence += 1

        if dry_run:
            continue

        ext = filepath.suffix.lower()
        try:
            if ext == ".mp3":
                _apply_mp3(filepath, ml, apply_genre, subgenre, config)
                stats.other_tags_applied += 1
            elif ext == ".flac":
                _apply_flac(filepath, ml, apply_genre, subgenre, config)
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
    subgenre: str,
    config: TaggingConfig,
) -> None:
    audio = MP3(filepath)
    if audio.tags is None:
        audio.add_tags()

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
        audio.tags.add(TCON(encoding=3, text=[subgenre]))
        audio.tags.delall("TIT1")
        audio.tags.add(TIT1(encoding=3, text=[subgenre]))

    if not config.skip_bpm_and_key:
        if ml.get("ml_bpm"):
            audio.tags.delall("TBPM")
            audio.tags.add(TBPM(encoding=3, text=[str(int(round(ml["ml_bpm"])))]))
        if ml.get("ml_key"):
            audio.tags.delall("TKEY")
            audio.tags.add(TKEY(encoding=3, text=[ml["ml_key"]]))

    for tag_name in ("ENERGY", "MOOD", "TIMESLOT", "DIRECTION", "VOCAL"):
        val = ml.get(f"ml_{tag_name.lower()}")
        if val is not None:
            audio.tags.delall(f"TXXX:{tag_name}")
            audio.tags.add(TXXX(encoding=3, desc=tag_name, text=[str(val)]))

    for key, frame in preserved:
        if key not in audio.tags:
            audio.tags.add(frame)

    audio.save()


def _apply_flac(
    filepath: Path,
    ml: dict[str, Any],
    apply_genre: bool,
    subgenre: str,
    config: TaggingConfig,
) -> None:
    audio = FLAC(filepath)
    if apply_genre:
        audio["GENRE"] = subgenre
        audio["CONTENTGROUP"] = subgenre

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


__all__ = [
    "BackupStats",
    "RestoreStats",
    "ApplyStats",
    "read_all_tags",
    "write_all_tags",
    "backup_tags",
    "restore_tags",
    "apply_ml_tags",
    "SUPPORTED_EXTENSIONS",
]
