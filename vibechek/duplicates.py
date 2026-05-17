"""Duplicate detection — exact (MD5) and audio (Chromaprint).

MD5 catches byte-identical copies. Chromaprint catches re-encoded duplicates
(MP3 vs FLAC of the same master, different bitrates, etc.) by comparing
acoustic fingerprints.

Source: ports of `legacy/find_duplicates.py` and `legacy/move_safe_duplicates.py`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import subprocess
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path

from mutagen import File as MutagenFile

from vibechek.config import DuplicateConfig
from vibechek.utils import (
    ProgressCallback,
    find_audio_files,
    find_fpcalc,
    report_progress,
)

log = logging.getLogger(__name__)

# Preference order when deciding which file to keep in a duplicate group.
# Lossless first, then larger files, then shorter paths.
_KEEPER_FORMAT_PRIORITY = {
    ".flac": 1, ".wav": 2, ".aiff": 3, ".aif": 3, ".m4a": 4,
    ".mp3": 5, ".ogg": 6, ".aac": 7, ".wma": 8,
}


class DuplicateAction(str, Enum):
    REPORT = "report"
    MOVE = "move"
    TRASH = "trash"


@dataclass
class FileInfo:
    path: str
    filename: str
    size_bytes: int
    size_mb: float
    file_hash: str | None = None
    audio_fingerprint: str | None = None
    # Extra metadata used by the auto-keeper rules in the GUI.
    # Best-effort: all may be None if the file is unreadable.
    codec: str | None = None           # "flac" | "mp3" | "wav" | ...
    bitrate_kbps: int | None = None    # average bitrate (lossless: computed)
    duration_s: float | None = None
    modified_time: float | None = None  # epoch seconds


@dataclass
class DuplicateGroup:
    method: str  # "md5" | "chromaprint"
    key: str  # the hash or fingerprint string
    keeper: FileInfo
    duplicates: list[FileInfo]
    recoverable_mb: float

    def to_dict(self) -> dict:
        return {
            "method": self.method,
            "key": self.key,
            "keep": asdict(self.keeper),
            "duplicates": [asdict(d) for d in self.duplicates],
            "recoverable_mb": self.recoverable_mb,
        }


@dataclass
class DuplicateReport:
    total_files: int = 0
    exact_groups: list[DuplicateGroup] = field(default_factory=list)
    audio_groups: list[DuplicateGroup] = field(default_factory=list)

    @property
    def total_duplicate_files(self) -> int:
        return sum(len(g.duplicates) for g in self.exact_groups + self.audio_groups)

    @property
    def recoverable_mb(self) -> float:
        return sum(g.recoverable_mb for g in self.exact_groups + self.audio_groups)

    def to_dict(self) -> dict:
        return {
            "summary": {
                "total_files": self.total_files,
                "exact_duplicate_groups": len(self.exact_groups),
                "exact_duplicate_files": sum(len(g.duplicates) for g in self.exact_groups),
                "audio_duplicate_groups": len(self.audio_groups),
                "audio_duplicate_files": sum(len(g.duplicates) for g in self.audio_groups),
                "total_duplicates": self.total_duplicate_files,
                "space_recoverable_mb": round(self.recoverable_mb, 2),
            },
            "exact_duplicates": [g.to_dict() for g in self.exact_groups],
            "audio_duplicates": [g.to_dict() for g in self.audio_groups],
        }


# ---------------------------------------------------------------------------
# Hashing primitives
# ---------------------------------------------------------------------------


def file_md5(filepath: Path, chunk_size: int = 65536) -> str | None:
    """Return the MD5 hex digest of `filepath`, or None on read error."""
    hasher = hashlib.md5()
    try:
        with open(filepath, "rb") as f:
            while chunk := f.read(chunk_size):
                hasher.update(chunk)
        return hasher.hexdigest()
    except OSError as e:
        log.warning("Could not hash %s: %s", filepath, e)
        return None


def audio_fingerprint(filepath: Path, fpcalc_cmd: str, duration: int = 120) -> str | None:
    """Generate a Chromaprint fingerprint hash, or None if unavailable / failed."""
    try:
        result = subprocess.run(
            [fpcalc_cmd, "-raw", "-length", str(duration), str(filepath)],
            capture_output=True,
            text=True,
            timeout=max(duration, 60),
        )
        if result.returncode != 0:
            return None
        for line in result.stdout.strip().splitlines():
            if line.startswith("FINGERPRINT="):
                fp = line.split("=", 1)[1]
                # Hash so fingerprints are comparable as strings of fixed size
                return hashlib.md5(fp.encode()).hexdigest()
    except (subprocess.SubprocessError, OSError) as e:
        log.warning("Fingerprint failed for %s: %s", filepath, e)
    return None


def _file_info(filepath: Path) -> FileInfo:
    stat = filepath.stat()
    info = FileInfo(
        path=str(filepath),
        filename=filepath.name,
        size_bytes=stat.st_size,
        size_mb=round(stat.st_size / (1024 * 1024), 2),
        codec=filepath.suffix.lower().lstrip("."),
        modified_time=stat.st_mtime,
    )

    # Best-effort bitrate / duration via mutagen. Catch broadly — corrupt
    # files shouldn't break the whole dedupe scan.
    try:
        audio = MutagenFile(str(filepath))
        if audio and getattr(audio, "info", None):
            duration = float(getattr(audio.info, "length", 0) or 0)
            if duration > 0:
                info.duration_s = round(duration, 1)
            bitrate = getattr(audio.info, "bitrate", None)
            if bitrate:
                info.bitrate_kbps = int(bitrate // 1000)
            elif duration > 0:
                # Lossless formats don't expose .bitrate; derive from file size
                # bytes/sec → bits/sec → kbps
                info.bitrate_kbps = int((stat.st_size * 8) / duration / 1000)
    except Exception as e:  # noqa: BLE001
        log.debug("metadata probe failed for %s: %s", filepath, e)

    return info


def choose_keeper(files: list[FileInfo]) -> tuple[FileInfo, list[FileInfo]]:
    """Pick which file to keep from a group; return (keeper, duplicates)."""
    def score(f: FileInfo) -> tuple[int, int, int]:
        ext = Path(f.path).suffix.lower()
        return (
            _KEEPER_FORMAT_PRIORITY.get(ext, 99),  # lossless first
            -f.size_bytes,                         # larger is better
            len(f.path),                           # shorter path is better
        )

    ordered = sorted(files, key=score)
    return ordered[0], ordered[1:]


# ---------------------------------------------------------------------------
# Main scan
# ---------------------------------------------------------------------------


def find_duplicates(
    library_path: Path,
    config: DuplicateConfig,
    on_progress: ProgressCallback | None = None,
) -> DuplicateReport:
    """Scan `library_path` and return all detected duplicate groups."""
    audio_files = find_audio_files(library_path)
    report = DuplicateReport(total_files=len(audio_files))

    # ---------- Phase 1: hash everything (cheap; rules out the common case) ----------
    file_infos: dict[str, FileInfo] = {}
    by_hash: dict[str, list[FileInfo]] = defaultdict(list)

    if config.use_md5:
        for i, fp in enumerate(audio_files):
            report_progress(on_progress, i + 1, len(audio_files), f"hash {fp.name}")
            info = _file_info(fp)
            file_infos[str(fp)] = info
            h = file_md5(fp)
            if h:
                info.file_hash = h
                by_hash[h].append(info)

        for h, files in by_hash.items():
            if len(files) > 1:
                keeper, dupes = choose_keeper(files)
                report.exact_groups.append(DuplicateGroup(
                    method="md5",
                    key=h,
                    keeper=keeper,
                    duplicates=dupes,
                    recoverable_mb=round(sum(d.size_mb for d in dupes), 2),
                ))

    # ---------- Phase 2: chromaprint on what's left ----------
    if config.use_chromaprint:
        fpcalc = find_fpcalc()
        if not fpcalc:
            log.warning("fpcalc not found; skipping audio fingerprinting")
        else:
            exact_dupe_paths = {
                d.path for g in report.exact_groups for d in g.duplicates
            }
            remaining = [fp for fp in audio_files if str(fp) not in exact_dupe_paths]
            by_fp: dict[str, list[FileInfo]] = defaultdict(list)

            for i, fp in enumerate(remaining):
                report_progress(on_progress, i + 1, len(remaining), f"fingerprint {fp.name}")
                info = file_infos.get(str(fp)) or _file_info(fp)
                file_infos[str(fp)] = info
                fp_hash = audio_fingerprint(fp, fpcalc)
                if fp_hash:
                    info.audio_fingerprint = fp_hash
                    by_fp[fp_hash].append(info)

            for fp_hash, files in by_fp.items():
                if len(files) <= 1:
                    continue
                # Require differing file hashes — same fingerprint AND same MD5
                # means it was already caught in phase 1.
                if len({f.file_hash for f in files if f.file_hash}) <= 1:
                    continue
                keeper, dupes = choose_keeper(files)
                report.audio_groups.append(DuplicateGroup(
                    method="chromaprint",
                    key=fp_hash,
                    keeper=keeper,
                    duplicates=dupes,
                    recoverable_mb=round(sum(d.size_mb for d in dupes), 2),
                ))

    return report


# ---------------------------------------------------------------------------
# Acting on the report
# ---------------------------------------------------------------------------


def handle_duplicates(
    report: DuplicateReport,
    config: DuplicateConfig,
    on_progress: ProgressCallback | None = None,
) -> dict[str, int]:
    """Act on `report` per `config.action`.

    Returns a `{moved, deleted, errors}` summary.
    """
    action = DuplicateAction(config.action)
    all_dupes = [
        d
        for g in (*report.exact_groups, *report.audio_groups)
        for d in g.duplicates
    ]
    summary = {"moved": 0, "deleted": 0, "errors": 0}

    if action is DuplicateAction.REPORT:
        return summary

    if action is DuplicateAction.MOVE:
        if not config.review_folder:
            raise ValueError("DuplicateConfig.action='move' requires a review_folder")
        dest_root = Path(config.review_folder)
        dest_root.mkdir(parents=True, exist_ok=True)

        for i, dupe in enumerate(all_dupes):
            report_progress(on_progress, i + 1, len(all_dupes), dupe.filename)
            src = Path(dupe.path)
            if not src.exists():
                summary["errors"] += 1
                continue
            dst = _unique_path(dest_root / src.name)
            try:
                shutil.move(str(src), str(dst))
                summary["moved"] += 1
            except OSError as e:
                log.warning("Move failed for %s: %s", src, e)
                summary["errors"] += 1

    elif action is DuplicateAction.TRASH:
        # Late import — send2trash is optional, only needed for this action
        try:
            from send2trash import send2trash
        except ImportError as e:
            raise RuntimeError(
                "send2trash is required for DuplicateAction.TRASH. "
                "Install with: pip install send2trash"
            ) from e

        for i, dupe in enumerate(all_dupes):
            report_progress(on_progress, i + 1, len(all_dupes), dupe.filename)
            src = Path(dupe.path)
            if not src.exists():
                summary["errors"] += 1
                continue
            try:
                send2trash(str(src))
                summary["deleted"] += 1
            except OSError as e:
                log.warning("Trash failed for %s: %s", src, e)
                summary["errors"] += 1

    return summary


def _unique_path(path: Path) -> Path:
    """Return `path`, or a numbered variant if it already exists."""
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    counter = 1
    while True:
        candidate = path.with_name(f"{stem}_{counter}{suffix}")
        if not candidate.exists():
            return candidate
        counter += 1


def save_report(report: DuplicateReport, output_path: Path) -> None:
    """Write a duplicate report to JSON in the same shape as the legacy tool."""
    Path(output_path).write_text(
        json.dumps(report.to_dict(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


__all__ = [
    "DuplicateAction",
    "FileInfo",
    "DuplicateGroup",
    "DuplicateReport",
    "file_md5",
    "audio_fingerprint",
    "choose_keeper",
    "find_duplicates",
    "handle_duplicates",
    "save_report",
]
