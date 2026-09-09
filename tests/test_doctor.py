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
    and the markdown surfaces it."""
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
# Engine-aware readiness + last-run sections
# ---------------------------------------------------------------------------


def test_engine_readiness_section_uses_saved_engine(monkeypatch) -> None:
    """doctor must probe the SAVED config's engine, not a hardcoded essentia_tf —
    else a native/onnx GUI gets a diagnostic describing the wrong engine.
    It must also use the FULL WSL probe: the quick probe can't see the
    per-distro engine venv, so it reported "not set up" on healthy boxes."""
    from vibechek import config as cfg_module
    from vibechek import preflight as _pf

    cfg_module.CONFIG_FILE.write_text(
        json.dumps({"analysis": {"inference_engine": "onnx"}}), encoding="utf-8"
    )

    seen: dict = {}

    def fake_pf(models_dir=None, *, quick_wsl=True, engine="essentia_tf"):
        seen["quick_wsl"] = quick_wsl
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
    assert seen["quick_wsl"] is False  # full probe — quick lies on healthy WSL
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


# ---------------------------------------------------------------------------
# The log tail is the report's one raw-text section. The module
# docstring promises no library contents and no user-private paths; the tail
# used to carry both, straight onto the clipboard via "Copy diagnostic".
# ---------------------------------------------------------------------------


def test_scrub_log_line_elides_track_names_and_home() -> None:
    home = str(Path.home())
    line = (
        "2026-09-05 10:00:01 WARNING duplicates: Could not hash "
        + home + r"\Music\Secret Set\Bicep - Glue (Extended Mix).mp3: denied"
    )
    scrubbed = doctor._scrub_log_line(line)

    assert home not in scrubbed
    assert "Bicep" not in scrubbed
    assert "Secret Set" not in scrubbed
    # The diagnostic value — which log, which failure — survives.
    assert "Could not hash" in scrubbed and "denied" in scrubbed
    assert ".mp3" in scrubbed


def test_scrub_log_line_elides_a_bare_track_filename() -> None:
    line = "2026-09-05 10:00:00 INFO organizer: No genre tag, skipping: Artist - Title.flac"
    scrubbed = doctor._scrub_log_line(line)
    assert "Artist - Title" not in scrubbed
    assert scrubbed.endswith("skipping: <track>.flac")


def test_scrub_log_line_leaves_non_library_lines_alone() -> None:
    line = "2026-09-05 10:00:00 INFO analyzer: loaded effnet-discogs.pb (86 MB)"
    assert doctor._scrub_log_line(line) == line


def test_log_tail_is_scrubbed_before_it_reaches_the_report(
    tmp_path: Path, monkeypatch
) -> None:
    from vibechek import logging_setup

    log_file = tmp_path / "vibechek.log"
    log_file.write_text(
        "2026-09-05 10:00:00 INFO organizer: No genre tag, skipping: Bicep - Glue.mp3\n"
        "2026-09-05 10:00:01 WARNING duplicates: Could not hash "
        + str(Path.home()) + r"\Music\Secret Set\track.mp3: denied" + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(logging_setup, "LOG_FILE", log_file, raising=True)

    report = doctor.build_report()
    joined = "\n".join(report.log_tail)
    assert "Bicep" not in joined
    assert "Secret Set" not in joined
    assert str(Path.home()) not in joined

    md = doctor.render_markdown(report)
    assert "Bicep" not in md
    assert str(Path.home()) not in md  # also covers the Config/Models paths


# ---------------------------------------------------------------------------
# Shell log tail. The Tauri shell writes `vibechek-shell.log` beside
# `vibechek.log`; on a windowed Windows build it is the ONLY record of a
# sidecar crash. The diagnostic carries it so a user can attach it without
# hunting for the data directory.
# ---------------------------------------------------------------------------


def _write_shell_log(monkeypatch, tmp_path: Path, text: str) -> Path:
    """Point the log dir at tmp_path and drop a shell log next to the app log."""
    from vibechek import logging_setup

    monkeypatch.setattr(logging_setup, "LOG_FILE", tmp_path / "vibechek.log", raising=True)
    shell_log = tmp_path / "vibechek-shell.log"
    shell_log.write_text(text, encoding="utf-8")
    return shell_log


def test_shell_log_tail_is_collected_and_rendered(tmp_path: Path, monkeypatch) -> None:
    _write_shell_log(
        monkeypatch,
        tmp_path,
        "2026-09-05T10:00:00Z [shell] Vibechek desktop shell 0.9.1 starting\n"
        "2026-09-05T10:00:03Z [shell] sidecar exited with code 3221225477\n",
    )

    report = doctor.build_report()
    assert len(report.shell_log_tail) == 2
    assert "3221225477" in report.shell_log_tail[-1]

    md = doctor.render_markdown(report)
    assert "## Shell log tail" in md
    assert "3221225477" in md


def test_shell_log_section_is_omitted_when_the_file_is_absent(
    tmp_path: Path, monkeypatch
) -> None:
    """CLI-only install: no shell has ever run, so an empty block would read as
    a missing file the user is supposed to go and find."""
    from vibechek import logging_setup

    monkeypatch.setattr(logging_setup, "LOG_FILE", tmp_path / "vibechek.log", raising=True)
    report = doctor.build_report()
    assert report.shell_log_tail == []
    assert "## Shell log tail" not in doctor.render_markdown(report)


def test_shell_log_tail_is_capped_to_the_requested_lines(
    tmp_path: Path, monkeypatch
) -> None:
    _write_shell_log(
        monkeypatch, tmp_path, "".join(f"line {i}\n" for i in range(200))
    )
    report = doctor.build_report()
    assert len(report.shell_log_tail) == 30
    assert report.shell_log_tail[0] == "line 170"
    assert report.shell_log_tail[-1] == "line 199"


def test_shell_log_tail_is_scrubbed_like_the_main_log(
    tmp_path: Path, monkeypatch
) -> None:
    """The shell relays the sidecar's stderr, so this file carries track paths
    and the user's home dir too — same paste-safety promise as the app log."""
    _write_shell_log(
        monkeypatch,
        tmp_path,
        "2026-09-05T10:00:00Z [shell] stderr: No genre tag, skipping: Bicep - Glue.mp3\n"
        "2026-09-05T10:00:01Z [shell] cwd " + str(Path.home()) + "\\Music\n",
    )

    report = doctor.build_report()
    joined = "\n".join(report.shell_log_tail)
    assert "Bicep" not in joined
    assert str(Path.home()) not in joined
    assert ".mp3" in joined  # the diagnostic value survives

    assert "Bicep" not in doctor.render_markdown(report)


def test_unreadable_shell_log_degrades_to_an_empty_tail(
    tmp_path: Path, monkeypatch
) -> None:
    """Best-effort like every other probe: a locked/undecodable shell log must
    not take the whole diagnostic down."""
    from vibechek import logging_setup

    monkeypatch.setattr(logging_setup, "LOG_FILE", tmp_path / "vibechek.log", raising=True)
    shell_log = tmp_path / "vibechek-shell.log"
    shell_log.mkdir()  # a directory where a file is expected — open() raises

    report = doctor.build_report()
    assert report.shell_log_tail == []
    assert "# Vibechek diagnostic report" in doctor.render_markdown(report)
