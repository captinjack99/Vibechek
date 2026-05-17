"""Shared helpers — filesystem walking, name sanitization, external tool discovery."""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path
from typing import Callable, Iterable

log = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS: frozenset[str] = frozenset({
    ".mp3", ".flac", ".m4a", ".wav", ".aiff", ".aif", ".ogg", ".aac", ".wma",
})

INVALID_FOLDER_CHARS = '<>:"/\\|?*'

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
    for p in iterator:
        if p.is_file() and p.suffix.lower() in exts:
            found.add(p)

    return sorted(found, key=str)


def sanitize_folder_name(name: str | None) -> str:
    """Strip characters that are invalid on Windows/macOS/Linux folder names."""
    if not name:
        return "Unknown"
    for ch in INVALID_FOLDER_CHARS:
        name = name.replace(ch, "_")
    return name.strip() or "Unknown"


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
