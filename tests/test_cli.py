"""Tests for vibechek.cli — smoke tests against the Click runner."""

from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import pytest
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


def test_selftest_native_registered_and_clean() -> None:
    """`selftest-native` is the frozen-build native gate.

    It's hidden (not in --help) but must be registered and must NEVER crash with
    a raw traceback: it either passes (essentia bundled/importable) or exits 1
    with a clean message (essentia absent — the normal [dev]/CI case, since
    essentia isn't a test dependency).
    """
    assert "selftest-native" in main.commands  # hidden, so absent from --help
    runner = CliRunner()
    result = runner.invoke(main, ["selftest-native"])
    assert result.exit_code in (0, 1), result.output
    if result.exit_code == 1:
        assert "native self-test FAILED" in result.output
    # The handler converts any failure into a clean Click exit — nothing leaks.
    assert result.exception is None or isinstance(result.exception, SystemExit)


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


def test_preflight_engine_flag_selects_engine(monkeypatch) -> None:
    """`vibechek preflight --engine onnx` must check the ONNX environment, not the
    hardcoded essentia_tf — matching the "anything the GUI can do, the CLI can do"
    contract for a Windows GUI whose default engine is native."""
    from vibechek import preflight as _preflight_module

    captured: dict[str, str] = {}

    def fake_preflight(models_dir=None, *, quick_wsl=True, engine="essentia_tf"):
        captured["engine"] = engine
        return _preflight_module.PreflightResult(
            ready=True,
            essentia=_preflight_module.EssentiaCheck(installed=True, version="2.1"),
            models=_preflight_module.ModelsCheck(models_dir="/x"),
            platform="test-platform",
            engine=engine,
            essentia_usable=True,
            analyze_via="native",
        )

    monkeypatch.setattr(_preflight_module, "preflight", fake_preflight)

    runner = CliRunner()
    result = runner.invoke(main, ["preflight", "--engine", "onnx", "--quick"])
    assert result.exit_code == 0, result.output
    assert captured["engine"] == "onnx"


def test_preflight_no_engine_flag_resolves_from_config(monkeypatch) -> None:
    """Without --engine the command resolves the engine the same way `analyze`
    does (saved config → platform default), not a hardcoded essentia_tf."""
    from vibechek import cli as _cli
    from vibechek import preflight as _preflight_module

    monkeypatch.setattr(_cli, "_resolve_default_engine", lambda: "native")
    captured: dict[str, str] = {}

    def fake_preflight(models_dir=None, *, quick_wsl=True, engine="essentia_tf"):
        captured["engine"] = engine
        return _preflight_module.PreflightResult(
            ready=True,
            essentia=_preflight_module.EssentiaCheck(installed=True),
            models=_preflight_module.ModelsCheck(models_dir="/x"),
            platform="test-platform",
            engine=engine,
            essentia_usable=True,
            analyze_via="native",
        )

    monkeypatch.setattr(_preflight_module, "preflight", fake_preflight)

    runner = CliRunner()
    result = runner.invoke(main, ["preflight", "--quick"])
    assert result.exit_code == 0, result.output
    assert captured["engine"] == "native"


# ---------------------------------------------------------------------------
# journal._read_journal — a stray non-dict line must be skipped, not crash the
# whole undo list / revert (bug: AttributeError/TypeError on valid-but-non-dict
# JSON lines like `["stray","array"]` or `42`).
# ---------------------------------------------------------------------------


def test_read_journal_skips_non_dict_lines(tmp_path: Path) -> None:
    """A line that's valid JSON but not an object (stray array/scalar from a
    partial/hand-mangled write) must be skipped, matching the documented
    'skip malformed lines' contract — not raise AttributeError/TypeError."""
    from vibechek import journal

    jf = tmp_path / "j.jsonl"
    jf.write_text(
        '{"kind":"organize","started_at":1000,"root":"/x"}\n'
        '["stray","array"]\n'
        '42\n'
        '"a bare string"\n'
        '{"action":"move","src":"/a","dst":"/b"}\n',
        encoding="utf-8",
    )
    header, entries = journal._read_journal(jf)
    assert header["kind"] == "organize"
    assert [e["action"] for e in entries] == ["move"]
    assert entries[0]["src"] == "/a"


def test_journals_command_tolerates_corrupt_journal(tmp_path: Path, monkeypatch) -> None:
    """One corrupt journal (a stray non-dict line) must not poison the whole
    `vibechek journals` list / GUI undo list with a raw traceback."""
    from vibechek import journal

    jdir = tmp_path / "journals"
    jdir.mkdir()
    monkeypatch.setattr(journal, "JOURNALS_DIR", jdir)
    # A good journal and a poisoned one in the same dir.
    (jdir / "20240101-000000-organize.jsonl").write_text(
        '{"kind":"organize","started_at":1000,"root":"/lib"}\n'
        '{"action":"move","src":"/lib/a.mp3","dst":"/lib/House/a.mp3"}\n',
        encoding="utf-8",
    )
    (jdir / "20240102-000000-organize.jsonl").write_text(
        '{"kind":"organize","started_at":2000,"root":"/lib"}\n'
        '["stray","array"]\n',
        encoding="utf-8",
    )
    result = CliRunner().invoke(main, ["journals"])
    assert result.exit_code == 0, result.output
    assert not isinstance(result.exception, (AttributeError, TypeError))
    assert "organize" in result.output


def test_revert_skips_non_dict_journal_line(tmp_path: Path) -> None:
    """`vibechek revert` on a real journal containing a stray non-dict line
    must revert the good moves and skip the bad line, not crash."""
    lib = tmp_path / "lib"
    (lib / "House").mkdir(parents=True)
    dst = lib / "House" / "a.mp3"
    dst.write_bytes(b"audio")
    src = lib / "a.mp3"

    jf = tmp_path / "20240101-000000-organize.jsonl"
    jf.write_text(
        f'{{"kind":"organize","started_at":1000,"root":"{lib.as_posix()}"}}\n'
        '["stray","array"]\n'
        f'{{"action":"move","src":"{src.as_posix()}","dst":"{dst.as_posix()}"}}\n',
        encoding="utf-8",
    )
    result = CliRunner().invoke(main, ["revert", str(jf)])
    assert result.exit_code == 0, result.output
    assert not isinstance(result.exception, (AttributeError, TypeError))
    assert src.exists() and not dst.exists()


# ---------------------------------------------------------------------------
# revert — pointing at a non-journal / corrupt file must give a clean error and
# a non-zero exit, NOT silently "succeed" (Reverted 0, exit 0).
# ---------------------------------------------------------------------------


def test_revert_corrupt_file_gives_clean_error(tmp_path: Path) -> None:
    bad = tmp_path / "corrupt.json"
    bad.write_text("not json {", encoding="utf-8")
    result = CliRunner().invoke(main, ["revert", str(bad)])
    assert result.exit_code != 0
    assert "isn't a Vibechek undo record" in result.output
    assert "Reverted 0" not in result.output
    assert not isinstance(result.exception, ValueError)


def test_revert_wrong_shape_file_gives_clean_error(tmp_path: Path) -> None:
    """A valid-JSON file that isn't a journal (e.g. an analysis.json) must be
    rejected, not reported as a successful Reverted 0."""
    notjournal = tmp_path / "analysis.json"
    notjournal.write_text(json.dumps({"tracks": []}), encoding="utf-8")
    result = CliRunner().invoke(main, ["revert", str(notjournal)])
    assert result.exit_code != 0
    assert "isn't a Vibechek undo record" in result.output
    assert "Reverted 0" not in result.output


def test_revert_valid_empty_journal_still_works(tmp_path: Path, monkeypatch) -> None:
    """A real but empty/fully-reverted journal (valid header, no move entries)
    must NOT be misclassified as a non-journal — it reverts 0 cleanly."""
    from vibechek import journal

    jdir = tmp_path / "journals"
    monkeypatch.setattr(journal, "JOURNALS_DIR", jdir)
    j = journal.start_journal(journal.KIND_ORGANIZE, root=str(tmp_path))
    j.close()  # header only, no moves
    result = CliRunner().invoke(main, ["revert", str(j.path)])
    assert result.exit_code == 0, result.output
    assert "Reverted 0" in result.output


# ---------------------------------------------------------------------------
# restore-tags — corrupt/empty/wrong-shape backups must yield a clean Click
# error, not a raw ValueError traceback.
# ---------------------------------------------------------------------------


def test_restore_tags_corrupt_backup_gives_clean_error(tmp_path: Path) -> None:
    bad = tmp_path / "corrupt.json"
    bad.write_text("{ bad\n", encoding="utf-8")
    result = CliRunner().invoke(main, ["restore-tags", str(bad)])
    assert result.exit_code != 0
    # The friendly tagger message survives; the raw ValueError does not leak.
    assert not isinstance(result.exception, ValueError)
    assert "Traceback" not in result.output


def test_restore_tags_empty_backup_gives_clean_error(tmp_path: Path) -> None:
    empty = tmp_path / "empty.json"
    empty.write_text("", encoding="utf-8")
    result = CliRunner().invoke(main, ["restore-tags", str(empty)])
    assert result.exit_code != 0
    assert not isinstance(result.exception, ValueError)
    assert "Traceback" not in result.output


# ---------------------------------------------------------------------------
# export --format m3u8 — non-dict track entries must be skipped, matching the
# CSV branch, not raise AttributeError.
# ---------------------------------------------------------------------------


def test_export_m3u8_skips_non_dict_entries(tmp_path: Path) -> None:
    weird = tmp_path / "weird.json"
    weird.write_text(
        json.dumps({"tracks": [123, "s", {"path": "/x.mp3"}, {"nopath": 1}]}),
        encoding="utf-8",
    )
    out = tmp_path / "weird.m3u8"
    result = CliRunner().invoke(main, ["export", str(weird), "--format", "m3u8", "-o", str(out)])
    assert result.exit_code == 0, result.output
    assert not isinstance(result.exception, AttributeError)
    assert out.read_text(encoding="utf-8").strip().splitlines() == ["/x.mp3"]


def test_export_m3u8_bare_list_of_strings(tmp_path: Path) -> None:
    """A bare list-of-strings file (which CSV handles) must not crash m3u8."""
    weird = tmp_path / "list.json"
    weird.write_text(json.dumps(["a", "b"]), encoding="utf-8")
    out = tmp_path / "list.m3u8"
    result = CliRunner().invoke(main, ["export", str(weird), "--format", "m3u8", "-o", str(out)])
    assert result.exit_code == 0, result.output
    assert not isinstance(result.exception, AttributeError)


# ---------------------------------------------------------------------------
# dedupe — disabling both detection methods must be rejected up front, not
# silently report "0 groups" without checking anything.
# ---------------------------------------------------------------------------


def test_dedupe_both_methods_disabled_is_rejected(tiny_library: Path, tmp_path: Path) -> None:
    out = tmp_path / "dd.json"
    result = CliRunner().invoke(
        main,
        ["dedupe", str(tiny_library), "--no-md5", "--no-chromaprint", "-o", str(out)],
    )
    assert result.exit_code != 0
    assert "no detection method would run" in result.output
    assert "Scan done" not in result.output
    # A genuine duplicate-free claim must never be written when nothing ran.
    assert not out.exists()


# ---------------------------------------------------------------------------
# analyze — a directory with zero audio files must surface the "did nothing"
# outcome (yellow warning), not a green "Done. → out.json".
# ---------------------------------------------------------------------------


def test_analyze_empty_dir_warns_instead_of_false_done(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    out = tmp_path / "out.json"
    result = CliRunner().invoke(main, ["analyze", str(empty), "-o", str(out)])
    assert result.exit_code == 0, result.output
    assert "No audio files found" in result.output
    # The misleading green success line that names a file is gone.
    assert "Done. Analyzed" not in result.output


def test_analyze_cli_appends_run_history(monkeypatch, tmp_path: Path) -> None:
    """The CLI analyze must append to the durable run history like the RPC
    path does — doctor's "last analyze run" was blind to CLI runs (the
    0.8.0 changelog promised "every analyze appends")."""
    from vibechek import analyzer, logging_setup

    def fake_analyze_directory(path, *, config, on_progress=None, output_path=None,
                               skip=0, limit=None, skip_paths=None):
        return {
            "summary": {"analyzed": 2, "errors": 0, "total_files": 2},
            "run_meta": {
                "engine": "onnx",
                "genre_classifier": "clap",
                "requested_workers": 4,
                "effective_workers": 2,
                "gpu_workers": 1,
                "cpu_workers": 1,
                "gpu_reason": None,
            },
            "tracks": [],
        }

    monkeypatch.setattr(analyzer, "analyze_directory", fake_analyze_directory)

    lib = tmp_path / "lib"
    lib.mkdir()
    out = tmp_path / "out.json"
    result = CliRunner().invoke(main, ["analyze", str(lib), "-o", str(out)])
    assert result.exit_code == 0, result.output

    history = logging_setup.read_run_history()
    assert history, "CLI analyze appended nothing to the run history"
    last = history[-1]
    assert last["engine"] == "onnx"
    assert last["genre_classifier"] == "clap"
    assert last["effective_workers"] == 2
    assert last["gpu_workers"] == 1
    assert last["analyzed"] == 2
    assert isinstance(last["duration_sec"], (int, float))


def test_tag_wrong_shape_json_no_traceback(tmp_path: Path) -> None:
    """`tag` on a bare-list / wrong-shape analysis must NOT dump a Python
    traceback (regression: apply_ml_tags did analysis_data.get on a list)."""
    bad = tmp_path / "bare.json"
    bad.write_text(json.dumps(["just", "strings"]), encoding="utf-8")
    result = CliRunner().invoke(main, ["tag", str(bad)])
    assert "Traceback" not in result.output
    assert "AttributeError" not in result.output

    s = tmp_path / "str.json"
    s.write_text(json.dumps("a string"), encoding="utf-8")
    result2 = CliRunner().invoke(main, ["tag", str(s)])
    assert result2.exit_code != 0
    assert "Traceback" not in result2.output
    assert "not a valid analysis file" in result2.output


def test_organize_wrong_shape_json_no_traceback(tmp_path: Path) -> None:
    """`organize` on a bare-list analysis must give a clean error, not a
    TypeError traceback from tracks[0]['path']."""
    bad = tmp_path / "bare.json"
    bad.write_text(json.dumps(["just", "strings"]), encoding="utf-8")
    result = CliRunner().invoke(main, ["organize", str(bad), "--dry-run"])
    assert result.exit_code != 0
    assert "Traceback" not in result.output
    assert "TypeError" not in result.output


# ---------------------------------------------------------------------------
# Exit status of the mutating commands — a batch where every operation failed
# must not exit 0 (`vibechek organize a.json && rm a.json` used to proceed).
# ---------------------------------------------------------------------------


def test_organize_exits_nonzero_when_every_move_fails(
    synthetic_analysis: dict, tmp_path: Path
) -> None:
    """A read-only target puts every track in stats.errors and moves nothing."""
    analysis_file = tmp_path / "analysis.json"
    analysis_file.write_text(json.dumps(synthetic_analysis), encoding="utf-8")

    def boom(*_a, **_kw):
        raise OSError(30, "Read-only file system")

    with mock.patch("vibechek.organizer.shutil.move", boom):
        result = CliRunner().invoke(
            main,
            ["organize", str(analysis_file), "--min-genre-size", "1",
             "--target-root", str(tmp_path / "library")],
        )
    assert result.exit_code != 0, result.output
    assert "Nothing was moved" in result.output
    # The green "Done." must not front a run in which nothing worked.
    assert "Done (with errors)." in result.output


def test_organize_error_list_says_how_many_were_withheld(
    synthetic_analysis: dict, tmp_path: Path
) -> None:
    """Truncating to 5 with no remainder count hid the scale of the failure."""
    analysis_file = tmp_path / "analysis.json"
    analysis_file.write_text(json.dumps(synthetic_analysis), encoding="utf-8")

    def boom(*_a, **_kw):
        raise OSError(30, "Read-only file system")

    with mock.patch("vibechek.organizer.shutil.move", boom):
        result = CliRunner().invoke(
            main,
            ["organize", str(analysis_file), "--min-genre-size", "1",
             "--target-root", str(tmp_path / "library")],
        )
    assert "and 2 more" in result.output  # 7 tracks, 5 shown


def test_restore_tags_exits_nonzero_when_nothing_restored(tmp_path: Path) -> None:
    """Every file failing to restore is a failed run, not a green one."""
    from vibechek.tagger import RestoreStats

    backup = tmp_path / "tags_backup.json"
    backup.write_text(json.dumps({"files": {}}), encoding="utf-8")

    stats = RestoreStats(total=3, restored=0, errors=["a: boom", "b: boom", "c: boom"])
    with mock.patch("vibechek.tagger.restore_tags", return_value=stats):
        result = CliRunner().invoke(main, ["restore-tags", str(backup)])
    assert result.exit_code != 0, result.output
    assert "Nothing was restored" in result.output


# ---------------------------------------------------------------------------
# logging — a CLI run must write to the same rotating log the sidecar and
# `doctor`'s log tail read (the group body never called configure()).
# ---------------------------------------------------------------------------


def test_cli_configures_logging(tmp_path: Path) -> None:
    """`vibechek <anything>` installs the file handler, so log.info is recorded."""
    import logging

    from vibechek import logging_setup

    assert not logging_setup.LOG_FILE.exists()  # conftest points this at a tmp dir
    result = CliRunner().invoke(main, ["journals"])
    assert result.exit_code == 0, result.output

    logging.getLogger("vibechek.test-probe").warning("probe-line")
    assert logging_setup.LOG_FILE.exists()
    assert "probe-line" in logging_setup.LOG_FILE.read_text(encoding="utf-8")


def test_cli_still_runs_when_the_log_dir_cannot_be_created(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Logging is a diagnostic, not a precondition. `configure()` now runs in the
    click group body, so an OSError there (read-only data dir, permissions) would
    otherwise traceback out of EVERY command — including `doctor`, the one a user
    runs precisely because their install is broken."""
    from vibechek import logging_setup

    # A file where the log DIRECTORY should be: mkdir(parents=True) raises
    # NotADirectoryError/FileExistsError (both OSError) on every platform.
    blocker = tmp_path / "not-a-dir"
    blocker.write_bytes(b"x")
    monkeypatch.setattr(logging_setup, "LOG_DIR", blocker / "logs")
    monkeypatch.setattr(logging_setup, "LOG_FILE", blocker / "logs" / "vibechek.log")
    monkeypatch.setattr(logging_setup, "_configured", False)

    result = CliRunner().invoke(main, ["journals"])

    assert result.exit_code == 0, result.output
    assert not isinstance(result.exception, OSError)
    # click only captures stderr SEPARATELY from 8.2 onward; pyproject pins
    # `click>=8.1`, and on 8.1 `CliRunner` defaults to mix_stderr=True and
    # `result.stderr` raises ValueError. Read whichever stream this click gives
    # us so the test asserts the warning, not the click version.
    try:
        err = result.stderr
    except ValueError:  # pragma: no cover - only on click < 8.2
        err = ""
    assert "could not open the log file" in (err or "") + result.output


def test_route_exits_nonzero_when_every_copy_fails(tmp_path: Path) -> None:
    """Sibling of the organize/tag case: `route` counts errors and ignored them."""
    staging = tmp_path / "staging"
    staging.mkdir()
    lib = tmp_path / "lib"
    lib.mkdir()

    summary = {"copied": 0, "skipped_no_genre": 0, "skipped_exists": 0,
               "routed_to_other": 0, "errors": 4}
    with mock.patch("vibechek.organizer.route_new_tracks", return_value=summary):
        result = CliRunner().invoke(main, ["route", str(staging), str(lib)])
    assert result.exit_code != 0, result.output
    assert "Nothing was copied" in result.output


def test_organize_target_root_help_describes_the_real_default() -> None:
    """The help documented "first track's parent" — the resolution 68c59df
    deleted as destructive. plan_organization now uses the commonpath of every
    analysed track's parent, so the old text pointed users at the wrong lever."""
    result = CliRunner().invoke(main, ["organize", "--help"])
    assert result.exit_code == 0, result.output
    assert "first track's parent" not in result.output
    assert "common parent folder" in result.output


# ---------------------------------------------------------------------------
# tag — the summary line has to account for every track
# ---------------------------------------------------------------------------


def _flat(text: str) -> str:
    """Collapse Rich's soft wrapping so assertions can name a whole phrase."""
    return " ".join(text.split())


def test_tag_summary_reports_parent_only_and_write_disabled(tmp_path: Path) -> None:
    """`applied + parent-only + skipped_*` is the track count, so the summary
    must print all four buckets. It printed only applied + low-conf, so the
    parent-genre fallback and the `write_genre`-off tracks vanished — 10 tracks
    in, "Genre applied: 2 (skipped low-conf: 1)" out."""
    from vibechek.tagger import ApplyStats

    analysis = tmp_path / "a.json"
    analysis.write_text(
        json.dumps({"tracks": [{"path": str(tmp_path / "x.mp3")}]}), encoding="utf-8"
    )
    stats = ApplyStats(
        total=10,
        genre_applied=2,
        genre_applied_parent_only=5,
        genre_skipped_low_confidence=1,
        genre_skipped_write_disabled=2,
        other_tags_applied=10,
    )
    with mock.patch("vibechek.tagger.apply_ml_tags", return_value=stats):
        result = CliRunner().invoke(main, ["tag", str(analysis)])
    assert result.exit_code == 0, result.output
    out = _flat(result.output)
    assert "Genre applied: 2" in out
    assert "parent-only: 5" in out
    assert "skipped low-conf: 1" in out
    assert "Genre writes off in config: 2" in out


def test_tag_summary_hides_write_disabled_when_zero(tmp_path: Path) -> None:
    """The write-disabled bucket is the abnormal case — a normal run must not
    grow a "0" term for a toggle the user never touched."""
    from vibechek.tagger import ApplyStats

    analysis = tmp_path / "a.json"
    analysis.write_text(
        json.dumps({"tracks": [{"path": str(tmp_path / "x.mp3")}]}), encoding="utf-8"
    )
    stats = ApplyStats(total=3, genre_applied=3, other_tags_applied=3)
    with mock.patch("vibechek.tagger.apply_ml_tags", return_value=stats):
        result = CliRunner().invoke(main, ["tag", str(analysis)])
    assert result.exit_code == 0, result.output
    out = _flat(result.output)
    assert "parent-only: 0" in out  # always shown: it's part of the accounting
    assert "config" not in out


def test_tag_parent_only_writes_count_as_success(tmp_path: Path) -> None:
    """A parent-genre fallback IS a tag on disk. It was left out of the
    success count, so a run where every genre came from the fallback and one
    file errored exited 1 with "Nothing was tagged"."""
    from vibechek.tagger import ApplyStats

    analysis = tmp_path / "a.json"
    analysis.write_text(
        json.dumps({"tracks": [{"path": str(tmp_path / "x.mp3")}]}), encoding="utf-8"
    )
    stats = ApplyStats(
        total=4,
        genre_applied=0,
        genre_applied_parent_only=3,
        other_tags_applied=0,
        errors=["x.mp3: unreadable"],
    )
    with mock.patch("vibechek.tagger.apply_ml_tags", return_value=stats):
        result = CliRunner().invoke(main, ["tag", str(analysis)])
    assert result.exit_code == 0, result.output
    assert "Nothing was tagged" not in result.output


# ---------------------------------------------------------------------------
# profile load — an unreadable config.json must not traceback
# ---------------------------------------------------------------------------


def test_profile_load_reports_a_refused_save_cleanly(tmp_path: Path) -> None:
    """`load_profile` is a load→save round trip; on an unreadable config.json
    `save()` raises ConfigSaveRefused rather than writing factory defaults over
    the user's real settings. Only KeyError was caught, so that refusal reached
    the user as a raw traceback."""
    from vibechek.config import ConfigSaveRefused

    refusal = ConfigSaveRefused(
        "Your settings file couldn't be read, so Vibechek is showing factory "
        "defaults — saving now would erase the settings still on disk.",
        detail="config.json could not be loaded",
    )
    with mock.patch("vibechek.profiles.load_profile", side_effect=refusal):
        result = CliRunner().invoke(main, ["profile", "load", "house-dj"])
    assert result.exit_code != 0
    assert "Traceback" not in result.output
    assert "couldn't be read" in _flat(result.output)


def test_profile_load_unknown_name_still_reports_cleanly() -> None:
    """Sibling of the above — the KeyError path must keep working."""
    result = CliRunner().invoke(main, ["profile", "load", "not-a-profile"])
    assert result.exit_code != 0
    assert "Traceback" not in result.output
    assert "Unknown profile" in _flat(result.output)


def test_organize_warns_when_the_undo_journal_is_incomplete(
    synthetic_analysis: dict, tmp_path: Path
) -> None:
    """`OrganizeStats.journal_incomplete` says completed moves are MISSING from
    the undo journal (a full volume, an AV/cloud-sync lock). The CLI dropped
    the flag, so a later `revert` silently restored only part of the run."""
    from vibechek.organizer import OrganizeStats

    analysis_file = tmp_path / "analysis.json"
    analysis_file.write_text(json.dumps(synthetic_analysis), encoding="utf-8")
    stats = OrganizeStats(planned=5, moved=5, journal_incomplete=True)
    with mock.patch("vibechek.organizer.organize_from_analysis", return_value=stats):
        result = CliRunner().invoke(main, ["organize", str(analysis_file)])
    assert result.exit_code == 0, result.output
    out = _flat(result.output)
    assert "undo journal is INCOMPLETE" in out
    assert "only restore part" in out
