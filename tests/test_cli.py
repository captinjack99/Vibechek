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


def test_export_corrupt_json_gives_clean_error(tmp_path: Path) -> None:
    """A corrupt/truncated analysis.json must yield a clean Click error, not a
    raw JSONDecodeError traceback. `click.Path(exists=True)` only checks the
    file exists — an interrupted `analyze` write leaves invalid JSON."""
    bad = tmp_path / "junk.json"
    bad.write_text("{ this is not valid json", encoding="utf-8")
    result = CliRunner().invoke(main, ["export", str(bad), "--format", "csv"])
    assert result.exit_code != 0
    assert "not a valid analysis JSON" in result.output
    assert not isinstance(result.exception, json.JSONDecodeError)


def test_organize_corrupt_json_gives_clean_error(tmp_path: Path) -> None:
    bad = tmp_path / "junk.json"
    bad.write_text("{ this is not valid json", encoding="utf-8")
    result = CliRunner().invoke(main, ["organize", str(bad), "--dry-run"])
    assert result.exit_code != 0
    assert "not a valid analysis JSON" in result.output
    assert not isinstance(result.exception, json.JSONDecodeError)


def test_organize_empty_analysis_gives_clean_error(tmp_path: Path) -> None:
    """Organizing an analysis with no tracks (and no --target-root) can't infer
    a base dir → plan_organization raises ValueError. The CLI must convert that
    to a clean ClickException, not leak the ValueError traceback (the RPC path
    already returns INVALID_PARAMS here)."""
    empty = tmp_path / "empty.json"
    empty.write_text(json.dumps({"tracks": []}), encoding="utf-8")
    result = CliRunner().invoke(main, ["organize", str(empty), "--dry-run"])
    assert result.exit_code != 0
    assert "No tracks in analysis" in result.output
    assert not isinstance(result.exception, ValueError)


def test_preflight_quick_mode_skips_distro_probes(monkeypatch) -> None:
    """`vibechek preflight --quick` skips the slow per-distro WSL probe."""
    from vibechek import preflight as _preflight_module
    from vibechek import wsl

    # The CLI imports `preflight()` from vibechek.preflight, which in turn
    # imports `detect_wsl` at module load. Monkeypatch the name in the
    # preflight module's namespace — that's where the call resolves.
    probe_calls: list[bool] = []

    def fake_detect(quick: bool = False, venv_subdir: str = "venv") -> wsl.WSLStatus:
        probe_calls.append(quick)
        return wsl.WSLStatus(is_windows=False, wsl_available=False, wsl_feature_enabled=False)

    monkeypatch.setattr(_preflight_module, "detect_wsl", fake_detect)

    runner = CliRunner()
    result = runner.invoke(main, ["preflight", "--quick"])
    assert result.exit_code in (0, 1), result.output
    assert "Vibechek preflight" in result.output
    assert any(call is True for call in probe_calls), \
        f"Expected detect_wsl(quick=True) call, got: {probe_calls}"


def test_analyze_directory_uses_full_wsl_probe(monkeypatch, tmp_path) -> None:
    """Regression: `analyze_directory` must do a non-quick WSL probe.

    Previously, analyze called `preflight()` which used quick=True WSL probe
    (skips per-distro essentia checks). Result: on Windows with essentia
    installed inside WSL, analyze would false-fail with "essentia-tensorflow
    is not installed (native, in WSL, or in the managed venv)" because the
    quick probe couldn't see the distro contents.
    """
    from vibechek import analyzer, wsl
    from vibechek import preflight as _preflight_module

    probe_calls: list[bool] = []

    def fake_detect(quick: bool = False, venv_subdir: str = "venv") -> wsl.WSLStatus:
        probe_calls.append(quick)
        return wsl.WSLStatus(is_windows=False, wsl_available=False, wsl_feature_enabled=False)

    monkeypatch.setattr(_preflight_module, "detect_wsl", fake_detect)

    # Need a non-empty library; analyzer short-circuits on total==0 before
    # the preflight check.
    library = tmp_path / "lib"
    library.mkdir()
    (library / "fake.mp3").write_text("not real audio")
    try:
        analyzer.analyze_directory(library)
    except RuntimeError:
        pass  # expected — preflight will fail because no engine + no models
    except Exception:
        pass  # any other failure also fine; we only check the probe call

    assert any(call is False for call in probe_calls), (
        f"analyze_directory must call detect_wsl(quick=False) so it sees "
        f"essentia inside WSL distros. Got: {probe_calls}"
    )


def test_preflight_full_mode_does_distro_probes(monkeypatch) -> None:
    """`vibechek preflight` (default) does the full probe — quick=False."""
    from vibechek import preflight as _preflight_module
    from vibechek import wsl

    probe_calls: list[bool] = []

    def fake_detect(quick: bool = False, venv_subdir: str = "venv") -> wsl.WSLStatus:
        probe_calls.append(quick)
        return wsl.WSLStatus(is_windows=False, wsl_available=False, wsl_feature_enabled=False)

    monkeypatch.setattr(_preflight_module, "detect_wsl", fake_detect)

    runner = CliRunner()
    result = runner.invoke(main, ["preflight"])
    assert result.exit_code in (0, 1)
    # Default is --full, which means quick=False
    assert any(call is False for call in probe_calls), \
        f"Expected detect_wsl(quick=False) call, got: {probe_calls}"
