"""Filename-based metadata extraction.

DJ filenames frequently embed BPM, key, mix type, and artist/title. We mine
these as a fallback / cross-check signal — never as the authoritative source.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

_BPM_RE = re.compile(r"[\s_\-](\d{2,3})(?:\s*bpm)?(?:\.[a-z0-9]+)?$", re.IGNORECASE)
_CAMELOT_RE = re.compile(r"[\s_\-\[\(]([1-9]|1[0-2])[AB][\s_\-\]\)\.]", re.IGNORECASE)
_LEAD_TRACK_NUM_RE = re.compile(r"^[\d]+[\s\-\.]+")
_MIX_TAIL_RE = re.compile(r"\s*[\(\[].*?(?:mix|edit|remix|version).*?[\)\]]", re.IGNORECASE)
_TRAILING_NUM_RE = re.compile(r"\s+\d{2,3}$")

_MIX_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"[\(\[]?\s*original\s*mix\s*[\)\]]?", re.IGNORECASE), "Original Mix"),
    (re.compile(r"[\(\[]?\s*extended\s*mix\s*[\)\]]?", re.IGNORECASE), "Extended Mix"),
    (re.compile(r"[\(\[]?\s*radio\s*edit\s*[\)\]]?", re.IGNORECASE), "Radio Edit"),
    (re.compile(r"[\(\[]?\s*club\s*mix\s*[\)\]]?", re.IGNORECASE), "Club Mix"),
    (re.compile(r"[\(\[]?\s*dub\s*mix\s*[\)\]]?", re.IGNORECASE), "Dub Mix"),
    (re.compile(r"[\(\[]?\s*vip\s*mix\s*[\)\]]?", re.IGNORECASE), "VIP Mix"),
    (re.compile(r"\s*-\s*[^-]+\s+remix", re.IGNORECASE), "Remix"),
    (re.compile(r"[\(\[]\s*[^)\]]+\s+remix\s*[\)\]]", re.IGNORECASE), "Remix"),
]


def extract_from_filename(filename: str) -> dict[str, Any]:
    """Best-effort parse of BPM, key, mix type, artist, title from a filename."""
    info: dict[str, Any] = {
        "filename_bpm": None,
        "filename_key": None,
        "filename_mix": None,
        "filename_artist": None,
        "filename_title": None,
    }

    if m := _BPM_RE.search(filename):
        bpm = int(m.group(1))
        if 60 <= bpm <= 200:
            info["filename_bpm"] = bpm

    if m := _CAMELOT_RE.search(filename):
        info["filename_key"] = m.group(0).strip(" _-[]().").upper()

    for pattern, mix_type in _MIX_PATTERNS:
        if pattern.search(filename):
            info["filename_mix"] = mix_type
            break

    stem = Path(filename).stem
    cleaned = _LEAD_TRACK_NUM_RE.sub("", stem)

    if " - " in cleaned:
        artist, _, title = cleaned.partition(" - ")
        info["filename_artist"] = artist.strip() or None
        title = _MIX_TAIL_RE.sub("", title)
        title = _TRAILING_NUM_RE.sub("", title)
        info["filename_title"] = title.strip() or None

    return info


__all__ = ["extract_from_filename"]
