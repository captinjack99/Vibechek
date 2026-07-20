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
