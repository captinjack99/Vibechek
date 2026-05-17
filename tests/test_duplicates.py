"""Tests for vibechek.duplicates."""

from __future__ import annotations

from pathlib import Path

import pytest

from vibechek.config import DuplicateConfig
from vibechek.duplicates import (
    DuplicateAction,
    FileInfo,
    choose_keeper,
    file_md5,
    find_duplicates,
    handle_duplicates,
)


# ---------- Pure helpers ----------


def test_file_md5_matches_known_string(tmp_path: Path) -> None:
    f = tmp_path / "x.mp3"
    f.write_bytes(b"hello world")
    # echo -n "hello world" | md5sum → 5eb63bbbe01eeed093cb22bb8f5acdc3
    assert file_md5(f) == "5eb63bbbe01eeed093cb22bb8f5acdc3"


def test_choose_keeper_prefers_flac_over_mp3() -> None:
    files = [
        FileInfo(path="/a/track.mp3", filename="track.mp3", size_bytes=5_000_000, size_mb=5.0),
        FileInfo(path="/a/track.flac", filename="track.flac", size_bytes=20_000_000, size_mb=20.0),
    ]
    keeper, dupes = choose_keeper(files)
    assert keeper.filename == "track.flac"
    assert dupes[0].filename == "track.mp3"


def test_choose_keeper_prefers_larger_on_format_tie() -> None:
    files = [
        FileInfo(path="/a/short.mp3", filename="short.mp3", size_bytes=3_000_000, size_mb=3.0),
        FileInfo(path="/a/long.mp3", filename="long.mp3", size_bytes=8_000_000, size_mb=8.0),
    ]
    keeper, _ = choose_keeper(files)
    assert keeper.filename == "long.mp3"


def test_choose_keeper_prefers_shorter_path_on_size_tie() -> None:
    files = [
        FileInfo(path="/very/deep/folder/track.mp3", filename="track.mp3",
                 size_bytes=1000, size_mb=0.001),
        FileInfo(path="/a/track.mp3", filename="track.mp3",
                 size_bytes=1000, size_mb=0.001),
    ]
    keeper, _ = choose_keeper(files)
    assert keeper.path == "/a/track.mp3"


# ---------- End-to-end scan ----------


def test_find_duplicates_detects_exact_md5_match(tiny_library: Path) -> None:
    # track3.mp3 and track3_dup.mp3 share content per the fixture
    report = find_duplicates(
        tiny_library,
        DuplicateConfig(use_md5=True, use_chromaprint=False),
    )

    assert len(report.exact_duplicates) == 1
    group = report.exact_duplicates[0]
    assert group.method == "md5"
    filenames = {group.keep.filename, *(d.filename for d in group.duplicates)}
    assert filenames == {"track3.mp3", "track3_dup.mp3"}


def test_find_duplicates_handle_move_relocates_dupes(tmp_path: Path) -> None:
    # Build two byte-identical files
    a = tmp_path / "lib" / "a.mp3"
    b = tmp_path / "lib" / "b.mp3"
    a.parent.mkdir()
    a.write_bytes(b"same content")
    b.write_bytes(b"same content")

    review = tmp_path / "review"
    config = DuplicateConfig(
        use_md5=True,
        use_chromaprint=False,
        action=DuplicateAction.MOVE.value,
        review_folder=review,
    )

    report = find_duplicates(tmp_path / "lib", config)
    summary = handle_duplicates(report, config)

    assert summary["moved"] == 1
    assert review.exists()
    moved_files = list(review.iterdir())
    assert len(moved_files) == 1
    # Originals: one stays, one was moved
    assert sum(1 for p in (a, b) if p.exists()) == 1


def test_handle_move_requires_review_folder(tmp_path: Path) -> None:
    config = DuplicateConfig(action=DuplicateAction.MOVE.value, review_folder=None)
    with pytest.raises(ValueError):
        handle_duplicates(
            __import__("vibechek.duplicates", fromlist=["DuplicateReport"]).DuplicateReport(),
            config,
        )
