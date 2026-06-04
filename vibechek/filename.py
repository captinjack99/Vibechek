"""Filename-based metadata extraction.

DJ filenames frequently embed BPM, key, mix type, and artist/title. We mine
these as a fallback / cross-check signal — never as the authoritative source.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

# An *explicit* BPM token (a number immediately followed by "bpm") is
# authoritative — "128bpm", "128 BPM", "128_bpm" all parse. The full 60-200
# band applies because the "bpm" suffix removes the ambiguity.
_BPM_EXPLICIT_RE = re.compile(r"(?<![\d.])(\d{2,3})\s*bpm\b", re.IGNORECASE)

# A *bare* trailing number on the stem: "...Title 128". Ambiguous on its own
# (could be a track number or a year fragment), so the caller corroborates it
# against the preceding separator before adopting it — see `_extract_bpm`.
_BPM_BARE_RE = re.compile(r"(?P<sep>\s+|[_\-])(?P<bpm>\d{2,3})$")
_CAMELOT_RE = re.compile(r"[\s_\-\[\(]([1-9]|1[0-2])[AB](?=[\s_\-\]\)\.]|$)", re.IGNORECASE)
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


# --- Version / edition detection (for variant-aware dedupe) ---------------
#
# A "version" identity has TWO axes that both matter to a DJ:
#   * remix identity — Original vs "<Artist> Remix" (a remix is a distinct work)
#   * length/edit    — Extended vs Radio Edit vs standard (different set tools)
# version_key() combines them as "<remix>|<length>" so two files are the SAME
# version (true duplicate candidates, differing only by format/bitrate) only
# when BOTH axes match. "Original Mix" and a bare title collapse to the same
# standard length. This is what lets dedupe keep an Extended + a Radio Edit, or
# a FLAC-original + an MP3-extended, while still collapsing a FLAC + MP3 of the
# exact same version.
_REMIX_PARENS_RE = re.compile(
    r"[\(\[]\s*(?P<who>[^)\]]+?)\s+(?:remix|rmx|bootleg|flip|refix|rework)\s*[\)\]]",
    re.IGNORECASE,
)
_REMIX_DASH_RE = re.compile(
    r"-\s*(?P<who>[^-\(\[\]]+?)\s+(?:remix|rmx|bootleg|flip|refix|rework)\b",
    re.IGNORECASE,
)
_LENGTH_TYPES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bextended\b", re.IGNORECASE), "extended"),
    (re.compile(r"\bradio\b", re.IGNORECASE), "radio"),
    (re.compile(r"\bclub\s*mix\b", re.IGNORECASE), "club"),
    (re.compile(r"\bdub\b", re.IGNORECASE), "dub"),
    (re.compile(r"\bvip\b", re.IGNORECASE), "vip"),
    (re.compile(r"\binstrumental\b", re.IGNORECASE), "instrumental"),
    (re.compile(r"\bac[ae]pella\b", re.IGNORECASE), "acapella"),
]


def _norm_remixer(who: str) -> str:
    """Normalize a remixer name to a stable comparison token."""
    who = re.sub(r"\b(feat\.?|ft\.?|presents|pres\.?)\b.*", "", who, flags=re.IGNORECASE)
    return re.sub(r"[^a-z0-9]+", "", who.lower())


def version_key(text: str) -> str:
    """Canonical version identity ``"<remix>|<length>"`` for an title/filename.

    Same key => same version (only format/bitrate may differ => true duplicate).
    Different key => a distinct version a DJ may legitimately keep. Pure.
    """
    t = text or ""
    remix = ""
    m = _REMIX_PARENS_RE.search(t) or _REMIX_DASH_RE.search(t)
    if m:
        norm = _norm_remixer(m.group("who"))
        if norm:
            remix = f"remix:{norm}"
    length = ""
    for pat, tok in _LENGTH_TYPES:
        if pat.search(t):
            length = tok
            break
    return f"{remix}|{length}"


def _extract_bpm(stem: str) -> int | None:
    """Best-effort BPM from a filename stem (extension already stripped).

    Strategy, in priority order:
      1. An explicit ``<n>bpm`` token anywhere in the stem is authoritative —
         the "bpm" suffix removes all ambiguity, so we accept the full 60-200
         band (e.g. "Artist - Title - 128bpm").
      2. A *bare* trailing number is only adopted when it is NOT introduced by
         a " - " field separator. A dash-delimited trailing number is almost
         always a track/catalog number ("Artist - Title - 128") or part of a
         release tag, not a tempo, so we drop it to avoid a confidently-wrong
         `filename_bpm`. A space- or underscore-joined number ("Artist - Title
         128", "track_128") is treated as a plausible tempo.

    Returns the BPM as an int when found and inside the [60, 200] band, else
    None. Pure and side-effect-free for unit testing.
    """
    if m := _BPM_EXPLICIT_RE.search(stem):
        bpm = int(m.group(1))
        if 60 <= bpm <= 200:
            return bpm

    if m := _BPM_BARE_RE.search(stem):
        sep = m.group("sep")
        before = stem[: m.start("sep")].rstrip()
        # Reject a " - " delimited trailing number: that's a track/catalog
        # field, not a tempo. Whitespace/underscore joins are accepted.
        dash_delimited = sep.strip() == "-" or before.endswith("-")
        if not dash_delimited:
            bpm = int(m.group("bpm"))
            if 60 <= bpm <= 200:
                return bpm

    return None


def extract_from_filename(filename: str) -> dict[str, Any]:
    """Best-effort parse of BPM, key, mix type, artist, title from a filename."""
    info: dict[str, Any] = {
        "filename_bpm": None,
        "filename_key": None,
        "filename_mix": None,
        "filename_artist": None,
        "filename_title": None,
    }

    info["filename_bpm"] = _extract_bpm(Path(filename).stem)

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


__all__ = ["extract_from_filename", "version_key"]
