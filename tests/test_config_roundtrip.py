"""Tests for vibechek.config persistence — JSON round-trip + TOML migration.

Covers:
- JSON round-trip preserves None (regression test).
- JSON round-trip preserves Path fields as strings on disk, Path objects in
  the loaded config.
- Legacy `config.toml` is read as a one-time migration fallback.
- Save always writes JSON, never TOML.
- Save round-trip of a TOML-loaded config produces a config.json the next load
  prefers over the legacy file.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from vibechek.config import (
    AnalysisConfig,
    CONFIG_FILE,
    DuplicateConfig,
    LEGACY_CONFIG_FILE,
    OrganizationConfig,
    TaggingConfig,
    VibechekConfig,
)


def test_save_writes_json_not_toml(tmp_path: Path) -> None:
    cfg = VibechekConfig()
    target = tmp_path / "config.json"
    cfg.save(target)
    assert target.exists()
    # Round-trips through json.loads — would raise on TOML output.
    raw = json.loads(target.read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    assert "analysis" in raw and "tagging" in raw


def test_roundtrip_preserves_none_for_optional_path(tmp_path: Path) -> None:
    """Regression: review_folder=None must round-trip as None."""
    cfg = VibechekConfig()
    assert cfg.duplicates.review_folder is None  # default
    assert cfg.organization.target_root is None  # default

    target = tmp_path / "config.json"
    cfg.save(target)

    raw = json.loads(target.read_text(encoding="utf-8"))
    # None is encoded as JSON null, NOT dropped from the dict.
    assert raw["duplicates"]["review_folder"] is None
    assert raw["organization"]["target_root"] is None

    loaded = VibechekConfig.load(target)
    assert loaded.duplicates.review_folder is None
    assert loaded.organization.target_root is None


def test_roundtrip_preserves_explicit_path(tmp_path: Path) -> None:
    cfg = VibechekConfig(
        duplicates=DuplicateConfig(review_folder=Path("/tmp/dupes")),
        organization=OrganizationConfig(target_root=Path("/tmp/sorted")),
    )
    target = tmp_path / "config.json"
    cfg.save(target)

    raw = json.loads(target.read_text(encoding="utf-8"))
    # Paths serialize as strings.
    assert raw["duplicates"]["review_folder"] == str(Path("/tmp/dupes"))
    assert raw["organization"]["target_root"] == str(Path("/tmp/sorted"))

    loaded = VibechekConfig.load(target)
    assert loaded.duplicates.review_folder == Path("/tmp/dupes")
    assert loaded.organization.target_root == Path("/tmp/sorted")


def test_load_missing_file_returns_defaults(tmp_path: Path) -> None:
    loaded = VibechekConfig.load(tmp_path / "does-not-exist.json")
    assert loaded == VibechekConfig()


def test_load_corrupt_json_returns_defaults(tmp_path: Path) -> None:
    target = tmp_path / "config.json"
    target.write_text("{ not valid json", encoding="utf-8")
    loaded = VibechekConfig.load(target)
    assert loaded == VibechekConfig()


def test_legacy_toml_fallback_when_no_json(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """If `config.json` is absent but `config.toml` exists, read TOML."""
    json_path = tmp_path / "config.json"
    toml_path = tmp_path / "config.toml"
    toml_path.write_text(
        "[analysis]\nworkers = 4\nuse_gpu = \"off\"\n",
        encoding="utf-8",
    )

    # Redirect the module-level paths to our tmp files.
    from vibechek import config as config_mod
    monkeypatch.setattr(config_mod, "CONFIG_FILE", json_path)
    monkeypatch.setattr(config_mod, "LEGACY_CONFIG_FILE", toml_path)

    loaded = VibechekConfig.load()
    assert loaded.analysis.workers == 4
    assert loaded.analysis.use_gpu == "off"


def test_legacy_toml_save_migrates_to_json(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """After loading from TOML, save() writes JSON to the new path."""
    json_path = tmp_path / "config.json"
    toml_path = tmp_path / "config.toml"
    toml_path.write_text(
        "[organization]\nmin_genre_size = 7\n",
        encoding="utf-8",
    )

    from vibechek import config as config_mod
    monkeypatch.setattr(config_mod, "CONFIG_FILE", json_path)
    monkeypatch.setattr(config_mod, "LEGACY_CONFIG_FILE", toml_path)

    loaded = VibechekConfig.load()
    assert loaded.organization.min_genre_size == 7

    out_path = loaded.save()
    assert out_path == json_path
    assert json_path.exists()

    # Verify it's JSON (not TOML).
    raw = json.loads(json_path.read_text(encoding="utf-8"))
    assert raw["organization"]["min_genre_size"] == 7


def test_load_prefers_json_over_legacy_toml(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """JSON beats TOML when both exist (migrate-once, JSON wins after)."""
    json_path = tmp_path / "config.json"
    toml_path = tmp_path / "config.toml"
    json_path.write_text(
        json.dumps({"analysis": {"workers": 2}}),
        encoding="utf-8",
    )
    toml_path.write_text(
        "[analysis]\nworkers = 99\n",
        encoding="utf-8",
    )

    from vibechek import config as config_mod
    monkeypatch.setattr(config_mod, "CONFIG_FILE", json_path)
    monkeypatch.setattr(config_mod, "LEGACY_CONFIG_FILE", toml_path)

    loaded = VibechekConfig.load()
    assert loaded.analysis.workers == 2


def test_module_exports_legacy_path() -> None:
    """Both CONFIG_FILE and LEGACY_CONFIG_FILE should be defined on the module
    so callers (and tests) can swap them. The conftest monkeypatches
    `CONFIG_FILE` to a test-local path, so we don't assert its suffix here."""
    from vibechek import config as config_mod
    assert hasattr(config_mod, "LEGACY_CONFIG_FILE")
    assert hasattr(config_mod, "CONFIG_FILE")
    # The unpatched module default ends with .json — reimport from disk would
    # show that, but the conftest's autouse fixture has already swapped it.
    # Asserting the legacy default at least catches accidental renames.
    assert str(config_mod.LEGACY_CONFIG_FILE).endswith(".toml")
