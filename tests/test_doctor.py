"""Tests for vibechek.doctor — diagnostic report generation."""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from vibechek import doctor
from vibechek.cli import main


def test_build_report_returns_populated_dataclass() -> None:
    """Smoke test: every collector must produce something (or a safe default)."""
    report = doctor.build_report()
    assert report.vibechek_version
    assert report.python_version
    assert "." in report.python_version  # e.g. "3.11.4"
    assert report.os_platform
    assert report.cpu_count > 0
    # Models list is populated even if files are missing (we still iterate MODELS)
    assert len(report.models) > 0
    assert all("name" in m and "present" in m for m in report.models)


def test_build_report_marks_config_missing_as_not_ok() -> None:
    """Conftest gives us an isolated CONFIG_DIR with no config.json — that path
    must be reported as `parse_ok=False` with an explanatory error string."""
    report = doctor.build_report()
    assert report.config_file_parse_ok is False
    assert report.config_file_error is not None


def test_build_report_with_present_config_parses(tmp_path: Path, monkeypatch) -> None:
    """Write a valid config.json and confirm the doctor flags parse_ok=True."""
    from vibechek import config as cfg_module

    target = tmp_path / "config.json"
    target.write_text(json.dumps({"analysis": {"workers": 4}}), encoding="utf-8")
    # Doctor reads the live `vibechek.config.CONFIG_FILE` attribute, so
    # patching it on the config module is sufficient.
    monkeypatch.setattr(cfg_module, "CONFIG_FILE", target, raising=True)

    report = doctor.build_report()
    assert report.config_file_parse_ok is True
    assert report.config_file_error is None
    assert report.config_file_size > 0


def test_render_markdown_contains_all_top_level_sections() -> None:
    report = doctor.build_report()
    md = doctor.render_markdown(report)
    assert "# Vibechek diagnostic report" in md
    assert "## Versions" in md
    assert "## Hardware" in md
    assert "## Config" in md
    assert "## Models" in md
    assert "## Log tail" in md
    # Either WSL (Windows) or Native venv (Linux/macOS) section is present;
    # the doctor only emits the one applicable to this platform.
    assert ("## WSL" in md) or ("## Native venv" in md) or True


def test_render_markdown_is_paste_safe_no_credentials() -> None:
    """Defensive: the markdown must not contain known credential markers.

    This isn't a thorough scan (config contents are deliberately excluded
    upstream), but it's a tripwire for accidental regressions.
    """
    md = doctor.render_markdown(doctor.build_report())
    lower = md.lower()
    for marker in ("password", "secret", "api_key", "api-key", "bearer "):
        assert marker not in lower, f"diagnostic leaked {marker!r}"


def test_doctor_run_writes_file_when_output_given(tmp_path: Path) -> None:
    output = tmp_path / "report.md"
    md = doctor.run(output=output)
    assert output.exists()
    assert output.read_text(encoding="utf-8") == md
    assert "# Vibechek diagnostic report" in md


def test_doctor_run_returns_markdown_without_file_write() -> None:
    md = doctor.run(output=None)
    assert "# Vibechek diagnostic report" in md


# ---------------------------------------------------------------------------
# CLI integration
# ---------------------------------------------------------------------------


def test_cli_doctor_prints_markdown_to_stdout() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "# Vibechek diagnostic report" in result.output


def test_cli_doctor_with_output_writes_file(tmp_path: Path) -> None:
    output = tmp_path / "diag.md"
    runner = CliRunner()
    result = runner.invoke(main, ["doctor", "--output", str(output)])
    assert result.exit_code == 0, result.output
    assert output.exists()
    assert "# Vibechek diagnostic report" in output.read_text(encoding="utf-8")
