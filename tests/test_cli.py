"""Tests for vibechek.cli — smoke tests against the Click runner."""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from vibechek.cli import main


def test_top_level_help() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["--help"])
    assert result.exit_code == 0
    for cmd in ("analyze", "tag", "dedupe", "organize", "backup-tags", "restore-tags", "route"):
        assert cmd in result.output


def test_version_command() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["--version"])
    assert result.exit_code == 0
    assert "vibechek" in result.output.lower()


def test_dedupe_runs_without_chromaprint(tiny_library: Path, tmp_path: Path) -> None:
    runner = CliRunner()
    output = tmp_path / "dupes.json"
    result = runner.invoke(
        main,
        ["dedupe", str(tiny_library), "--no-chromaprint", "-o", str(output)],
    )
    assert result.exit_code == 0, result.output
    assert output.exists()
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["summary"]["exact_duplicate_files"] == 1  # track3 dup


def test_organize_dry_run(synthetic_analysis: dict, tmp_path: Path) -> None:
    analysis_file = tmp_path / "analysis.json"
    analysis_file.write_text(json.dumps(synthetic_analysis), encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(
        main,
        ["organize", str(analysis_file), "--dry-run", "--min-genre-size", "3"],
    )
    assert result.exit_code == 0, result.output
    assert "moves planned" in result.output


def test_preflight_quick_mode_skips_distro_probes(monkeypatch) -> None:
    """`vibechek preflight --quick` skips the slow per-distro WSL probe."""
    from vibechek import wsl

    # Drop a sentinel: detect_wsl(quick=True) returns an empty status fast.
    probe_calls: list[bool] = []

    def fake_detect(quick: bool = False) -> wsl.WSLStatus:
        probe_calls.append(quick)
        return wsl.WSLStatus(is_windows=False, wsl_available=False, wsl_feature_enabled=False)

    monkeypatch.setattr(wsl, "detect_wsl", fake_detect)
    # The CLI does a `from vibechek.wsl import detect_wsl` inside the command
    # function, so monkeypatch on the source module is enough — Click invokes
    # the function fresh each call.

    runner = CliRunner()
    result = runner.invoke(main, ["preflight", "--quick"])
    # Exit may be 0 or 1 depending on model state; we care that it ran.
    assert result.exit_code in (0, 1), result.output
    assert "Vibechek preflight" in result.output
    assert any(call is True for call in probe_calls), \
        f"Expected detect_wsl(quick=True) call, got: {probe_calls}"


def test_preflight_full_mode_does_distro_probes(monkeypatch) -> None:
    """`vibechek preflight` (default) does the full probe — quick=False."""
    from vibechek import wsl

    probe_calls: list[bool] = []

    def fake_detect(quick: bool = False) -> wsl.WSLStatus:
        probe_calls.append(quick)
        return wsl.WSLStatus(is_windows=False, wsl_available=False, wsl_feature_enabled=False)

    monkeypatch.setattr(wsl, "detect_wsl", fake_detect)

    runner = CliRunner()
    result = runner.invoke(main, ["preflight"])
    assert result.exit_code in (0, 1)
    # Default is --full, which means quick=False
    assert any(call is False for call in probe_calls), \
        f"Expected detect_wsl(quick=False) call, got: {probe_calls}"
