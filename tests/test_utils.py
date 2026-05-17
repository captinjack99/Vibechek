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
