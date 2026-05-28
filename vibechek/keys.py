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

# Valid harmonic-mixing modes. Each picks a different set of "compatible"
# Camelot wheels relative to a starting key. Order roughly: tightest to loosest.
COMPATIBLE_MODES = ("strict", "harmonic", "energy", "energy-boost")


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
        if mode is None:
            # Bare note (e.g. "C") → major, the conventional default.
            full = f"{note} major"
        elif mode.lower() in ("min", "minor", "m"):
            full = f"{note} minor"
        elif mode.lower() in ("maj", "major"):
            full = f"{note} major"
        else:
            # An explicit but unrecognized mode ("dim", "aug", ...) — don't
            # silently guess major; return None so callers treat it as unknown
            # rather than a confidently-wrong Camelot.
            return None
        return KEY_TO_CAMELOT.get(full)

    return None


def _parse_camelot(value: str | None) -> tuple[int, str] | None:
    """Return (number, letter) for a Camelot string, or None for garbage input.

    Accepts both raw Camelot (`"8A"`) and any free-form key that resolves
    through `key_to_camelot` (`"C minor"` → `("5", "A")`). Anything that
    doesn't parse returns None — callers should treat that as "no compatible
    keys" rather than raising, so the UI degrades gracefully on dirty data.
    """
    camelot = key_to_camelot(value)
    if camelot is None:
        return None
    m = _CAMELOT_RE.match(camelot)
    if m is None:
        return None
    return int(m.group(1)), m.group(2).upper()


def _wrap_camelot(number: int, letter: str) -> str:
    """Camelot wheel wraps 12 → 1, 0 → 12. Used by every neighbour calculation."""
    n = ((number - 1) % 12) + 1
    return f"{n}{letter}"


def _flip_letter(letter: str) -> str:
    return "B" if letter == "A" else "A"


def compatible_camelot(key: str | None, mode: str = "energy") -> list[str]:
    """Return Camelot wheels harmonically compatible with `key` for DJ mixing.

    Modes (matches the LibraryFilters mode toggle):
      - `strict`        : just the relative key across A/B (8A → [8B]).
      - `harmonic`      : same letter, ±1 step (8A → [7A, 9A]).
      - `energy`        : harmonic + relative (8A → [7A, 9A, 8B]). Default.
      - `energy-boost`  : energy + a +1-semitone modulation (8A → [7A, 9A, 8B, 3A]).
                          Camelot semitone-up is +7 on the wheel within the same letter.

    Returns `[]` for invalid / missing input rather than raising — the helper
    is called from filter chips that fire on every render. Order is stable
    (used directly in the UI for chip placement); no duplicates.
    """
    parsed = _parse_camelot(key)
    if parsed is None:
        return []
    number, letter = parsed
    if mode not in COMPATIBLE_MODES:
        # Unknown mode → default to the safest "energy" behaviour.
        mode = "energy"

    result: list[str] = []

    def add(wheel: str) -> None:
        if wheel not in result:
            result.append(wheel)

    if mode == "strict":
        add(_wrap_camelot(number, _flip_letter(letter)))
        return result

    if mode == "harmonic":
        add(_wrap_camelot(number - 1, letter))
        add(_wrap_camelot(number + 1, letter))
        return result

    # "energy" (and the base of "energy-boost"): ±1 step + relative
    add(_wrap_camelot(number - 1, letter))
    add(_wrap_camelot(number + 1, letter))
    add(_wrap_camelot(number, _flip_letter(letter)))

    if mode == "energy-boost":
        # +1 semitone modulation: same letter, +7 on the wheel (wraps).
        add(_wrap_camelot(number + 7, letter))

    return result


def is_compatible_with(a: str | None, b: str | None, mode: str = "energy") -> bool:
    """Symmetric compatibility check. True iff b ∈ compatible(a) OR a == b.

    "Same key" is always compatible — DJs mix tracks in identical keys all the
    time. Either input being unparseable returns False (no half-known matches).
    """
    a_parsed = _parse_camelot(a)
    b_parsed = _parse_camelot(b)
    if a_parsed is None or b_parsed is None:
        return False
    a_camelot = _wrap_camelot(*a_parsed)
    b_camelot = _wrap_camelot(*b_parsed)
    if a_camelot == b_camelot:
        return True
    # Check BOTH directions so the function is genuinely symmetric as
    # documented. "energy-boost" is directional (it adds a +7 modulation but
    # not its inverse), so `b in compatible(a)` alone would make
    # is_compatible_with(8A, 3A) != is_compatible_with(3A, 8A). OR-ing both
    # directions restores symmetry without changing the looser/creative intent.
    return (
        b_camelot in compatible_camelot(a_camelot, mode=mode)
        or a_camelot in compatible_camelot(b_camelot, mode=mode)
    )


__all__ = [
    "KEY_TO_CAMELOT",
    "KEY_SHORTHAND",
    "COMPATIBLE_MODES",
    "key_to_camelot",
    "compatible_camelot",
    "is_compatible_with",
]
