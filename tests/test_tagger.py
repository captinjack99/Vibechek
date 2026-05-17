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
    RestoreStats,
    _first,
    apply_ml_tags,
    backup_tags,
    read_all_tags,
    restore_tags,
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
    # All tracks counted as either applied or low-conf
    assert stats.genre_applied + stats.genre_skipped_low_confidence == stats.total


def test_apply_ml_tags_respects_confidence_threshold(synthetic_analysis: dict) -> None:
    """Lower threshold → more tracks pass; higher threshold → fewer."""
    low = TaggingConfig(genre_confidence_threshold=0.5)
    high = TaggingConfig(genre_confidence_threshold=0.9)

    low_stats = apply_ml_tags(synthetic_analysis, low, dry_run=True)
    high_stats = apply_ml_tags(synthetic_analysis, high, dry_run=True)

    assert low_stats.genre_applied >= high_stats.genre_applied


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
