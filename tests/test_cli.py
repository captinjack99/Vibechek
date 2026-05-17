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
