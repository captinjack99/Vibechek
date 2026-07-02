"""Tests for vibechek.keys (Camelot conversion + harmonic mixing helpers)."""

from __future__ import annotations

import pytest

from vibechek.keys import (
    COMPATIBLE_MODES,
    compatible_camelot,
    is_compatible_with,
    key_to_camelot,
)


@pytest.mark.parametrize(
    "key_input,expected",
    [
        # Already-Camelot inputs
        ("8A", "8A"),
        ("12B", "12B"),
        ("1a", "1A"),
        ("10b", "10B"),
        # Full names
        ("C major", "8B"),
        ("A minor", "8A"),
        ("F# major", "2B"),
        ("Gb major", "2B"),       # enharmonic
        ("Bb minor", "3A"),
        # Shorthand
        ("C", "8B"),
        ("Cm", "5A"),
        ("F#m", "11A"),
        ("Dbm", "12A"),
        # Free-form
        ("F# min", "11A"),
        ("Eb maj", "5B"),
        ("c minor", "5A"),  # case-insensitive prefix
        # Zero-padded Camelot (Mixed In Key writes "01A".."12B" in some versions)
        ("01A", "1A"),
        ("09B", "9B"),
        # Open Key notation (Traktor / Rekordbox / Beatport exports):
        # 1d = C major = 8B; d ("dur") → major/B, m ("moll") → minor/A
        ("1d", "8B"),
        ("1m", "8A"),
        ("2d", "9B"),
        ("5m", "12A"),
        ("6d", "1B"),   # wheel wrap: 6+7 → 13 → 1
        ("12m", "7A"),
        ("07d", "2B"),  # zero-padded open key
        ("10M", "5A"),  # case-insensitive letter
        # Trailing parenthetical (web/DJ-tool exports like "8B (C major)")
        ("8B (C major)", "8B"),
        ("2d (G major)", "9B"),
        ("C major (8B)", "8B"),
    ],
)
def test_key_to_camelot_known(key_input: str, expected: str) -> None:
    assert key_to_camelot(key_input) == expected


@pytest.mark.parametrize(
    "key_input",
    [
        None, "", "Q minor", "12345", "  ", "Z#m",
        "0d", "13d", "13A", "0A",       # out-of-range wheel numbers
        # Real words that a bare prefix-parse used to misread as keys
        # ("Ambient" → A minor, "Emotional" → E minor): must be unknown.
        "Ambient", "Emotional", "Dark", "Deep House",
        "8A - Energy 7",                 # combined MIK comment, not a key field
    ],
)
def test_key_to_camelot_unknown(key_input) -> None:
    assert key_to_camelot(key_input) is None


# ---------------------------------------------------------------------------
# compatible_camelot
# ---------------------------------------------------------------------------


class TestCompatibleCamelot:
    """The contract that the LibraryFilters/TrackDetails UI relies on."""

    def test_energy_default(self) -> None:
        # 8A → 7A, 9A (±1 step) + 8B (relative)
        assert set(compatible_camelot("8A")) == {"7A", "9A", "8B"}

    def test_energy_mode_explicit(self) -> None:
        assert set(compatible_camelot("8A", mode="energy")) == {"7A", "9A", "8B"}

    def test_harmonic_mode_only_same_letter_neighbours(self) -> None:
        assert set(compatible_camelot("8A", mode="harmonic")) == {"7A", "9A"}
        assert set(compatible_camelot("12B", mode="harmonic")) == {"11B", "1B"}

    def test_strict_mode_only_relative(self) -> None:
        assert compatible_camelot("8A", mode="strict") == ["8B"]
        assert compatible_camelot("3B", mode="strict") == ["3A"]

    def test_energy_boost_includes_semitone_modulation(self) -> None:
        # 8A + 7 (mod 12) = 15 → 3A
        out = compatible_camelot("8A", mode="energy-boost")
        assert set(out) == {"7A", "9A", "8B", "3A"}

    def test_wheel_wraps_at_boundaries(self) -> None:
        # 12A → 11A, 1A (wrap), 12B
        assert set(compatible_camelot("12A")) == {"11A", "1A", "12B"}
        # 1A → 12A (wrap), 2A, 1B
        assert set(compatible_camelot("1A")) == {"12A", "2A", "1B"}
        # 12B → 11B, 1B, 12A (wraps both directions)
        assert set(compatible_camelot("12B")) == {"11B", "1B", "12A"}

    def test_accepts_free_form_key_input(self) -> None:
        # "A minor" resolves through key_to_camelot to 8A
        assert set(compatible_camelot("A minor")) == {"7A", "9A", "8B"}
        assert set(compatible_camelot("Cm")) == set(compatible_camelot("5A"))

    def test_lowercase_letter_accepted(self) -> None:
        # _CAMELOT_RE is case-insensitive
        assert set(compatible_camelot("8a")) == {"7A", "9A", "8B"}

    @pytest.mark.parametrize(
        "garbage",
        # NB: "ABC" / single letters look invalid but `key_to_camelot`
        # interprets them as bare note names (A → "A major" → 11B), which is
        # actually desired forgiveness. Stick to truly malformed inputs here.
        [None, "", "  ", "13A", "0B", "Q minor", "8C", "###"],
    )
    def test_invalid_input_returns_empty(self, garbage) -> None:
        # Spec: "must work for missing/malformed keys (return [] if input is invalid)"
        assert compatible_camelot(garbage) == []
        for mode in COMPATIBLE_MODES:
            assert compatible_camelot(garbage, mode=mode) == []

    def test_unknown_mode_falls_back_to_energy(self) -> None:
        # Don't crash on a stale mode value coming back from the UI store.
        assert set(compatible_camelot("8A", mode="nonsense")) == {"7A", "9A", "8B"}

    def test_no_duplicates_in_output(self) -> None:
        # Sanity: every mode produces a unique list
        for mode in COMPATIBLE_MODES:
            out = compatible_camelot("8A", mode=mode)
            assert len(out) == len(set(out)), f"duplicates in mode {mode}: {out}"


# ---------------------------------------------------------------------------
# is_compatible_with
# ---------------------------------------------------------------------------


class TestIsCompatibleWith:
    def test_same_key_is_always_compatible(self) -> None:
        assert is_compatible_with("8A", "8A") is True
        assert is_compatible_with("A minor", "8A") is True  # normalized

    def test_neighbour_compatible_in_energy(self) -> None:
        assert is_compatible_with("8A", "9A") is True
        assert is_compatible_with("8A", "8B") is True

    def test_distant_keys_not_compatible(self) -> None:
        assert is_compatible_with("8A", "2B") is False
        assert is_compatible_with("1A", "6A") is False

    def test_symmetric_for_neighbours(self) -> None:
        for mode in COMPATIBLE_MODES:
            # 7A ↔ 8A should be the same answer either way under any mode
            # that considers them compatible at all.
            forward = is_compatible_with("8A", "7A", mode=mode)
            backward = is_compatible_with("7A", "8A", mode=mode)
            assert forward == backward, f"asymmetric under {mode}"

    def test_invalid_input_returns_false(self) -> None:
        assert is_compatible_with(None, "8A") is False
        assert is_compatible_with("8A", None) is False
        assert is_compatible_with("nonsense", "8A") is False
        assert is_compatible_with("8A", "13Z") is False

    def test_strict_only_matches_relative(self) -> None:
        assert is_compatible_with("8A", "8B", mode="strict") is True
        assert is_compatible_with("8A", "7A", mode="strict") is False
