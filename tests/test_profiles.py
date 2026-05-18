"""Tests for vibechek.profiles — built-in DJ presets."""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from vibechek import profiles
from vibechek.cli import main
from vibechek.config import VibechekConfig


def test_built_in_profiles_present() -> None:
    expected = {"house-dj", "disco-dj", "open-format", "edm-festival", "bar-resident", "afterhours"}
    assert expected <= set(profiles.BUILT_IN_PROFILES)


def test_list_profiles_returns_json_safe_dicts() -> None:
    out = profiles.list_profiles()
    assert len(out) >= 6
    for p in out:
        # The CLI / RPC serializes this — every field must be primitive.
        assert isinstance(p["name"], str)
        assert isinstance(p["description"], str)
        # timeslot_bpm_bands serialized to list pairs (JSON has no tuple)
        for band in p["timeslot_bpm_bands"].values():
            assert isinstance(band, list)
            assert len(band) == 2


def test_get_profile_case_insensitive() -> None:
    assert profiles.get_profile("HOUSE-DJ") is not None
    assert profiles.get_profile("house-dj") is not None
    assert profiles.get_profile("  disco-dj ") is not None  # strip


def test_get_profile_unknown_returns_none() -> None:
    assert profiles.get_profile("ambient-jazz") is None
    assert profiles.get_profile("") is None
    assert profiles.get_profile(None) is None  # type: ignore[arg-type]


def test_apply_profile_overrides_fields(tmp_path: Path) -> None:
    cfg = VibechekConfig()
    # Sanity baseline
    assert cfg.tagging.genre_confidence_threshold == 0.85

    disco = profiles.get_profile("disco-dj")
    assert disco is not None
    profiles.apply_profile(disco, cfg)
    assert cfg.tagging.genre_confidence_threshold == 0.75
    assert cfg.organization.min_genre_size == 3


def test_apply_profile_only_writes_specified_fields() -> None:
    cfg = VibechekConfig()
    cfg.tagging.write_subgenre_as_main_genre = False  # arbitrary unrelated tweak

    profiles.apply_profile(profiles.get_profile("house-dj"), cfg)  # type: ignore[arg-type]
    # The profile says nothing about write_subgenre_as_main_genre → untouched.
    assert cfg.tagging.write_subgenre_as_main_genre is False


def test_load_profile_writes_to_disk_and_returns_summary() -> None:
    result = profiles.load_profile("edm-festival")
    assert result["loaded"] == "edm-festival"
    assert result["applied"]["min_genre_size"] == 30
    assert result["applied"]["use_gpu"] == "on"

    # Reload from disk and confirm the change persisted.
    reloaded = VibechekConfig.load()
    assert reloaded.organization.min_genre_size == 30


def test_load_profile_unknown_raises_keyerror() -> None:
    with pytest.raises(KeyError):
        profiles.load_profile("non-existent")


def test_cli_profile_list_shows_built_ins() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["profile", "list"])
    assert result.exit_code == 0, result.output
    for name in ("house-dj", "disco-dj", "edm-festival", "bar-resident"):
        assert name in result.output


def test_cli_profile_load_persists_and_prints_summary() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["profile", "load", "bar-resident"])
    assert result.exit_code == 0, result.output
    assert "bar-resident" in result.output
    reloaded = VibechekConfig.load()
    assert reloaded.tagging.genre_confidence_threshold == 0.65


def test_cli_profile_load_unknown_returns_usage_error() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["profile", "load", "no-such-profile"])
    # Click UsageError exit code is 2
    assert result.exit_code != 0
