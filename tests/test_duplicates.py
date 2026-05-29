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


# ---------- Chromaprint similarity ----------


def test_fingerprint_similarity_identical_returns_one() -> None:
    """Two identical fingerprints have similarity 1.0."""
    from vibechek.duplicates import fingerprint_similarity
    fp = [0x12345678, 0xABCDEF01, 0xDEADBEEF]
    assert fingerprint_similarity(fp, fp) == 1.0


def test_fingerprint_similarity_complement_returns_zero() -> None:
    """All-bits-flipped fingerprints have similarity 0.0."""
    from vibechek.duplicates import fingerprint_similarity
    a = [0x00000000, 0x00000000]
    b = [0xFFFFFFFF, 0xFFFFFFFF]
    assert fingerprint_similarity(a, b) == 0.0


def test_fingerprint_similarity_partial_match() -> None:
    """One differing bit in 32 → similarity ≈ 31/32."""
    from vibechek.duplicates import fingerprint_similarity
    a = [0x00000000]
    b = [0x00000001]  # 1 bit set
    sim = fingerprint_similarity(a, b)
    assert abs(sim - (31 / 32)) < 1e-9


def test_fingerprint_similarity_handles_different_lengths() -> None:
    """Truncates to the shorter list — no IndexError on misaligned inputs."""
    from vibechek.duplicates import fingerprint_similarity
    a = [0x12345678, 0xABCDEF01]
    b = [0x12345678]
    # Compares only the overlapping prefix (1 int) → identical
    assert fingerprint_similarity(a, b) == 1.0


def test_fingerprint_similarity_empty_returns_zero() -> None:
    from vibechek.duplicates import fingerprint_similarity
    assert fingerprint_similarity([], []) == 0.0
    assert fingerprint_similarity([1], []) == 0.0


def test_audio_fingerprint_raw_parses_fpcalc_output(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """audio_fingerprint_raw returns a list of unsigned 32-bit ints."""
    import subprocess as _subprocess
    from vibechek import duplicates

    fake_stdout = "DURATION=120\nFINGERPRINT=305419896,-559038737,3735928559\n"
    fake = _subprocess.CompletedProcess(["fpcalc"], 0, fake_stdout, "")
    monkeypatch.setattr(duplicates.subprocess, "run", lambda *a, **kw: fake)

    f = tmp_path / "track.mp3"
    f.write_bytes(b"x")
    raw = duplicates.audio_fingerprint_raw(f, "fpcalc")
    assert raw == [305419896, (-559038737) & 0xFFFFFFFF, 3735928559]


def test_chromaprint_threshold_clusters_similar_fingerprints(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """find_duplicates groups files whose raw fingerprints are similar enough.

    Verifies the chromaprint_similarity_threshold field is actually consulted
    — the bug was that the threshold was dead code.
    """
    from vibechek import duplicates
    from vibechek.config import DuplicateConfig

    # Two files: byte-different so MD5 catches nothing, but raw fingerprints
    # differ by only ~3% of bits — should cluster at threshold=0.85, not at 0.99.
    a = tmp_path / "a.mp3"
    b = tmp_path / "b.mp3"
    c = tmp_path / "c.mp3"
    a.write_bytes(b"a-content")
    b.write_bytes(b"b-content")
    c.write_bytes(b"c-content-totally-different")

    fps = {
        str(a): [0x12345678, 0xABCDEF01, 0xDEADBEEF],
        str(b): [0x12345678, 0xABCDEF01, 0xDEADBEEE],  # 1 bit different
        str(c): [0x00000000, 0x11111111, 0x22222222],  # very different
    }

    monkeypatch.setattr(duplicates, "find_fpcalc", lambda: "fpcalc")

    def fake_raw(path, _cmd, duration=120):
        return fps.get(str(path))

    monkeypatch.setattr(duplicates, "audio_fingerprint_raw", fake_raw)

    # Threshold 0.85: a + b should cluster; c stands alone.
    cfg = DuplicateConfig(
        use_md5=False,
        use_chromaprint=True,
        chromaprint_similarity_threshold=0.85,
    )
    report = duplicates.find_duplicates(tmp_path, cfg)
    assert len(report.audio_duplicates) == 1
    group = report.audio_duplicates[0]
    all_paths = {group.keep.path, *(d.path for d in group.duplicates)}
    assert all_paths == {str(a), str(b)}

    # Threshold 0.999: a + b differ in 1 bit / 96 bits → 0.9896, below 0.999.
    cfg_strict = DuplicateConfig(
        use_md5=False,
        use_chromaprint=True,
        chromaprint_similarity_threshold=0.999,
    )
    report_strict = duplicates.find_duplicates(tmp_path, cfg_strict)
    assert len(report_strict.audio_duplicates) == 0


# ---------- Error surface ----------


def test_handle_duplicates_error_messages_surface_to_caller(tmp_path: Path) -> None:
    """The summary must include `error_messages` so the GUI can show them.

    Errors were just a count — the toast said "errors — see report"
    but there was no report. The summary now includes a list of message strings.
    """
    from vibechek import duplicates as dd

    # Build a report referencing files that don't exist on disk → every move
    # attempt fails with "file not found".
    missing_a = tmp_path / "ghost-a.mp3"
    missing_b = tmp_path / "ghost-b.mp3"
    keeper = dd.FileInfo(
        path=str(tmp_path / "keeper.mp3"),
        filename="keeper.mp3", size_bytes=1, size_mb=0.001,
    )
    group = dd.DuplicateGroup(
        method="md5",
        key="deadbeef",
        keep=keeper,
        duplicates=[
            dd.FileInfo(path=str(missing_a), filename="ghost-a.mp3",
                        size_bytes=1, size_mb=0.001),
            dd.FileInfo(path=str(missing_b), filename="ghost-b.mp3",
                        size_bytes=1, size_mb=0.001),
        ],
        recoverable_mb=0.002,
    )
    report = dd.DuplicateReport(exact_duplicates=[group])
    review = tmp_path / "review"
    cfg = DuplicateConfig(
        action=DuplicateAction.MOVE.value, review_folder=review,
    )
    summary = dd.handle_duplicates(report, cfg)

    assert summary["errors"] == 2
    assert "error_messages" in summary
    assert len(summary["error_messages"]) == 2
    # Each message names the missing file path so the user has actionable info.
    assert any("ghost-a.mp3" in m for m in summary["error_messages"])
    assert any("ghost-b.mp3" in m for m in summary["error_messages"])
    # All "not found" since the source paths didn't exist
    assert all("not found" in m for m in summary["error_messages"])
