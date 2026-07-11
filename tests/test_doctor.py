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
    # the doctor only emits the one applicable to this platform. (A trailing
    # `or True` used to make this assertion unable to fail.)
    assert ("## WSL" in md) or ("## Native venv" in md)


def test_model_integrity_unverified_surfaced_when_sha_table_empty(monkeypatch) -> None:
    """With an empty MODEL_SHA256 table the report flags integrity unverified
    and the markdown surfaces it (audit: INFO -> surface)."""
    from vibechek import analyzer

    monkeypatch.setattr(analyzer, "MODEL_SHA256", {}, raising=True)
    report = doctor.build_report()
    assert report.model_integrity_verified is False
    md = doctor.render_markdown(report)
    assert "Integrity: **unverified**" in md


def test_model_integrity_verified_when_sha_table_populated(monkeypatch) -> None:
    from vibechek import analyzer

    monkeypatch.setattr(
        analyzer,
        "MODEL_SHA256",
        {"effnet": {"pb": "ab" * 32, "json": "cd" * 32}},
        raising=True,
    )
    report = doctor.build_report()
    assert report.model_integrity_verified is True
    md = doctor.render_markdown(report)
    assert "Integrity: **verified**" in md


# ---------------------------------------------------------------------------
# Engine-aware readiness + last-run sections (WP9)
# ---------------------------------------------------------------------------


def test_engine_readiness_section_uses_saved_engine(monkeypatch) -> None:
    """doctor must probe the SAVED config's engine, not a hardcoded essentia_tf —
    else a native/onnx GUI gets a diagnostic describing the wrong engine."""
    from vibechek import config as cfg_module
    from vibechek import preflight as _pf

    cfg_module.CONFIG_FILE.write_text(
        json.dumps({"analysis": {"inference_engine": "onnx"}}), encoding="utf-8"
    )

    def fake_pf(models_dir=None, *, quick_wsl=True, engine="essentia_tf"):
        return _pf.PreflightResult(
            ready=False,
            essentia=_pf.EssentiaCheck(installed=True),
            models=_pf.ModelsCheck(models_dir="/x", found=["a"], missing=["b"]),
            platform="test",
            engine=engine,
            essentia_usable=False,
            analyze_via=None,
        )

    monkeypatch.setattr(_pf, "preflight", fake_pf)

    report = doctor.build_report()
    er = report.engine_readiness
    assert er is not None
    assert er["engine"] == "onnx"
    assert er["ready"] is False
    assert er["models_missing"] == 1
    assert er["reasons_not_ready"]  # populated (missing engine + missing model)

    md = doctor.render_markdown(report)
    assert "## Engine readiness" in md
    assert "`onnx`" in md


def test_last_run_section_reads_run_history() -> None:
    from vibechek import logging_setup

    logging_setup.append_run_summary({
        "ts": "2026-07-11T10:00:00+0000",
        "engine": "native",
        "genre_classifier": "discogs",
        "requested_workers": 8,
        "effective_workers": 4,
        "gpu_workers": 0,
        "cpu_workers": 4,
        "gpu_reason": "no GPU registered",
        "analyzed": 100,
        "errors": 3,
        "total": 103,
        "duration_sec": 42.5,
        "warnings": {"model_degradation_warning": "mood head failed to load"},
    })

    report = doctor.build_report()
    assert report.last_run is not None
    assert report.last_run["engine"] == "native"

    md = doctor.render_markdown(report)
    assert "## Last analyze run" in md
    assert "`native`" in md
    assert "no GPU registered" in md
    assert "mood head failed to load" in md


def test_last_run_section_empty_when_no_runs() -> None:
    report = doctor.build_report()
    assert report.last_run is None
    md = doctor.render_markdown(report)
    assert "## Last analyze run" in md
    assert "no analyze run recorded yet" in md


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
