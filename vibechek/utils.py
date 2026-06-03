"""Shared helpers — filesystem walking, name sanitization, external tool discovery."""

from __future__ import annotations

import logging
import shutil
import subprocess
import unicodedata
from collections.abc import Callable, Iterable
from pathlib import Path

log = logging.getLogger(__name__)


def resolve_existing_path(path: str | Path) -> Path | None:
    """Return an existing Path for `path`, tolerant of Unicode normalization.

    A stored track path can disagree with its on-disk form purely in Unicode
    normalization (NFC vs NFD): macOS (HFS+/APFS) exposes filenames as NFD while
    most other sources produce NFC, so an analysis.json written on one platform
    and applied on another can carry the "wrong" form for an accented filename
    like "Tiësto - Adagio.flac". A raw ``Path(p).exists()`` then returns False
    even though the file is right there, and organize/tag silently skip it as
    "not found". (The separate cp1252-stdin mojibake that bit the packaged GUI
    is fixed at the source in rpc.serve(); this stays as cross-platform
    hardening.)

    Try the path as given, then its NFC and NFD normal forms; return the first
    that exists on disk, or None if the file is genuinely missing. Only the
    fallback forms are tried (cheap) — an exact hit returns immediately and a
    truly-absent file still yields None, so there are no false positives.
    """
    p = Path(path)
    if p.exists():
        return p
    s = str(path)
    for form in ("NFC", "NFD"):
        try:
            candidate = Path(unicodedata.normalize(form, s))
        except (TypeError, ValueError):
            continue
        if candidate != p and candidate.exists():
            return candidate
    return None

SUPPORTED_EXTENSIONS: frozenset[str] = frozenset({
    ".mp3", ".flac", ".m4a", ".wav", ".aiff", ".aif", ".ogg", ".aac", ".wma",
})

INVALID_FOLDER_CHARS = '<>:"/\\|?*'

# Windows reserved device names. A folder named exactly one of these (case-
# insensitively, with or without an extension) is uncreatable on Windows and
# behaves as a device on the legacy DOS namespace — `CON`, `PRN`, `AUX`, `NUL`,
# `COM1`..`COM9`, `LPT1`..`LPT9`. A genre tag of "CON" (or "nul.mp3") would
# otherwise produce a folder the OS refuses to create, failing every move into
# it. We map these to a safe sentinel instead.
_WIN_RESERVED_NAMES = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{i}" for i in range(1, 10)}
    | {f"lpt{i}" for i in range(1, 10)}
)

ProgressCallback = Callable[[int, int, str], None]


def find_audio_files(
    root: Path,
    recursive: bool = True,
    extensions: Iterable[str] = SUPPORTED_EXTENSIONS,
) -> list[Path]:
    """Return every audio file under `root`, sorted by string path for stable ordering.

    Case-insensitive on extension matching, deduplicated across upper/lower variants.
    """
    root = Path(root)
    if not root.exists():
        raise FileNotFoundError(f"Path does not exist: {root}")

    exts = {e.lower() for e in extensions}
    found: set[Path] = set()

    iterator = root.rglob("*") if recursive else root.glob("*")
    # One unreadable entry (broken symlink, a name exceeding Windows MAX_PATH,
    # a permission-denied dir) must not abort the whole scan of a 12k-track
    # library. `is_file()` can raise OSError mid-iteration; skip the bad entry.
    while True:
        try:
            p = next(iterator)
        except StopIteration:
            break
        except OSError as e:
            log.debug("Skipping unreadable entry during scan: %s", e)
            continue
        try:
            if p.is_file() and p.suffix.lower() in exts:
                found.add(p)
        except OSError as e:
            log.debug("Skipping unreadable file %s: %s", p, e)

    return sorted(found, key=str)


def sanitize_folder_name(name: str | None) -> str:
    """Sanitize a string for use as a single folder name.

    Strips characters invalid on Windows/macOS/Linux AND defends against
    path traversal. The genre used to build a destination folder can come
    straight from a file's existing tag (see organizer.route_new_tracks),
    which is fully attacker-controlled on a downloaded track. Without the
    traversal guard, a genre tag of ".." or "../../Windows" would escape the
    library root — `library_root / ".."` writes into the PARENT directory.

    Guarantees the result is a single, safe path segment: no separators, no
    leading/trailing dots, never "." or "..", never a Windows reserved device
    name (CON/PRN/AUX/NUL/COM1-9/LPT1-9).
    """
    if not name:
        return "Unknown"
    for ch in INVALID_FOLDER_CHARS:
        name = name.replace(ch, "_")
    # Collapse any stray separators the loop above already turned into "_"
    # is handled, but a literal forward slash on POSIX is in INVALID_FOLDER_CHARS
    # so it's covered. Now strip leading/trailing dots + whitespace so "..",
    # ".", " ..", "..hidden" etc. can't traverse or create dotfiles. Trailing
    # dots/spaces are also silently dropped by the Windows shell ("foo." → "foo"),
    # so stripping them keeps the on-disk name and our intended name in sync.
    # `strip(" .")` removes any mix of trailing/leading dots and spaces in one
    # pass (so "House. . " → "House"), which the older chained
    # `.strip().strip(".")` missed when dots and spaces were interleaved.
    name = name.strip(" .")
    # After stripping, a name that was purely dots/separators is empty, OR a
    # residual "." / ".." would still be dangerous — reject them outright.
    if not name or name in (".", ".."):
        return "Unknown"
    # Windows reserved device names are uncreatable. Match case-insensitively on
    # the stem before the first dot ("nul.mp3" is just as reserved as "NUL").
    if name.split(".", 1)[0].lower() in _WIN_RESERVED_NAMES:
        return f"_{name}"
    return name


def find_fpcalc() -> str | None:
    """Locate the `fpcalc` (Chromaprint) executable, returning its path or None.

    Tries PATH first, then a few well-known install locations.
    """
    on_path = shutil.which("fpcalc")
    if on_path:
        return on_path

    candidates = [
        "/usr/bin/fpcalc",
        "/usr/local/bin/fpcalc",
        "/opt/homebrew/bin/fpcalc",
        r"C:\Program Files\Chromaprint\fpcalc.exe",
    ]
    for cmd in candidates:
        try:
            result = subprocess.run([cmd, "-version"], capture_output=True, timeout=5)
            if result.returncode == 0:
                return cmd
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
    return None


def report_progress(
    on_progress: ProgressCallback | None,
    current: int,
    total: int,
    message: str = "",
) -> None:
    """Invoke a progress callback if one is provided, swallowing exceptions.

    Callbacks come from UIs that may go away mid-operation; we never want a
    flaky callback to crash analysis.
    """
    if on_progress is None:
        return
    try:
        on_progress(current, total, message)
    except Exception:
        log.exception("Progress callback raised; ignoring")
