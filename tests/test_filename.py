"""Tests for vibechek.filename (filename pattern extraction)."""

from __future__ import annotations

from vibechek.filename import extract_from_filename


def test_extract_bpm_from_trailing_number() -> None:
    info = extract_from_filename("Artist - Title 128.mp3")
    assert info["filename_bpm"] == 128


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
