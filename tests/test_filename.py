"""Tests for vibechek.filename (filename pattern extraction)."""

from __future__ import annotations

from vibechek.filename import extract_from_filename


def test_extract_bpm_from_trailing_number() -> None:
    info = extract_from_filename("Artist - Title 128.mp3")
    assert info["filename_bpm"] == 128


# ---------------------------------------------------------------------------
# BPM false-positive regressions (audit: support-modules MED)
# ---------------------------------------------------------------------------


def test_dash_delimited_trailing_number_not_treated_as_bpm() -> None:
    """`Artist - Title - 128` is a track/catalog number, not a tempo."""
    info = extract_from_filename("Artist - Title - 128.mp3")
    assert info["filename_bpm"] is None


def test_year_in_parens_not_treated_as_bpm() -> None:
    """A trailing `(1998)` year must not be mined as BPM."""
    info = extract_from_filename("Artist - Title (1998).mp3")
    assert info["filename_bpm"] is None


def test_bare_four_digit_year_not_treated_as_bpm() -> None:
    info = extract_from_filename("Artist - Live At Somewhere 1998.mp3")
    assert info["filename_bpm"] is None


def test_explicit_bpm_token_overrides_dash_delimiter() -> None:
    """An explicit `bpm` suffix is authoritative even after a ` - ` separator."""
    info = extract_from_filename("Artist - Title - 128bpm.mp3")
    assert info["filename_bpm"] == 128


def test_explicit_bpm_token_with_space() -> None:
    info = extract_from_filename("Artist - Title 124 BPM.flac")
    assert info["filename_bpm"] == 124


def test_underscore_joined_trailing_number_is_bpm() -> None:
    info = extract_from_filename("track_128.mp3")
    assert info["filename_bpm"] == 128


def test_camelot_key_at_end_of_stem_no_delimiter() -> None:
    """End-of-stem keys (no trailing delimiter/extension) must still parse."""
    info = extract_from_filename("Artist - Title - 5A")
    assert info["filename_key"] == "5A"


def test_extract_bpm_with_explicit_bpm_suffix() -> None:
    info = extract_from_filename("Some Track - 124bpm.flac")
    assert info["filename_bpm"] == 124


def test_extract_bpm_rejects_out_of_range() -> None:
    info = extract_from_filename("Album 2024 Remaster - 999.mp3")
    # 999 is out of [60, 200]; should not be captured
    assert info["filename_bpm"] is None


def test_extract_camelot_key() -> None:
    info = extract_from_filename("Artist - Title [8A].mp3")
    assert info["filename_key"] == "8A"


def test_extract_artist_title_with_separator() -> None:
    info = extract_from_filename("Daft Punk - Around the World (Extended Mix).mp3")
    assert info["filename_artist"] == "Daft Punk"
    assert info["filename_title"] == "Around the World"
    assert info["filename_mix"] == "Extended Mix"


def test_strip_leading_track_number() -> None:
    info = extract_from_filename("01 - Some Artist - Some Title.flac")
    assert info["filename_artist"] == "Some Artist"
    assert info["filename_title"] == "Some Title"


def test_no_separator_yields_no_artist_or_title() -> None:
    info = extract_from_filename("just_a_filename.mp3")
    assert info["filename_artist"] is None
    assert info["filename_title"] is None
