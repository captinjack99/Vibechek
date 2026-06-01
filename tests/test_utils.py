"""Tests for vibechek.utils."""

from __future__ import annotations

from pathlib import Path

import pytest

from vibechek.utils import (
    SUPPORTED_EXTENSIONS,
    find_audio_files,
    report_progress,
    sanitize_folder_name,
)


def test_sanitize_strips_invalid_chars() -> None:
    assert sanitize_folder_name('Hip*Hop/Rap?') == "Hip_Hop_Rap_"


def test_sanitize_empty_returns_unknown() -> None:
    assert sanitize_folder_name(None) == "Unknown"
    assert sanitize_folder_name("") == "Unknown"
    assert sanitize_folder_name("   ") == "Unknown"


def test_sanitize_passes_through_clean_name() -> None:
    assert sanitize_folder_name("Deep House") == "Deep House"


def test_sanitize_rejects_dot_traversal_names() -> None:
    # A genre tag of ".." (or "." / leading-dot variants) must never survive as
    # a path segment that could escape the library root.
    assert sanitize_folder_name("..") == "Unknown"
    assert sanitize_folder_name(".") == "Unknown"
    assert sanitize_folder_name("  ..  ") == "Unknown"
    assert sanitize_folder_name("...") == "Unknown"
    # Separators collapse to "_", so an embedded traversal can't introduce a sep.
    assert "/" not in sanitize_folder_name("../../Windows")
    assert "\\" not in sanitize_folder_name("..\\..\\Windows")


def test_sanitize_strips_trailing_dots_and_spaces() -> None:
    # Windows silently drops trailing dots/spaces from folder names; normalize so
    # our intended name matches what lands on disk.
    assert sanitize_folder_name("House.") == "House"
    assert sanitize_folder_name("House ") == "House"
    assert sanitize_folder_name("House. . ") == "House"


def test_sanitize_maps_windows_reserved_device_names() -> None:
    # Reserved device names are uncreatable on Windows; they must be remapped to
    # a safe, creatable name rather than passed through verbatim.
    for reserved in ("CON", "con", "PRN", "AUX", "NUL", "COM1", "COM9", "LPT1", "LPT9"):
        out = sanitize_folder_name(reserved)
        assert out.lower() not in {
            "con", "prn", "aux", "nul",
            *(f"com{i}" for i in range(1, 10)),
            *(f"lpt{i}" for i in range(1, 10)),
        }
        assert out  # never empty
    # A reserved stem with an extension (e.g. a genre "nul.mp3") is just as
    # reserved on Windows and must also be remapped.
    assert sanitize_folder_name("nul.mp3").lower() != "nul.mp3"
    # Non-reserved lookalikes are untouched.
    assert sanitize_folder_name("Console") == "Console"
    assert sanitize_folder_name("COM10") == "COM10"


def test_find_audio_files_returns_audio_only(tiny_library: Path) -> None:
    files = find_audio_files(tiny_library)
    assert all(f.suffix.lower() in SUPPORTED_EXTENSIONS for f in files)
    assert any(f.name == "track1.mp3" for f in files)
    assert not any(f.name == "notes.txt" for f in files)


def test_find_audio_files_is_deterministic(tiny_library: Path) -> None:
    a = find_audio_files(tiny_library)
    b = find_audio_files(tiny_library)
    assert [str(p) for p in a] == [str(p) for p in b]


def test_find_audio_files_missing_path_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        find_audio_files(tmp_path / "does_not_exist")


def test_report_progress_swallows_callback_errors() -> None:
    def boom(*_args: object) -> None:
        raise RuntimeError("UI gone")

    # Should not raise — callback errors are isolated
    report_progress(boom, 1, 10, "msg")


def test_report_progress_no_callback() -> None:
    # Calling with None should be a no-op
    report_progress(None, 1, 10, "msg")
