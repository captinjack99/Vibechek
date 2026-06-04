"""Tag reading, backup, and writing.

Three responsibilities:

1. **Snapshot** every ID3/Vorbis/atom on every file before any write — including
   Rekordbox-specific GEOB and PRIV frames (cue points, beat grids, memory cues).
   ID3-bearing formats (MP3 *and* AIFF/WAV) capture GEOB/PRIV; FLAC captures every
   Vorbis comment; M4A captures every atom (binary atoms like ``covr`` / Serato
   freeform are base64-encoded). Binary payloads are base64-encoded for JSON safety.
   Formats with no tag-reader path are recorded as ``_unsupported`` so the count
   surfaces in ``BackupStats`` rather than masquerading as a complete backup.
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

from mutagen.aiff import AIFF
from mutagen.flac import FLAC
from mutagen.id3 import (
    GEOB,
    ID3,
    PRIV,
    TALB,
    TBPM,
    TCON,
    TIT1,
    TIT2,
    TKEY,
    TPE1,
    TXXX,
)
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4, MP4Cover, MP4FreeForm
from mutagen.wave import WAVE

from vibechek.config import TaggingConfig
from vibechek.io import atomic_write_json
from vibechek.utils import (
    SUPPORTED_EXTENSIONS,
    ProgressCallback,
    find_audio_files,
    report_progress,
    resolve_existing_path,
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
    # Files that were scanned and written into the backup but whose format has
    # no tag-reader path (`read_all_tags` returned `_unsupported`). These carry
    # NO tags, so a restore can't put anything back — they are NOT a safety net.
    # Surfaced so the UI can warn "N files not fully backed up" instead of the
    # backup silently claiming "no loss" for formats it can't actually read.
    not_fully_backed_up: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass
class RestoreStats:
    total: int = 0
    restored: int = 0
    skipped_missing: int = 0
    # Backup entries whose format had no reader at backup time (`_unsupported`):
    # there is nothing to restore, but we count them explicitly rather than
    # silently skipping, so the caller can tell "30 files had no backup to
    # restore" apart from "30 files were already up to date".
    skipped_unsupported: int = 0
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

    The backup is FORMAT-COMPLETE for every reader we have:

    - **MP3 / AIFF / WAV** (all ID3-based): every text frame plus all TXXX, and
      the Rekordbox-specific GEOB and PRIV binary frames base64-encoded. AIFF
      and WAV carry the *same* ID3 cue/beatgrid frames as MP3, so dropping them
      (as the old code did) was a real cue-loss risk on restore.
    - **FLAC**: every Vorbis comment (not a fixed subset), so ReplayGain,
      label/catalog, comments and Serato fields all survive.
    - **M4A**: every atom, with binary atoms (``covr`` artwork, Serato
      ``----``-freeform atoms) base64-encoded.

    Any extension we have no reader for is recorded as ``{"_unsupported": True}``
    so the count surfaces in `BackupStats.not_fully_backed_up` and restore can
    report it, rather than the backup silently claiming "no loss".
    """
    ext = filepath.suffix.lower()
    tags: dict[str, Any] = {}
    try:
        if ext == ".mp3":
            tags = _read_id3_tags(MP3(filepath).tags)
        elif ext in (".aiff", ".aif"):
            tags = _read_id3_tags(AIFF(filepath).tags)
        elif ext == ".wav":
            tags = _read_id3_tags(WAVE(filepath).tags)
        elif ext == ".flac":
            tags = _read_flac_tags(filepath)
        elif ext == ".m4a":
            tags = _read_m4a_tags(filepath)
        else:
            tags["_unsupported"] = True
    except Exception as e:  # noqa: BLE001
        tags["_error"] = str(e)
    return {k: v for k, v in tags.items() if v is not None}


def _read_id3_tags(id3: ID3 | None) -> dict[str, Any]:
    """Snapshot an ID3 tag block (shared by MP3, AIFF and WAV).

    AIFF/WAV embed a standard ID3v2 chunk, so the same GEOB/PRIV cue frames a
    DJ tool writes to MP3 can live here too — this is the single place that
    captures them for every ID3-bearing format.
    """
    out: dict[str, Any] = {}
    if not id3:
        return out

    text_frame_map = {
        "TIT2": "title", "TPE1": "artist", "TALB": "album", "TCON": "genre",
        "TBPM": "bpm", "TKEY": "key", "TIT1": "subgenre",
    }
    for frame_id, field_name in text_frame_map.items():
        if frame_id in id3:
            out[field_name] = str(id3[frame_id])

    for key in id3:
        if key.startswith("TXXX:"):
            desc = key.split(":", 1)[1]
            # TXXX descriptions are CASE-SENSITIVE per ID3 (ReplayGain frames,
            # Mixed In Key `EnergyLevel`, Serato fields). The lowercased key is
            # only for lookup/dedup; preserve the real `desc` verbatim so a
            # restore rebuilds the frame byte-identically instead of forcing it
            # to UPPER-case (which renames the frame and breaks case-sensitive
            # readers).
            out[f"txxx_{desc.lower()}"] = {"desc": desc, "text": str(id3[key])}
        elif key.startswith("GEOB:"):
            frame = id3[key]
            out[f"geob_{key}"] = {
                "encoding": frame.encoding,
                "mime": frame.mime,
                "filename": frame.filename,
                "desc": frame.desc,
                "data": base64.b64encode(frame.data).decode("ascii"),
            }
        elif key.startswith("PRIV:"):
            frame = id3[key]
            out[f"priv_{key}"] = {
                "owner": frame.owner,
                "data": base64.b64encode(frame.data).decode("ascii"),
            }
    return out


def _read_flac_tags(filepath: Path) -> dict[str, Any]:
    audio = FLAC(filepath)
    if not audio.tags:
        return {}
    # Pull the well-known DJ fields into our canonical names so restore writes
    # them back to the right Vorbis key (genre→GENRE, subgenre→GROUPING, ...).
    canonical = {
        "title": "title", "artist": "artist", "album": "album",
        "genre": "genre", "bpm": "bpm", "key": "key", "grouping": "subgenre",
    }
    out: dict[str, Any] = {}
    handled: set[str] = set()
    for vorbis_key, field_name in canonical.items():
        values = audio.tags.get(vorbis_key)
        val = _first(values)
        if val is not None:
            # Keep a SCALAR canonical key for display / apply consumers...
            out[field_name] = val
            handled.add(vorbis_key)
            # ...but if the field is multi-valued (collaborations, multi-genre,
            # classical performers), also stash the FULL list under the generic
            # `txxx_<key>` entry so restore is lossless instead of dropping the
            # 2nd+ values. `_write_flac_tags` writes the list back to the same
            # Vorbis key, overriding the scalar.
            if isinstance(values, list) and len(values) > 1:
                out[f"txxx_{vorbis_key}"] = list(values)
    # Format-complete: capture EVERY remaining Vorbis comment (ReplayGain,
    # label/catalog, comment, Serato fields, custom energy/mood/etc.) as a
    # `txxx_<key>` entry so restore puts it back verbatim. Store the FULL value
    # list (not just value[0]) so multi-valued comments survive. The old code
    # only snapshotted a fixed field set and collapsed everything to the first.
    for vorbis_key in audio.tags.keys():
        lk = vorbis_key.lower()
        if lk in handled:
            continue
        values = audio.tags.get(vorbis_key)
        if not values:
            continue
        # Single-valued comment stays a scalar (the historical snapshot shape);
        # only multi-valued comments are stored as a list, so a backup's 2nd+
        # values are no longer dropped while existing scalar snapshots/round-
        # trips are unchanged.
        if isinstance(values, list) and len(values) > 1:
            out[f"txxx_{lk}"] = list(values)
        else:
            out[f"txxx_{lk}"] = _first(values)
    return out


def _read_m4a_tags(filepath: Path) -> dict[str, Any]:
    audio = MP4(filepath)
    if not audio.tags:
        return {}
    # Pull the well-known atoms into canonical names; everything else is
    # captured generically below so the backup is format-complete.
    canonical = {
        "\xa9nam": "title", "\xa9ART": "artist", "\xa9alb": "album",
        "\xa9gen": "genre",
    }
    out: dict[str, Any] = {}
    handled: set[str] = set()
    for atom, field_name in canonical.items():
        values = audio.tags.get(atom)
        val = _first(values)
        if val is not None:
            # Scalar canonical key for display / apply...
            out[field_name] = val
            handled.add(atom)
            # ...plus the FULL list under the generic atom key when the atom is
            # multi-valued (multiple ©ART / ©gen), so a restore brings every
            # value back instead of only the first. `_write_m4a_tags` writes the
            # generic atom after the scalar, overriding it with the full list.
            if isinstance(values, list) and len(values) > 1:
                out[f"mp4atom_{atom}"] = _encode_m4a_atom(values)
    if "tmpo" in audio.tags:
        out["bpm"] = str(audio.tags["tmpo"][0])
        handled.add("tmpo")

    # Format-complete: snapshot EVERY other atom. Binary atoms (artwork `covr`,
    # Serato `----:...` freeform atoms, any bytes payload) are base64-encoded
    # under an `mp4atom_<atom>` key so they round-trip exactly; text/numeric
    # atoms keep their values. The old code dropped all of these.
    for atom in audio.tags.keys():
        if atom in handled:
            continue
        out[f"mp4atom_{atom}"] = _encode_m4a_atom(audio.tags[atom])
    return out


def _encode_m4a_atom(value: Any) -> Any:
    """JSON-safe encoding of an arbitrary MP4 atom value list.

    Returns a dict tagged with how it was encoded so `_write_m4a_tags` can
    rebuild the exact mutagen type on restore (base64 for binary atoms,
    freeform metadata preserved).
    """
    items: list[Any] = []
    for item in value if isinstance(value, list) else [value]:
        if isinstance(item, MP4FreeForm):
            items.append({
                "kind": "freeform",
                "dataformat": int(item.dataformat),
                "data": base64.b64encode(bytes(item)).decode("ascii"),
            })
        elif isinstance(item, MP4Cover):
            items.append({
                "kind": "cover",
                "imageformat": int(item.imageformat),
                "data": base64.b64encode(bytes(item)).decode("ascii"),
            })
        elif isinstance(item, (bytes, bytearray)):
            items.append({
                "kind": "bytes",
                "data": base64.b64encode(bytes(item)).decode("ascii"),
            })
        else:
            items.append({"kind": "plain", "value": item})
    return {"_mp4atom": True, "items": items}


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

    `config` controls the ID3 text-frame encoding used for ID3 writes (MP3 and
    AIFF/WAV); when omitted we use a fresh `TaggingConfig()` (UTF-8). FLAC/M4A
    ignore it.
    """
    ext = filepath.suffix.lower()
    cfg = config or TaggingConfig()
    try:
        if ext == ".mp3":
            audio = MP3(filepath)
            if audio.tags is None:
                audio.add_tags()
            _write_id3_tags(audio.tags, tags, cfg.id3_text_encoding)
            audio.save()
        elif ext in (".aiff", ".aif"):
            audio = AIFF(filepath)
            if audio.tags is None:
                audio.add_tags()
            _write_id3_tags(audio.tags, tags, cfg.id3_text_encoding)
            audio.save()
        elif ext == ".wav":
            audio = WAVE(filepath)
            if audio.tags is None:
                audio.add_tags()
            _write_id3_tags(audio.tags, tags, cfg.id3_text_encoding)
            audio.save()
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
    """Restore an ID3 snapshot to an MP3 (kept for the public test surface)."""
    audio = MP3(filepath)
    if audio.tags is None:
        audio.add_tags()
    _write_id3_tags(audio.tags, tags, config.id3_text_encoding)
    audio.save()


def _write_id3_tags(id3: ID3, tags: dict[str, Any], enc: int) -> None:
    """Write a tag snapshot into an ID3 block (shared by MP3, AIFF and WAV).

    Pre-clears any existing GEOB/PRIV frames before re-adding the snapshot's
    own, so a restore is idempotent and can't leave a *stale* cue/beatgrid
    frame from the live file sitting alongside the restored one. (Text frames
    are already overwritten by key; only the keyed-by-description binary frames
    needed the explicit clear.)
    """
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
            id3[frame_id] = frame_cls(encoding=enc, text=str(tags[field_name]))

    # Pre-clear existing GEOB/PRIV so the snapshot's frames fully replace
    # whatever is on disk rather than accumulating duplicates.
    if any(k.startswith(("geob_", "priv_")) for k in tags):
        for existing in [k for k in list(id3.keys())
                         if k.startswith("GEOB:") or k.startswith("PRIV:")]:
            del id3[existing]

    for key, val in tags.items():
        if key.startswith("txxx_"):
            # New snapshots store {"desc": <verbatim>, "text": ...} so the
            # original case-sensitive TXXX description round-trips exactly.
            # Old snapshots stored a bare string under a lowercased key — for
            # those, fall back to the key suffix verbatim (no case-folding)
            # rather than the historical .upper(), which renamed frames.
            if isinstance(val, dict):
                desc = val.get("desc", key[5:])
                text = val.get("text", "")
            else:
                desc = key[5:]
                text = val
            id3.add(TXXX(encoding=enc, desc=desc, text=str(text)))
        elif key.startswith("geob_"):
            id3.add(GEOB(
                encoding=val["encoding"],
                mime=val["mime"],
                filename=val["filename"],
                desc=val["desc"],
                data=base64.b64decode(val["data"]),
            ))
        elif key.startswith("priv_"):
            id3.add(PRIV(
                owner=val["owner"],
                data=base64.b64decode(val["data"]),
            ))


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
            # Preserve multi-valued Vorbis comments: a list snapshot is written
            # back as a list (every value), a scalar as a single string. This
            # also overrides any scalar canonical field written above (e.g. a
            # multi-artist ARTIST) so restore is lossless.
            if isinstance(val, list):
                audio[key[5:]] = [str(v) for v in val]
            else:
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
    # Restore the generic atoms captured by `_read_m4a_tags` — binary atoms
    # (artwork, Serato freeform) are rebuilt as their exact mutagen type.
    for key, val in tags.items():
        if key.startswith("mp4atom_"):
            atom = key[len("mp4atom_"):]
            try:
                audio[atom] = _decode_m4a_atom(val)
            except Exception:  # noqa: BLE001
                log.debug("Skipping un-decodable M4A atom %r", atom)
    audio.save()


def _decode_m4a_atom(encoded: Any) -> list[Any]:
    """Rebuild a list of mutagen MP4 atom values from `_encode_m4a_atom`."""
    if not (isinstance(encoded, dict) and encoded.get("_mp4atom")):
        # Legacy/plain value — wrap as-is.
        return encoded if isinstance(encoded, list) else [encoded]
    out: list[Any] = []
    for item in encoded["items"]:
        kind = item.get("kind")
        if kind == "freeform":
            out.append(MP4FreeForm(
                base64.b64decode(item["data"]),
                dataformat=item["dataformat"],
            ))
        elif kind == "cover":
            out.append(MP4Cover(
                base64.b64decode(item["data"]),
                imageformat=item["imageformat"],
            ))
        elif kind == "bytes":
            out.append(base64.b64decode(item["data"]))
        else:
            out.append(item["value"])
    return out


# ---------------------------------------------------------------------------
# Batch operations
# ---------------------------------------------------------------------------


def backup_tags(
    library_path: Path,
    output_path: Path,
    on_progress: ProgressCallback | None = None,
) -> BackupStats:
    """Snapshot every supported audio file under `library_path` to a JSON file."""
    return _snapshot_tags_to_file(
        find_audio_files(library_path), str(library_path), output_path, on_progress
    )


def backup_tag_files(
    paths: list[str] | list[Path],
    output_path: Path,
    on_progress: ProgressCallback | None = None,
) -> BackupStats:
    """Snapshot a SPECIFIC list of audio files (not a directory scan).

    Used by the "backup before write" safety net in the apply-tags flow: it
    snapshots exactly the files about to be mutated (the apply set carries only
    track paths, not a library root), so the backup scope matches the change and
    no broad tree walk happens. Same on-disk format as `backup_tags`, so the
    normal restore paths read it.
    """
    files = [Path(p) for p in paths if Path(p).exists()]
    source = str(files[0].parent) if files else ""
    return _snapshot_tags_to_file(files, source, output_path, on_progress)


def _snapshot_tags_to_file(
    files: list[Path],
    source_directory: str,
    output_path: Path,
    on_progress: ProgressCallback | None = None,
) -> BackupStats:
    """Read every file's tags and atomically write the backup JSON. Shared core
    of `backup_tags` (directory scan) and `backup_tag_files` (explicit list)."""
    stats = BackupStats(total=len(files))

    backup = {
        "version": BACKUP_VERSION,
        "source_directory": source_directory,
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
            entry = read_all_tags(filepath)
            # Record byte size so `restore_tags_with_remap`'s (filename, size)
            # strategy can disambiguate same-named tracks in a moved library
            # instead of falling back to the risky basename-only match.
            try:
                entry["_size"] = filepath.stat().st_size
            except OSError:
                pass
            backup["files"][str(filepath)] = entry
            stats.backed_up += 1
            # A format we have no reader for carries NO tags — count it so the
            # caller can warn instead of treating it as a real safety net.
            if entry.get("_unsupported"):
                stats.not_fully_backed_up += 1
        except Exception as e:  # noqa: BLE001
            stats.errors.append(f"{filepath.name}: {e}")

    # Atomic write: a kill-during-write of a tag backup leaves the user
    # unable to restore their original tags. The history-record
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
    hardening independently.
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
    raw KeyError to the GUI.
    """
    from vibechek import cancellation

    files = _load_backup_files(backup_path)
    stats = RestoreStats(total=len(files))

    for i, (filepath_str, tags) in enumerate(files.items()):
        cancellation.check()
        report_progress(on_progress, i + 1, stats.total, Path(filepath_str).name)

        if "_error" in tags:
            continue  # File had a backup error — nothing to restore

        if tags.get("_unsupported"):
            # This format had no reader at backup time, so there's nothing to
            # put back. Record it explicitly rather than letting it fall through
            # to `write_all_tags` (which would log a misleading "Unsupported
            # format" *error* for a file the backup never actually captured).
            stats.skipped_unsupported += 1
            continue

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

    # Tolerate a wrong-shape payload (a bare list of tracks, or junk) without an
    # AttributeError: a dict uses its "tracks", a list IS the track list, and
    # anything else yields no tracks. The per-entry loop below skips non-dict
    # rows. (The CLI also pre-validates for a clean error message.)
    if isinstance(analysis_data, dict):
        tracks = analysis_data.get("tracks", [])
    elif isinstance(analysis_data, list):
        tracks = analysis_data
    else:
        tracks = []
    if not isinstance(tracks, list):
        tracks = []
    stats = ApplyStats(total=len(tracks))

    for i, track in enumerate(tracks):
        cancellation.check()

        # A hand-edited / wrong-shape analysis.json can carry non-dict elements
        # (a bare string/number) or dicts missing `path`. Skip them rather than
        # crashing the whole apply with an AttributeError/KeyError — matches the
        # guard the `export` command already applies to the same files.
        if not isinstance(track, dict):
            stats.errors.append(f"Skipped malformed track entry (not an object): {track!r}")
            continue
        track_path = track.get("path")
        if not track_path:
            stats.errors.append("Skipped track entry with no 'path'")
            continue

        report_progress(on_progress, i + 1, stats.total, Path(track_path).name)

        # Tolerate NFC/NFD normalization drift between the stored path and the
        # on-disk name (see resolve_existing_path) — otherwise accented
        # filenames arriving NFD-normalized are wrongly skipped as "Not found"
        # and never get tagged.
        filepath = resolve_existing_path(track_path)
        if filepath is None:
            stats.errors.append(f"Not found: {Path(track_path)}")
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
        # `subgenre_to_write` is what lands in the SUBGENRE frame (TIT1 /
        # CONTENTGROUP / ©grp); `genre_to_write` is the MAIN genre frame (TCON /
        # GENRE / ©gen). When `write_subgenre_as_main_genre` is False the user
        # wants the parent family in the sortable main-genre field (e.g. "House")
        # while the precise subgenre is still recorded in its own frame — so the
        # subgenre is never lost, it just doesn't become the Rekordbox sort key.
        subgenre_to_write = ""
        if apply_subgenre:
            subgenre_to_write = subgenre
            if config.write_subgenre_as_main_genre:
                genre_to_write = subgenre
            else:
                genre_to_write = parent_genre or subgenre
            stats.genre_applied += 1
        elif apply_parent_only:
            genre_to_write = parent_genre
            stats.genre_applied_parent_only += 1
        else:
            genre_to_write = ""
            stats.genre_skipped_low_confidence += 1

        # The genre write is gated by BOTH the confidence thresholds above AND
        # the per-field `write_genre` toggle. Every other field has its own
        # independent toggle (checked in _apply_mp3/_apply_flac) — none are
        # gated by genre confidence.
        apply_genre = (apply_subgenre or apply_parent_only) and config.write_genre

        if dry_run:
            continue

        ext = filepath.suffix.lower()
        try:
            if ext == ".mp3":
                _apply_mp3(filepath, ml, apply_genre, genre_to_write, config, subgenre_to_write)
                stats.other_tags_applied += 1
            elif ext == ".flac":
                _apply_flac(filepath, ml, apply_genre, genre_to_write, config, subgenre_to_write)
                stats.other_tags_applied += 1
            elif ext in (".aiff", ".aif", ".wav"):
                # AIFF/WAV carry the same ID3v2 frames as MP3 and the analyzer
                # decodes them — tag them instead of erroring 'unsupported'.
                _apply_aiff_wav(filepath, ml, apply_genre, genre_to_write, config, subgenre_to_write)
                stats.other_tags_applied += 1
            elif ext == ".m4a":
                _apply_m4a(filepath, ml, apply_genre, genre_to_write, config, subgenre_to_write)
                stats.other_tags_applied += 1
            else:
                # Genuinely tag-less formats (ogg/aac/wma) have no apply writer.
                stats.errors.append(f"{filepath.name}: unsupported format {ext}")
        except Exception as e:  # noqa: BLE001
            stats.errors.append(f"{filepath.name}: {e}")

    return stats


# Maps the derived-field tag name → the TaggingConfig toggle that gates it.
# BPM/Key are handled separately (they have their own write_bpm/write_key
# plus the skip-rich-write semantics). GENRE is gated upstream by apply_genre.
_FIELD_WRITE_TOGGLES = {
    "ENERGY": "write_energy",
    "MOOD": "write_mood",
    "TIMESLOT": "write_timeslot",
    "DIRECTION": "write_direction",
    "VOCAL": "write_vocal",
}


def _resolve_vocal(ml: dict[str, Any], config: TaggingConfig) -> str | None:
    """The VOCAL value to write.

    Prefer re-deriving from the raw `ml_vocal_score` using the config's
    (tunable) thresholds, so changing the cutoff re-tags correctly WITHOUT a
    re-analyze. Falls back to the stored `ml_vocal` label for older analyses
    that predate the raw score.
    """
    from vibechek.analyzer import _classify_vocal  # noqa: PLC0415
    score = ml.get("ml_vocal_score")
    if isinstance(score, (int, float)):
        return _classify_vocal(
            float(score),
            instrumental_max=config.vocal_instrumental_max,
            full_min=config.vocal_full_min,
        )
    val = ml.get("ml_vocal")
    return str(val) if val is not None else None


def _derived_field_value(tag_name: str, ml: dict[str, Any], config: TaggingConfig):
    """Value to write for a derived (non-genre) field, honoring its toggle."""
    toggle = _FIELD_WRITE_TOGGLES[tag_name]
    if not getattr(config, toggle):
        return None
    if tag_name == "VOCAL":
        return _resolve_vocal(ml, config)
    val = ml.get(f"ml_{tag_name.lower()}")
    return val if val is not None else None


def _apply_id3_frames(
    id3: ID3,
    ml: dict[str, Any],
    apply_genre: bool,
    genre_value: str,
    config: TaggingConfig,
    subgenre_value: str = "",
) -> None:
    """Write ML-derived frames into an ID3 block.

    Shared by MP3 *and* AIFF/WAV — all three embed the same ID3v2 frames, so
    the frame construction (TCON/TIT1/TBPM/TKEY/TXXX) lives in one place and
    can't drift between formats. Honors `preserve_rekordbox_frames` by
    snapshotting GEOB/PRIV before the writes and re-adding any that a write
    didn't already replace.

    `genre_value` is the MAIN genre (TCON); `subgenre_value` is the SUBGENRE
    (TIT1). They differ only when `write_subgenre_as_main_genre` is off (parent
    in TCON, precise subgenre still preserved in TIT1).
    """
    enc = config.id3_text_encoding

    # Snapshot binary frames to restore after destructive writes
    preserved: list[tuple[str, Any]] = []
    if config.preserve_rekordbox_frames:
        preserved = [
            (k, id3[k])
            for k in list(id3.keys())
            if k.startswith("GEOB:") or k.startswith("PRIV:")
        ]

    if apply_genre:
        id3.delall("TCON")
        id3.add(TCON(encoding=enc, text=[genre_value]))
        # Write TIT1 (subgenre frame) whenever we have a genuine subgenre to
        # record. Under stage-2 parent-only fallback subgenre_value is "" so
        # TIT1 is left untouched — we don't mislabel an unclear track.
        if subgenre_value:
            id3.delall("TIT1")
            id3.add(TIT1(encoding=enc, text=[subgenre_value]))

    if config.write_bpm and ml.get("ml_bpm"):
        id3.delall("TBPM")
        id3.add(TBPM(encoding=enc, text=[str(int(round(ml["ml_bpm"])))]))
    if config.write_key and ml.get("ml_key"):
        id3.delall("TKEY")
        id3.add(TKEY(encoding=enc, text=[ml["ml_key"]]))

    for tag_name in ("ENERGY", "MOOD", "TIMESLOT", "DIRECTION", "VOCAL"):
        val = _derived_field_value(tag_name, ml, config)
        if val is not None:
            id3.delall(f"TXXX:{tag_name}")
            id3.add(TXXX(encoding=enc, desc=tag_name, text=[str(val)]))

    for key, frame in preserved:
        if key not in id3:
            id3.add(frame)


def _apply_mp3(
    filepath: Path,
    ml: dict[str, Any],
    apply_genre: bool,
    genre_value: str,
    config: TaggingConfig,
    subgenre_value: str = "",
) -> None:
    audio = MP3(filepath)
    if audio.tags is None:
        audio.add_tags()
    _apply_id3_frames(audio.tags, ml, apply_genre, genre_value, config, subgenre_value)
    audio.save()


def _apply_aiff_wav(
    filepath: Path,
    ml: dict[str, Any],
    apply_genre: bool,
    genre_value: str,
    config: TaggingConfig,
    subgenre_value: str = "",
) -> None:
    """Apply ML tags to AIFF/WAV — both carry the same ID3v2 frames as MP3.

    AIFF is the standard lossless format for Rekordbox/Serato, so a confident
    analysis must be able to tag it (it used to error 'unsupported format').
    """
    ext = filepath.suffix.lower()
    audio = AIFF(filepath) if ext in (".aiff", ".aif") else WAVE(filepath)
    if audio.tags is None:
        audio.add_tags()
    _apply_id3_frames(audio.tags, ml, apply_genre, genre_value, config, subgenre_value)
    audio.save()


def _apply_m4a(
    filepath: Path,
    ml: dict[str, Any],
    apply_genre: bool,
    genre_value: str,
    config: TaggingConfig,
    subgenre_value: str = "",
) -> None:
    """Apply ML tags to an M4A/MP4 container.

    Genre → ``©gen``, subgenre → ``©grp`` (grouping, the M4A analog of TIT1),
    BPM → ``tmpo`` (int atom), and each derived field → a freeform
    ``----:com.apple.iTunes:<NAME>`` atom (the iTunes convention DJ tools read).
    """
    audio = MP4(filepath)
    if audio.tags is None:
        audio.add_tags()

    if apply_genre:
        audio["\xa9gen"] = [genre_value]
        if subgenre_value:
            audio["\xa9grp"] = [subgenre_value]

    if config.write_bpm and ml.get("ml_bpm"):
        audio["tmpo"] = [int(round(ml["ml_bpm"]))]
    if config.write_key and ml.get("ml_key"):
        audio["----:com.apple.iTunes:initialkey"] = [
            MP4FreeForm(str(ml["ml_key"]).encode("utf-8"))
        ]

    for tag_name in ("ENERGY", "MOOD", "TIMESLOT", "DIRECTION", "VOCAL"):
        val = _derived_field_value(tag_name, ml, config)
        if val is not None:
            audio[f"----:com.apple.iTunes:{tag_name}"] = [
                MP4FreeForm(str(val).encode("utf-8"))
            ]

    audio.save()


def _apply_flac(
    filepath: Path,
    ml: dict[str, Any],
    apply_genre: bool,
    genre_value: str,
    config: TaggingConfig,
    subgenre_value: str = "",
) -> None:
    audio = FLAC(filepath)
    if apply_genre:
        audio["GENRE"] = genre_value
        # CONTENTGROUP is the FLAC analog of MP3's TIT1 (subgenre) — populate it
        # whenever we have a genuine subgenre. Stage-2 parent-only fallback
        # passes subgenre_value="" so it does NOT pollute CONTENTGROUP.
        if subgenre_value:
            audio["CONTENTGROUP"] = subgenre_value

    if config.write_bpm and ml.get("ml_bpm"):
        audio["BPM"] = str(int(round(ml["ml_bpm"])))
    if config.write_key and ml.get("ml_key"):
        audio["INITIALKEY"] = ml["ml_key"]

    for tag_name in ("ENERGY", "MOOD", "TIMESLOT", "DIRECTION", "VOCAL"):
        val = _derived_field_value(tag_name, ml, config)
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
    config: TaggingConfig | None = None,
    allow_filename_only: bool = False,
) -> RemapRestoreStats:
    """Restore a tag backup with automatic remap for moved libraries.

    `restore_tags` keys exclusively on the original absolute path
    captured at backup time. If the user backed up `D:\\Music\\foo.mp3` then
    renamed the drive to `E:\\` (or moved the whole library), restore skips
    every file. This function walks `library_root`, then for each backup entry
    attempts three strategies in order:

    1. **Exact path** — the backup's original path exists on disk verbatim.
    2. **Filename + size** — a file under `library_root` has the same name
       AND the same byte size as the backup entry. Backups now record `_size`,
       so this strategy is the live disambiguator for moved-but-unmodified
       libraries (drive rename, folder relocation).
    3. **Filename alone** — a single file under `library_root` has the same
       name (no other file in the library shares the name). This is OFF by
       default (`allow_filename_only=False`): for common DJ filenames
       (`track01.mp3`, `Untitled.mp3`) a basename-only match can write one
       track's cue frames onto a *different* same-named track. The caller must
       explicitly opt in once they understand that risk.

    `config` is forwarded to `write_all_tags` so MP3/AIFF/WAV restores honor the
    user's chosen ID3 text-frame encoding (the RPC layer passes `config=cfg`).

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

        if tags.get("_unsupported"):
            # No tags were captured for this format — nothing to remap/restore.
            match_record["strategy"] = "unsupported"
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

            # Strategy 3: filename alone — only safe when unique in the library,
            # and even then only when the caller opted in. A unique basename is
            # NOT proof it's the same track (two different `Untitled.mp3` files
            # in different libraries), so writing cue frames on a basename match
            # alone can corrupt a different track. Gated behind
            # `allow_filename_only` so it can't fire silently.
            if target is None and allow_filename_only:
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

        success, error = write_all_tags(target, tags, config)
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
