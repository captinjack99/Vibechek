"""Tests for vibechek.keys (Camelot conversion)."""

from __future__ import annotations

import pytest

from vibechek.keys import key_to_camelot


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
    ],
)
def test_key_to_camelot_known(key_input: str, expected: str) -> None:
    assert key_to_camelot(key_input) == expected


@pytest.mark.parametrize("key_input", [None, "", "Q minor", "12345", "  ", "Z#m"])
def test_key_to_camelot_unknown(key_input) -> None:
    assert key_to_camelot(key_input) is None
