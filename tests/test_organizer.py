"""Tests for vibechek.organizer."""

from __future__ import annotations

from pathlib import Path

from vibechek.config import OrganizationConfig
from vibechek.organizer import (
    organize_from_analysis,
    plan_organization,
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
