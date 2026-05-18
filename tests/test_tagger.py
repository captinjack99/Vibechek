"""Tests for vibechek.tagger.

Integration tests need real audio files — drop them in `tests/fixtures/`
(see the tagger_fixture_dir fixture below). Without fixtures, only the
pure-helper tests run.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vibechek.config import TaggingConfig
from vibechek.tagger import (
    ApplyStats,
    BackupStats,
    RemapRestoreStats,
    RestoreStats,
    _apply_mp3,
    _first,
    _write_mp3_tags,
    apply_ml_tags,
    backup_tags,
    read_all_tags,
    restore_tags,
    restore_tags_with_remap,
    write_all_tags,
)

FIXTURES = Path(__file__).parent / "fixtures"


def test_first_unwraps_list() -> None:
    assert _first(["a", "b"]) == "a"


def test_first_passes_through_scalar() -> None:
    assert _first("hello") == "hello"


def test_first_handles_none_and_empty() -> None:
    assert _first(None) is None
    assert _first([]) is None


def test_read_all_tags_unsupported_format(tmp_path: Path) -> None:
    junk = tmp_path / "track.xyz"
    junk.write_bytes(b"not audio")
    tags = read_all_tags(junk)
    assert tags.get("_unsupported") is True


def test_apply_ml_tags_dry_run_does_not_touch_files(synthetic_analysis: dict) -> None:
    config = TaggingConfig()
    stats = apply_ml_tags(synthetic_analysis, config, dry_run=True)
    assert isinstance(stats, ApplyStats)
    # Empty files used as fixtures — applying for real would fail; dry run is safe
    assert stats.other_tags_applied == 0
    # Every track counted as strict-applied, parent-only-applied, or low-conf.
    # `genre_applied_parent_only` was added when we introduced the two-stage
    # confidence fallback (see TaggingConfig.parent_genre_confidence_threshold).
    assert (
        stats.genre_applied
        + stats.genre_applied_parent_only
        + stats.genre_skipped_low_confidence
        == stats.total
    )


def test_apply_ml_tags_respects_confidence_threshold(synthetic_analysis: dict) -> None:
    """Lower threshold → more tracks pass; higher threshold → fewer."""
    # Pin parent_genre_confidence_threshold to a very high value so the
    # two-stage fallback can't kick in here — this test exercises only the
    # strict subgenre threshold.
    low = TaggingConfig(genre_confidence_threshold=0.5, parent_genre_confidence_threshold=0.99)
    high = TaggingConfig(genre_confidence_threshold=0.9, parent_genre_confidence_threshold=0.99)

    low_stats = apply_ml_tags(synthetic_analysis, low, dry_run=True)
    high_stats = apply_ml_tags(synthetic_analysis, high, dry_run=True)

    assert low_stats.genre_applied >= high_stats.genre_applied


def test_apply_ml_tags_parent_fallback_lifts_coverage(synthetic_analysis: dict) -> None:
    """Two-stage: subgenre <85% but family >=50% → tagged with parent genre.

    The synthetic_analysis fixture has tracks at confidence 0.92, 0.88, 0.75,
    0.50, 0.91, 0.42, 0.65. None set `ml_genre_raw_confidence`, so the
    backward-compat path treats family conf as the stage-1 input. Stage-1
    (strict 0.85) tags 0.92, 0.88, 0.91. Stage-2 (parent 0.50) picks up
    0.75, 0.50, 0.65 as parent-only. 0.42 falls below both.
    """
    config = TaggingConfig()  # defaults: 0.85 strict, 0.50 parent fallback
    stats = apply_ml_tags(synthetic_analysis, config, dry_run=True)
    assert stats.genre_applied_parent_only > 0
    # The 0.42 track is the only one we expect below both thresholds.
    assert stats.genre_skipped_low_confidence == 1


def test_apply_ml_tags_respects_raw_confidence_when_present(tmp_path) -> None:
    """If ml_genre_raw_confidence is present it gates stage-1, not family conf."""
    track_path = tmp_path / "x.mp3"
    track_path.write_bytes(b"")
    analysis = {
        "tracks": [{
            "path": str(track_path),
            "ml_analysis": {
                "ml_genre": "House",
                "ml_subgenre": "Deep House",
                # Family is high, but the single-class winner is uncertain —
                # i.e. the model knows it's House but can't subgenre it.
                "ml_genre_confidence": 0.92,
                "ml_genre_raw_confidence": 0.30,
            },
        }],
    }
    stats = apply_ml_tags(analysis, TaggingConfig(), dry_run=True)
    # Stage 1 sees raw_confidence=0.30, fails (<0.85). Stage 2 sees
    # family=0.92 >= 0.50, passes — parent-only tag.
    assert stats.genre_applied == 0
    assert stats.genre_applied_parent_only == 1


# ---------------------------------------------------------------------------
# Integration: real audio file required
# ---------------------------------------------------------------------------


def _has_fixtures() -> bool:
    if not FIXTURES.exists():
        return False
    for ext in (".mp3", ".flac", ".m4a"):
        if any(FIXTURES.glob(f"*{ext}")):
            return True
    return False


@pytest.mark.skipif(not _has_fixtures(), reason="No audio fixtures in tests/fixtures/")
def test_backup_restore_roundtrip(tmp_path: Path) -> None:
    """Backing up and immediately restoring should be a no-op."""
    # Copy fixtures into a tmp library so the test doesn't write into the repo
    library = tmp_path / "lib"
    library.mkdir()
    for f in FIXTURES.iterdir():
        if f.suffix.lower() in (".mp3", ".flac", ".m4a"):
            (library / f.name).write_bytes(f.read_bytes())

    backup_file = tmp_path / "backup.json"
    backup_stats = backup_tags(library, backup_file)
    assert isinstance(backup_stats, BackupStats)
    assert backup_stats.backed_up == backup_stats.total > 0

    # Snapshot pre-restore tag state from the backup itself
    pre_backup = json.loads(backup_file.read_text(encoding="utf-8"))

    # Restore (no-op since nothing changed)
    restore_stats = restore_tags(backup_file)
    assert isinstance(restore_stats, RestoreStats)
    assert restore_stats.restored == restore_stats.total

    # Backup again and compare — content should be identical (timestamps aside)
    second_backup = tmp_path / "backup2.json"
    backup_tags(library, second_backup)
    post_backup = json.loads(second_backup.read_text(encoding="utf-8"))

    assert pre_backup["files"].keys() == post_backup["files"].keys()
    for path in pre_backup["files"]:
        # Tags should match exactly
        assert pre_backup["files"][path] == post_backup["files"][path]


# ---------------------------------------------------------------------------
# restore_tags_with_remap — audit #19
# ---------------------------------------------------------------------------
#
# These tests exercise the match-strategy logic. The mock files are .mp3 files
# with text contents, so the actual mutagen `write_all_tags` call inevitably
# fails (no MPEG sync) — but the matching strategy is recorded on each
# `matches` entry BEFORE the write is attempted, and the `matched_*` tally is
# incremented regardless of write outcome. That lets us validate the remap
# algorithm without a binary MP3 fixture.


def _write_backup(path: Path, files: dict[str, dict]) -> None:
    """Helper: write a backup JSON with the given files dict."""
    backup = {
        "version": "1.0",
        "source_directory": "/orig/somewhere",
        "total_files": len(files),
        "files": files,
    }
    path.write_text(json.dumps(backup), encoding="utf-8")


def test_restore_remap_exact_match_preferred(tmp_path: Path) -> None:
    """When the backup's original path still exists on disk, use it."""
    library = tmp_path / "lib"
    library.mkdir()
    track = library / "song.mp3"
    track.write_text("fake mp3", encoding="utf-8")

    backup_file = tmp_path / "backup.json"
    _write_backup(backup_file, {str(track): {"artist": "Foo"}})

    stats = restore_tags_with_remap(backup_file, library)
    assert isinstance(stats, RemapRestoreStats)
    assert stats.total == 1
    assert stats.matched_exact == 1
    assert stats.matched_filename_size == 0
    assert stats.matched_filename == 0
    assert stats.skipped_missing == 0

    rec = stats.matches[0]
    assert rec["strategy"] == "exact"
    assert rec["matched"] == str(track)


def test_restore_remap_moved_library_matches_by_filename(tmp_path: Path) -> None:
    """When the original path is gone but the filename appears uniquely in
    library_root, restore matches by filename."""
    old_lib = tmp_path / "D" / "Music"
    old_lib.mkdir(parents=True)
    # Pretend the user backed up D:\Music\song.mp3 then moved everything to E:\
    backup_path_str = str(old_lib / "song.mp3")
    # Do NOT create that file — only the new home exists
    new_lib = tmp_path / "E" / "Music"
    new_lib.mkdir(parents=True)
    new_track = new_lib / "song.mp3"
    new_track.write_text("fake mp3", encoding="utf-8")

    backup_file = tmp_path / "backup.json"
    _write_backup(backup_file, {backup_path_str: {"artist": "Foo"}})

    stats = restore_tags_with_remap(backup_file, new_lib)
    assert stats.total == 1
    assert stats.matched_exact == 0
    assert stats.matched_filename == 1
    assert stats.skipped_missing == 0

    rec = stats.matches[0]
    assert rec["strategy"] == "filename"
    assert rec["matched"] == str(new_track)


def test_restore_remap_ambiguous_filename_is_skipped(tmp_path: Path) -> None:
    """Two files in library_root share the backup entry's filename → skip,
    don't gamble on which one to restore to."""
    old_lib = tmp_path / "old"
    old_lib.mkdir()
    backup_path_str = str(old_lib / "song.mp3")
    new_lib = tmp_path / "new"
    (new_lib / "albumA").mkdir(parents=True)
    (new_lib / "albumB").mkdir(parents=True)
    (new_lib / "albumA" / "song.mp3").write_text("a", encoding="utf-8")
    (new_lib / "albumB" / "song.mp3").write_text("b", encoding="utf-8")

    backup_file = tmp_path / "backup.json"
    _write_backup(backup_file, {backup_path_str: {"artist": "Foo"}})

    stats = restore_tags_with_remap(backup_file, new_lib)
    assert stats.total == 1
    assert stats.restored == 0
    assert stats.matched_exact == 0
    assert stats.matched_filename == 0
    assert stats.skipped_missing == 1

    rec = stats.matches[0]
    assert rec["strategy"] == "ambiguous"
    assert rec["matched"] is None


def test_restore_remap_missing_file_is_skipped(tmp_path: Path) -> None:
    """No file in library_root matches the backup entry's filename → skip."""
    old_lib = tmp_path / "old"
    old_lib.mkdir()
    backup_path_str = str(old_lib / "gone.mp3")
    new_lib = tmp_path / "new"
    new_lib.mkdir()
    # library_root has a different file
    (new_lib / "different.mp3").write_text("x", encoding="utf-8")

    backup_file = tmp_path / "backup.json"
    _write_backup(backup_file, {backup_path_str: {"artist": "Foo"}})

    stats = restore_tags_with_remap(backup_file, new_lib)
    assert stats.total == 1
    assert stats.restored == 0
    assert stats.skipped_missing == 1
    assert stats.matched_exact == stats.matched_filename == 0

    rec = stats.matches[0]
    assert rec["strategy"] == "missing"
    assert rec["matched"] is None


def test_restore_remap_filename_size_match_when_size_recorded(tmp_path: Path) -> None:
    """When the backup records `_size`, a (filename, size) match beats a
    plain filename match — even if multiple files share the name."""
    old_lib = tmp_path / "old"
    old_lib.mkdir()
    backup_path_str = str(old_lib / "song.mp3")

    new_lib = tmp_path / "new"
    (new_lib / "a").mkdir(parents=True)
    (new_lib / "b").mkdir(parents=True)
    correct = new_lib / "a" / "song.mp3"
    decoy = new_lib / "b" / "song.mp3"
    correct.write_text("AAAA", encoding="utf-8")  # 4 bytes
    decoy.write_text("XXXXXXXX", encoding="utf-8")  # 8 bytes

    backup_file = tmp_path / "backup.json"
    _write_backup(backup_file, {backup_path_str: {"artist": "Foo", "_size": 4}})

    stats = restore_tags_with_remap(backup_file, new_lib)
    assert stats.total == 1
    assert stats.matched_filename_size == 1
    assert stats.matched_filename == 0
    assert stats.skipped_missing == 0

    rec = stats.matches[0]
    assert rec["strategy"] == "filename_size"
    assert rec["matched"] == str(correct)


def test_restore_remap_mixed_outcomes(tmp_path: Path) -> None:
    """End-to-end: a single backup with one exact match, one filename match,
    one ambiguous, one missing. Confirms the per-strategy counters."""
    old_lib = tmp_path / "old"
    old_lib.mkdir()
    new_lib = tmp_path / "new"
    new_lib.mkdir()

    # exact match: the original path lives under new_lib too
    exact_track = new_lib / "exact.mp3"
    exact_track.write_text("e", encoding="utf-8")

    # filename match: backup path is in old_lib, only one match in new_lib
    moved_track = new_lib / "subdir" / "moved.mp3"
    moved_track.parent.mkdir()
    moved_track.write_text("m", encoding="utf-8")

    # ambiguous: two files with the same name in new_lib
    (new_lib / "dupA").mkdir()
    (new_lib / "dupB").mkdir()
    (new_lib / "dupA" / "dup.mp3").write_text("a", encoding="utf-8")
    (new_lib / "dupB" / "dup.mp3").write_text("b", encoding="utf-8")

    backup_file = tmp_path / "backup.json"
    _write_backup(backup_file, {
        str(exact_track): {"artist": "E"},
        str(old_lib / "moved.mp3"): {"artist": "M"},
        str(old_lib / "dup.mp3"): {"artist": "D"},
        str(old_lib / "vanished.mp3"): {"artist": "V"},
    })

    stats = restore_tags_with_remap(backup_file, new_lib)
    assert stats.total == 4
    assert stats.matched_exact == 1
    assert stats.matched_filename == 1
    # Ambiguous is counted in skipped_missing (no write happened)
    assert stats.skipped_missing == 2

    strategies = sorted(r["strategy"] for r in stats.matches)
    assert strategies == ["ambiguous", "exact", "filename", "missing"]


def test_restore_remap_progress_callback_invoked(tmp_path: Path) -> None:
    """Sanity: the progress callback fires once per backup entry."""
    new_lib = tmp_path / "lib"
    new_lib.mkdir()
    track = new_lib / "one.mp3"
    track.write_text("x", encoding="utf-8")

    backup_file = tmp_path / "backup.json"
    _write_backup(backup_file, {
        str(track): {"artist": "A"},
        str(tmp_path / "old" / "two.mp3"): {"artist": "B"},
    })

    calls: list[tuple[int, int, str]] = []
    stats = restore_tags_with_remap(
        backup_file, new_lib,
        on_progress=lambda c, t, m: calls.append((c, t, m)),
    )
    assert stats.total == 2
    assert len(calls) == 2
    assert calls[0][1] == 2  # total reported correctly
    assert calls[-1][0] == 2  # final tick is current==total


# ---------------------------------------------------------------------------
# ID3 text-frame encoding — audit #23
# ---------------------------------------------------------------------------
#
# We can't write a real MP3 in pytest without a binary fixture, so we cover
# the encoding plumbing with unit tests on mocked mutagen calls. The goal is
# to prove every TXXX/TCON/TIT1/TBPM/TKEY/TIT2/TPE1/TALB write uses the
# configured encoding, not a hardcoded `3`.


class _RecordingTags(dict):
    """Stand-in for `mutagen.id3.ID3Tags` that records frame writes."""

    def __init__(self) -> None:
        super().__init__()
        self.added: list = []

    def add(self, frame) -> None:  # noqa: ANN001 — mutagen has no public Frame type
        self.added.append(frame)
        self[frame.HashKey] = frame

    def delall(self, key: str) -> None:
        for k in list(self.keys()):
            if k == key or k.startswith(key + ":"):
                del self[k]


class _FakeMP3:
    """Minimal MP3 stub: real tag classes, fake `save()`."""

    def __init__(self) -> None:
        self.tags = _RecordingTags()
        self.saved = False

    def add_tags(self) -> None:
        pass

    def save(self) -> None:
        self.saved = True


def _install_fake_mp3(monkeypatch: pytest.MonkeyPatch) -> list[_FakeMP3]:
    """Patch tagger.MP3 to return a fresh _FakeMP3 per call. Returns the
    accumulator list so tests can introspect what got written."""
    instances: list[_FakeMP3] = []

    def factory(_path):  # noqa: ANN001
        inst = _FakeMP3()
        instances.append(inst)
        return inst

    from vibechek import tagger
    monkeypatch.setattr(tagger, "MP3", factory)
    return instances


def test_apply_mp3_uses_configured_encoding(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Audit #23: every TCON/TIT1/TXXX/TBPM/TKEY write uses
    config.id3_text_encoding (here 1 = UTF-16 for Rekordbox 5)."""
    instances = _install_fake_mp3(monkeypatch)
    track = tmp_path / "song.mp3"
    track.write_text("not a real mp3", encoding="utf-8")

    config = TaggingConfig(
        id3_text_encoding=1,
        skip_bpm_and_key=False,  # exercise the BPM/key paths too
        preserve_rekordbox_frames=False,
    )
    ml = {
        "ml_subgenre": "Deep House",
        "ml_bpm": 124.3,
        "ml_key": "9A",
        "ml_energy": 4,
        "ml_mood": "Driving",
        "ml_timeslot": "Peak",
        "ml_direction": "Up",
        "ml_vocal": "Vocal",
    }

    _apply_mp3(track, ml, apply_genre=True, genre_value="Deep House", config=config)

    assert len(instances) == 1
    written = instances[0].tags.added
    assert written, "expected at least one frame to be written"
    for frame in written:
        assert frame.encoding == 1, (
            f"{type(frame).__name__} wrote encoding={frame.encoding}, "
            f"expected 1 (config.id3_text_encoding)"
        )


def test_apply_mp3_default_encoding_is_utf8(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Default TaggingConfig() still writes UTF-8 — no behavior change for
    existing users."""
    instances = _install_fake_mp3(monkeypatch)
    track = tmp_path / "song.mp3"
    track.write_text("not a real mp3", encoding="utf-8")

    config = TaggingConfig(preserve_rekordbox_frames=False)
    ml = {"ml_subgenre": "Tech House"}
    _apply_mp3(track, ml, apply_genre=True, genre_value="Tech House", config=config)

    assert instances[0].tags.added
    for frame in instances[0].tags.added:
        assert frame.encoding == 3


def test_write_mp3_tags_respects_config_encoding(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """_write_mp3_tags (used by restore) honors the passed config's encoding."""
    instances = _install_fake_mp3(monkeypatch)
    track = tmp_path / "song.mp3"
    track.write_text("x", encoding="utf-8")

    tags = {
        "title": "T", "artist": "A", "album": "Al", "genre": "G",
        "bpm": "120", "key": "1A", "subgenre": "S",
        "txxx_energy": "3",
    }
    _write_mp3_tags(track, tags, TaggingConfig(id3_text_encoding=0))

    assert instances[0].tags.added or len(instances[0].tags) > 0
    # Frames written via __setitem__ (text frames) live in the tags dict; TXXX
    # writes go through add(). Inspect both.
    all_frames = list(instances[0].tags.values())
    assert all_frames
    for frame in all_frames:
        # GEOB/PRIV would have their own encoding; we only test text frames.
        assert getattr(frame, "encoding", 0) == 0


def test_write_all_tags_passes_config_through(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The public `write_all_tags(path, tags, config)` forwards config into
    the MP3 writer."""
    instances = _install_fake_mp3(monkeypatch)
    track = tmp_path / "song.mp3"
    track.write_text("x", encoding="utf-8")

    ok, err = write_all_tags(
        track,
        {"title": "Hi"},
        TaggingConfig(id3_text_encoding=1),
    )
    assert ok and err is None
    assert instances[0].tags
    for frame in instances[0].tags.values():
        assert frame.encoding == 1
