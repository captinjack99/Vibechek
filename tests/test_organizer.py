"""Tests for vibechek.organizer."""

from __future__ import annotations

from pathlib import Path

import pytest

from vibechek.config import OrganizationConfig
from vibechek.organizer import (
    organize_from_analysis,
    plan_organization,
    route_new_tracks,
)


def test_plan_buckets_rare_genres_into_other(synthetic_analysis: dict) -> None:
    config = OrganizationConfig(use_subgenres=True, min_genre_size=3)
    plan = plan_organization(synthetic_analysis, config)

    # 4 House tracks: above threshold, go into House/<Subgenre>/
    # 2 Techno: below threshold, go into Other/Techno/
    # 1 Vaporwave: below threshold, goes into Other/Vaporwave/
    house_moves = [m for m in plan.moves if "House" in m.destination.parts and "Other" not in m.destination.parts]
    other_moves = [m for m in plan.moves if "Other" in m.destination.parts]

    assert len(house_moves) == 4
    assert len(other_moves) == 3  # 2 Techno + 1 Vaporwave
    assert {"Techno", "Vaporwave"}.issubset(plan.small_genres)


def test_plan_respects_no_subgenres_flag(synthetic_analysis: dict) -> None:
    config = OrganizationConfig(use_subgenres=False, min_genre_size=3)
    plan = plan_organization(synthetic_analysis, config)

    # When use_subgenres is False, all House tracks land flat in House/
    house_moves = [m for m in plan.moves if "House" in m.destination.parts and "Other" not in m.destination.parts]
    for m in house_moves:
        # destination should be base_dir / House / filename, not base_dir / House / <sub> / filename
        rel = m.destination.relative_to(plan.base_dir)
        assert rel.parts[0] == "House"
        assert len(rel.parts) == 2  # House/<filename>


def test_dry_run_does_not_move_files(synthetic_analysis: dict) -> None:
    config = OrganizationConfig(use_subgenres=True, min_genre_size=3)
    plan_before = plan_organization(synthetic_analysis, config)
    source_paths = [m.source for m in plan_before.moves]
    assert all(p.exists() for p in source_paths)

    stats = organize_from_analysis(synthetic_analysis, config, dry_run=True)

    assert stats.planned == len(plan_before.moves)
    assert stats.moved == 0
    assert all(p.exists() for p in source_paths)  # no files moved


def test_organize_actually_moves_files(synthetic_analysis: dict) -> None:
    config = OrganizationConfig(use_subgenres=True, min_genre_size=3)
    stats = organize_from_analysis(synthetic_analysis, config, dry_run=False)

    assert stats.moved == stats.planned > 0
    assert len(stats.errors) == 0


def test_plan_with_explicit_base_dir_overrides_inferred(synthetic_analysis: dict, tmp_path: Path) -> None:
    custom_root = tmp_path / "custom_destination"
    config = OrganizationConfig(use_subgenres=True, min_genre_size=3)
    plan = plan_organization(synthetic_analysis, config, base_dir=custom_root)
    assert plan.base_dir == custom_root
    for move in plan.moves:
        assert custom_root in move.destination.parents


def test_route_new_tracks_uniquifies_colliding_basenames(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A different staging track with a colliding basename is imported under a
    uniquified name, not silently dropped.

    Regression for the audit's LOW finding: skip-on-exists discarded a
    legitimately-different same-named track (it only showed in skipped_exists,
    never in the library).
    """
    from vibechek import organizer

    staging = tmp_path / "staging"
    library = tmp_path / "library"
    staging.mkdir()

    # A track already in the library's House folder.
    existing = library / "House" / "track.mp3"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"ORIGINAL library track")

    # A DIFFERENT track in staging that happens to share the basename.
    incoming = staging / "track.mp3"
    incoming.write_bytes(b"DIFFERENT staging track content")

    # Avoid needing real ID3 tags: every staging file resolves to genre "House".
    monkeypatch.setattr(organizer, "_read_genre_tag", lambda _fp: "House")

    summary = route_new_tracks(staging, library)

    # The collision was detected (reported) AND the file was still imported.
    assert summary["skipped_exists"] == 1
    assert summary["copied"] == 1
    assert summary["errors"] == 0

    # The original library track is untouched...
    assert existing.read_bytes() == b"ORIGINAL library track"
    # ...and the different incoming track lives alongside it under a unique name.
    house_files = sorted(p.name for p in (library / "House").iterdir())
    assert house_files == ["track.mp3", "track_1.mp3"]
    assert (library / "House" / "track_1.mp3").read_bytes() == b"DIFFERENT staging track content"
