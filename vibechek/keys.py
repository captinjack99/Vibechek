"""Musical key ↔ Camelot wheel conversion.

The Camelot wheel groups keys for harmonic mixing. Keys at adjacent positions
or in the same number (across A/B = minor/major) blend well. Numbers 1-12,
letter A (minor) or B (major). Example: 8A = A minor, 8B = C major.
"""

from __future__ import annotations

import re

KEY_TO_CAMELOT: dict[str, str] = {
    # Majors
    "C major": "8B", "G major": "9B", "D major": "10B", "A major": "11B",
    "E major": "12B", "B major": "1B",
    "F# major": "2B", "Gb major": "2B",
    "C# major": "3B", "Db major": "3B",
    "G# major": "4B", "Ab major": "4B",
    "D# major": "5B", "Eb major": "5B",
    "A# major": "6B", "Bb major": "6B",
    "F major": "7B",
    # Minors
    "A minor": "8A", "E minor": "9A", "B minor": "10A",
    "F# minor": "11A", "Gb minor": "11A",
    "C# minor": "12A", "Db minor": "12A",
    "G# minor": "1A", "Ab minor": "1A",
    "D# minor": "2A", "Eb minor": "2A",
    "A# minor": "3A", "Bb minor": "3A",
    "F minor": "4A", "C minor": "5A", "G minor": "6A", "D minor": "7A",
}

KEY_SHORTHAND: dict[str, str] = {
    "C": "C major", "Cm": "C minor",
    "C#": "C# major", "C#m": "C# minor",
    "Db": "Db major", "Dbm": "Db minor",
    "D": "D major", "Dm": "D minor",
    "D#": "D# major", "D#m": "D# minor",
    "Eb": "Eb major", "Ebm": "Eb minor",
    "E": "E major", "Em": "E minor",
    "F": "F major", "Fm": "F minor",
    "F#": "F# major", "F#m": "F# minor",
    "Gb": "Gb major", "Gbm": "Gb minor",
    "G": "G major", "Gm": "G minor",
    "G#": "G# major", "G#m": "G# minor",
    "Ab": "Ab major", "Abm": "Ab minor",
    "A": "A major", "Am": "A minor",
    "A#": "A# major", "A#m": "A# minor",
    "Bb": "Bb major", "Bbm": "Bb minor",
    "B": "B major", "Bm": "B minor",
}

_CAMELOT_RE = re.compile(r"^([1-9]|1[0-2])([AB])$", re.IGNORECASE)
_KEY_PARSE_RE = re.compile(r"^([A-Ga-g][#b]?)\s*(maj|min|major|minor|m)?", re.IGNORECASE)


def key_to_camelot(key_str: str | None) -> str | None:
    """Normalize any key representation to Camelot (e.g. '8A', '11B').

    Accepts already-Camelot input, full names ('C major'), shorthand ('Cm'),
    and free-form like 'F# min'. Returns None for inputs that don't parse.
    """
    if not key_str:
        return None
    s = str(key_str).strip()

    if m := _CAMELOT_RE.match(s):
        return f"{m.group(1)}{m.group(2).upper()}"

    if s in KEY_TO_CAMELOT:
        return KEY_TO_CAMELOT[s]

    if s in KEY_SHORTHAND:
        return KEY_TO_CAMELOT.get(KEY_SHORTHAND[s])

    if m := _KEY_PARSE_RE.match(s):
        note = m.group(1).capitalize()
        # Normalize C#/Db style by leaving as-typed; lookup will handle both
        mode = m.group(2)
        full = f"{note} minor" if mode and mode.lower() in ("min", "minor", "m") else f"{note} major"
        return KEY_TO_CAMELOT.get(full)

    return None


__all__ = ["KEY_TO_CAMELOT", "KEY_SHORTHAND", "key_to_camelot"]
