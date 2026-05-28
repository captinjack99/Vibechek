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
    keep: FileInfo  # The "winner" — kept on disk; everything in `duplicates` is removed
    duplicates: list[FileInfo]
    recoverable_mb: float

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class DuplicateSummary:
    """Computed counts for a DuplicateReport. Field names match the JSON wire."""

    total_files: int = 0
    exact_duplicate_groups: int = 0
    exact_duplicate_files: int = 0
    audio_duplicate_groups: int = 0
    audio_duplicate_files: int = 0
    total_duplicates: int = 0
    space_recoverable_mb: float = 0.0


@dataclass
class DuplicateReport:
    """The result of a duplicate scan. Field names match the JSON-RPC wire shape
    so the TS generator and the runtime payload are 1:1."""

    summary: DuplicateSummary = field(default_factory=DuplicateSummary)
    exact_duplicates: list[DuplicateGroup] = field(default_factory=list)
    audio_duplicates: list[DuplicateGroup] = field(default_factory=list)

    def update_summary(self, total_files: int | None = None) -> None:
        """Recompute the summary from current groups. Call after mutating lists.

        `total_files` defaults to whatever the summary already had — useful
        when filtering groups without rescanning the library.
        """
        if total_files is not None:
            self.summary.total_files = total_files
        exact_files = sum(len(g.duplicates) for g in self.exact_duplicates)
        audio_files = sum(len(g.duplicates) for g in self.audio_duplicates)
        self.summary.exact_duplicate_groups = len(self.exact_duplicates)
        self.summary.exact_duplicate_files = exact_files
        self.summary.audio_duplicate_groups = len(self.audio_duplicates)
        self.summary.audio_duplicate_files = audio_files
        self.summary.total_duplicates = exact_files + audio_files
        self.summary.space_recoverable_mb = round(
            sum(g.recoverable_mb for g in self.exact_duplicates + self.audio_duplicates),
            2,
        )

    def to_dict(self) -> dict:
        return asdict(self)


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
    """Generate a Chromaprint fingerprint hash, or None if unavailable / failed.

    Returns an MD5 of the raw fingerprint string — suitable for exact-match
    bucketing (two byte-perfect re-encodings often produce the same hash).
    For similarity-based matching (transcodes / different bitrates), use
    `audio_fingerprint_raw` which returns the underlying integer sequence.
    """
    raw = audio_fingerprint_raw(filepath, fpcalc_cmd, duration=duration)
    if raw is None:
        return None
    # Hash so fingerprints are comparable as strings of fixed size
    return hashlib.md5(",".join(str(x) for x in raw).encode()).hexdigest()


def audio_fingerprint_raw(
    filepath: Path, fpcalc_cmd: str, duration: int = 120,
) -> list[int] | None:
    """Return the raw Chromaprint fingerprint as a list of 32-bit sub-fingerprints.

    Each int encodes a frame of ~125ms of audio. Bit-level similarity (Hamming
    distance) between aligned positions of two such lists yields the standard
    Chromaprint similarity score. None on read/decode failure.
    """
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
                payload = line.split("=", 1)[1].strip()
                if not payload:
                    return None
                try:
                    # fpcalc -raw emits comma-separated signed 32-bit ints.
                    # We coerce to unsigned for clean bit operations later.
                    return [int(x) & 0xFFFFFFFF for x in payload.split(",") if x]
                except ValueError:
                    return None
    except (subprocess.SubprocessError, OSError) as e:
        log.warning("Fingerprint failed for %s: %s", filepath, e)
    return None


def fingerprint_similarity(a: list[int], b: list[int]) -> float:
    """Return Hamming-distance similarity in [0.0, 1.0] for two raw fingerprints.

    Aligns at index 0 and compares the overlapping prefix bit-by-bit. 1.0 means
    identical, 0.0 means every bit differs. This is the standard cheap
    chromaprint similarity check — for longer audio with offsets you'd slide
    one window over the other, but for full-track duplicate detection the
    aligned form catches re-encodes well.
    """
    n = min(len(a), len(b))
    if n == 0:
        return 0.0
    diff_bits = 0
    for x, y in zip(a[:n], b[:n]):
        # XOR gives a 1 bit wherever the two sub-fingerprints differ.
        diff_bits += (x ^ y).bit_count()
    total_bits = n * 32
    return 1.0 - (diff_bits / total_bits)


def _file_info(filepath: Path, read_metadata: bool = True) -> FileInfo:
    """Build a FileInfo for `filepath`.

    When `read_metadata` is False, skip the mutagen probe entirely — only
    path/filename/size/codec/modified_time come from os.stat. This saves
    ~30s on a 12k-track library when the caller doesn't need bitrate/duration
    (e.g. MD5-only dedup that doesn't use rule-based keeper picking).
    """
    stat = filepath.stat()
    info = FileInfo(
        path=str(filepath),
        filename=filepath.name,
        size_bytes=stat.st_size,
        size_mb=round(stat.st_size / (1024 * 1024), 2),
        codec=filepath.suffix.lower().lstrip("."),
        modified_time=stat.st_mtime,
    )

    if not read_metadata:
        return info

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


def _cluster_by_similarity(
    items: list[tuple[FileInfo, list[int]]], threshold: float,
) -> list[list[tuple[FileInfo, list[int]]]]:
    """Greedy single-link clustering on chromaprint similarity.

    For each input, attach it to the first existing cluster whose head it's
    >= threshold similar to; otherwise start a new cluster. Quadratic in the
    bucket size, but buckets are small in practice (see find_duplicates).
    """
    clusters: list[list[tuple[FileInfo, list[int]]]] = []
    for info, raw in items:
        placed = False
        for cluster in clusters:
            head_raw = cluster[0][1]
            if fingerprint_similarity(raw, head_raw) >= threshold:
                cluster.append((info, raw))
                placed = True
                break
        if not placed:
            clusters.append([(info, raw)])
    return clusters


def choose_keeper(files: list[FileInfo]) -> tuple[FileInfo, list[FileInfo]]:
    """Pick which file to keep from a group; return (keeper, duplicates).

    Selection is fully deterministic so repeated runs (and the GUI's preview
    vs. execute) agree. Ordering, in priority:

      1. Zero-byte files are deprioritized HARD. A corrupt 0-byte `.flac`
         would otherwise win on format priority over a healthy 8 MB `.mp3`
         and we'd keep the empty file while deleting the real audio.
      2. Format priority (lossless before lossy).
      3. Larger size (better bitrate / less truncation).
      4. Shorter path (prefer the canonical location over a `/dupes/` copy).
      5. Path string — a final tiebreaker so two otherwise-identical files
         always order the same way regardless of scan order.
    """
    def score(f: FileInfo) -> tuple[int, int, int, int, str]:
        ext = Path(f.path).suffix.lower()
        return (
            0 if f.size_bytes > 0 else 1,          # real files before 0-byte
            _KEEPER_FORMAT_PRIORITY.get(ext, 99),  # lossless first
            -f.size_bytes,                         # larger is better
            len(f.path),                           # shorter path is better
            f.path,                                # deterministic tiebreak
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
    read_metadata: bool = True,
) -> DuplicateReport:
    """Scan `library_path` and return all detected duplicate groups.

    Set `read_metadata=False` to skip per-file mutagen probes — saves significant
    time on large libraries when the caller doesn't need bitrate/duration info
    (e.g. MD5-only dedup with default keeper rules). The format-priority and
    size-based keeper picking still works without metadata.
    """
    audio_files = find_audio_files(library_path)
    report = DuplicateReport(summary=DuplicateSummary(total_files=len(audio_files)))

    # ---------- Phase 1: hash everything (cheap; rules out the common case) ----------
    file_infos: dict[str, FileInfo] = {}
    by_hash: dict[str, list[FileInfo]] = defaultdict(list)

    # Lazy import so this module stays usable as a plain library.
    from vibechek import cancellation

    if config.use_md5:
        for i, fp in enumerate(audio_files):
            # Cancel must actually stop hashing — without this check, a user
            # cancelling mid-dedupe sees no stop and the long-op lock stays
            # held for the entire duration of the would-be cancellation.
            cancellation.check()
            report_progress(on_progress, i + 1, len(audio_files), f"hash {fp.name}")
            info = _file_info(fp, read_metadata=read_metadata)
            file_infos[str(fp)] = info
            h = file_md5(fp)
            if h:
                info.file_hash = h
                by_hash[h].append(info)

        for h, files in by_hash.items():
            if len(files) > 1:
                keeper, dupes = choose_keeper(files)
                report.exact_duplicates.append(DuplicateGroup(
                    method="md5",
                    key=h,
                    keep=keeper,
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
                d.path for g in report.exact_duplicates for d in g.duplicates
            }
            remaining = [fp for fp in audio_files if str(fp) not in exact_dupe_paths]

            # Bucket key: the first sub-fingerprint as hex. Truly-similar tracks
            # almost always share the first ~32 bits (this is the start of the
            # spectral envelope and is extremely stable across re-encodes).
            # Bucketing first keeps the pairwise similarity step O(N) in
            # practice instead of O(N²) over the whole library.
            buckets: dict[str, list[tuple[FileInfo, list[int]]]] = defaultdict(list)

            for i, fp in enumerate(remaining):
                # Same cancellation check as the MD5 loop; chromaprint is
                # slower (it shells out to fpcalc per file) so this matters more.
                cancellation.check()
                report_progress(on_progress, i + 1, len(remaining), f"fingerprint {fp.name}")
                info = file_infos.get(str(fp)) or _file_info(fp, read_metadata=read_metadata)
                file_infos[str(fp)] = info
                raw_fp = audio_fingerprint_raw(fp, fpcalc)
                if raw_fp:
                    # Keep the exact-match hash too so the report dict still has
                    # a `audio_fingerprint` field the GUI / API can show.
                    info.audio_fingerprint = hashlib.md5(
                        ",".join(str(x) for x in raw_fp).encode()
                    ).hexdigest()
                    buckets[f"{raw_fp[0]:08x}"].append((info, raw_fp))

            threshold = max(0.0, min(1.0, config.chromaprint_similarity_threshold))
            # Per-bucket union-find: cluster files whose pairwise similarity
            # crosses the configured threshold. This is the threshold the user
            # sets in Settings — at 1.0 only byte-identical fingerprints group,
            # at 0.85 you get re-encodes / different bitrates of the same master.
            for bucket in buckets.values():
                if len(bucket) <= 1:
                    continue
                clusters = _cluster_by_similarity(bucket, threshold)
                for cluster in clusters:
                    if len(cluster) <= 1:
                        continue
                    files = [info for info, _ in cluster]
                    # Avoid double-reporting: if everyone in the cluster shares
                    # the same MD5 hash, phase 1 already grouped them. Only skip
                    # when MD5 actually ran (config.use_md5 True AND all hashes
                    # known) — otherwise we wrongly drop legitimate similarity
                    # clusters.
                    hashes = [f.file_hash for f in files if f.file_hash]
                    if (
                        config.use_md5
                        and len(hashes) == len(files)
                        and len(set(hashes)) <= 1
                    ):
                        continue
                    keeper, dupes = choose_keeper(files)
                    # Group key: the keeper's fingerprint hash. Stable + unique.
                    group_key = keeper.audio_fingerprint or ""
                    report.audio_duplicates.append(DuplicateGroup(
                        method="chromaprint",
                        key=group_key,
                        keep=keeper,
                        duplicates=dupes,
                        recoverable_mb=round(sum(d.size_mb for d in dupes), 2),
                    ))

    report.update_summary()
    return report


# ---------------------------------------------------------------------------
# Acting on the report
# ---------------------------------------------------------------------------


def handle_duplicates(
    report: DuplicateReport,
    config: DuplicateConfig,
    on_progress: ProgressCallback | None = None,
) -> dict:
    """Act on `report` per `config.action`.

    Returns a `{moved, deleted, errors, error_messages}` summary. `errors` is
    the count; `error_messages` is a list of human-readable strings (one per
    failed file). The list is what the GUI shows in its "errors — see report"
    toast — without it the toast pointed at a report that didn't exist
    (duplicates audit #6).
    """
    # Local import — keeps cancellation a soft dep when duplicates is used as
    # a library outside the sidecar (mirrors the scan path).
    from vibechek import cancellation

    action = DuplicateAction(config.action)
    all_dupes = [
        d
        for g in (*report.exact_duplicates, *report.audio_duplicates)
        for d in g.duplicates
    ]
    error_messages: list[str] = []
    summary: dict = {
        "moved": 0,
        "deleted": 0,
        "errors": 0,
        "error_messages": error_messages,
    }

    if action is DuplicateAction.REPORT:
        return summary

    from vibechek import journal as _journal
    summary["journal_path"] = None

    if action is DuplicateAction.MOVE:
        if not config.review_folder:
            raise ValueError("DuplicateConfig.action='move' requires a review_folder")
        dest_root = Path(config.review_folder)
        dest_root.mkdir(parents=True, exist_ok=True)

        # Move-to-review is fully revertible (move the file back), so we
        # journal each move. Trash is recorded too (below) but only for
        # transparency — send2trash has no reliable restore.
        jrnl = _journal.start_journal(_journal.KIND_DEDUPE_MOVE, root=dest_root)
        try:
            for i, dupe in enumerate(all_dupes):
                # Honor Cancel mid-batch — a user stopping a 12k-file move must
                # actually stop, not just stop seeing progress.
                cancellation.check()
                report_progress(on_progress, i + 1, len(all_dupes), dupe.filename)
                src = Path(dupe.path)
                if not src.exists():
                    summary["errors"] += 1
                    error_messages.append(f"{src}: file not found")
                    continue
                dst = _unique_path(dest_root / src.name)
                try:
                    shutil.move(str(src), str(dst))
                    jrnl.record_move(src, dst)
                    summary["moved"] += 1
                except OSError as e:
                    log.warning("Move failed for %s: %s", src, e)
                    summary["errors"] += 1
                    error_messages.append(f"{src}: move failed — {e}")
        finally:
            jrnl.close()
        if jrnl.entries > 0:
            summary["journal_path"] = str(jrnl.path)

    elif action is DuplicateAction.TRASH:
        # Late import — send2trash is optional, only needed for this action
        try:
            from send2trash import send2trash
        except ImportError as e:
            raise RuntimeError(
                "send2trash is required for DuplicateAction.TRASH. "
                "Install with: pip install send2trash"
            ) from e

        # Trash journal is transparency-only: it records what was trashed so
        # the user has a manifest, but revert_journal can't auto-restore from
        # the OS recycle bin — it reports them for manual restore.
        jrnl = _journal.start_journal(_journal.KIND_DEDUPE_TRASH, root=None)
        try:
            for i, dupe in enumerate(all_dupes):
                cancellation.check()
                report_progress(on_progress, i + 1, len(all_dupes), dupe.filename)
                src = Path(dupe.path)
                if not src.exists():
                    summary["errors"] += 1
                    error_messages.append(f"{src}: file not found")
                    continue
                try:
                    send2trash(str(src))
                    jrnl.record_trash(src)
                    summary["deleted"] += 1
                except OSError as e:
                    log.warning("Trash failed for %s: %s", src, e)
                    summary["errors"] += 1
                    error_messages.append(f"{src}: trash failed — {e}")
        finally:
            jrnl.close()
        if jrnl.entries > 0:
            summary["journal_path"] = str(jrnl.path)

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
    """Write a duplicate report to JSON in the same shape as the legacy tool.

    Atomic: this report drives the GUI's destructive delete/move decisions, so
    a kill mid-write that leaves a truncated file (then read back) could
    under-report dupes. atomic_write_json writes-then-renames.
    """
    from vibechek.io import atomic_write_json
    atomic_write_json(Path(output_path), report.to_dict(), indent=2)


__all__ = [
    "DuplicateAction",
    "FileInfo",
    "DuplicateGroup",
    "DuplicateReport",
    "file_md5",
    "audio_fingerprint",
    "audio_fingerprint_raw",
    "fingerprint_similarity",
    "choose_keeper",
    "find_duplicates",
    "handle_duplicates",
    "save_report",
]
