"""Tests for vibechek.utils."""

from __future__ import annotations

import unicodedata
from pathlib import Path

import pytest

from vibechek.utils import (
    SUPPORTED_EXTENSIONS,
    find_audio_files,
    report_progress,
    resolve_existing_path,
    sanitize_folder_name,
)


def test_resolve_existing_path_exact(tmp_path: Path) -> None:
    f = tmp_path / "plain.flac"
    f.write_bytes(b"x")
    assert resolve_existing_path(str(f)) == f


def test_resolve_existing_path_tolerates_nfd_input(tmp_path: Path) -> None:
    """An accented filename stored on disk as NFC must still resolve when the
    path arrives NFD-normalized — e.g. an analysis.json written on macOS (NFD)
    and applied on another platform. Without it, organize/tag report every
    accented track ("Tiësto", "Naté", "Années") as not found and skip it.
    """
    f = tmp_path / "Tiësto - Adagio.flac"  # NFC 'ë'
    f.write_bytes(b"x")
    nfd = unicodedata.normalize("NFD", str(f))
    if nfd == str(f):
        pytest.skip("platform pre-normalizes; nothing to test")
    resolved = resolve_existing_path(nfd)
    assert resolved is not None and resolved.exists()


def test_resolve_existing_path_missing_returns_none(tmp_path: Path) -> None:
    assert resolve_existing_path(str(tmp_path / "nope_zzz.flac")) is None


def test_sanitize_strips_invalid_chars() -> None:
    assert sanitize_folder_name('Hip*Hop/Rap?') == "Hip_Hop_Rap_"


def test_sanitize_empty_returns_unknown() -> None:
    assert sanitize_folder_name(None) == "Unknown"
    assert sanitize_folder_name("") == "Unknown"
    assert sanitize_folder_name("   ") == "Unknown"


def test_sanitize_passes_through_clean_name() -> None:
    assert sanitize_folder_name("Deep House") == "Deep House"


def test_sanitize_replaces_control_characters() -> None:
    """A NUL in a path makes `mkdir` raise ValueError — not OSError — which
    escaped route_new_tracks' handler and aborted the whole batch. It arrives
    for real: a multi-value ID3 genre frame stringifies NUL-joined."""
    assert sanitize_folder_name("Techno\x00House") == "Techno_House"
    assert sanitize_folder_name("Tech\tHouse\nLive") == "Tech_House_Live"
    assert sanitize_folder_name("Techno\x7f") == "Techno_"


def test_sanitize_rejects_dot_traversal_names() -> None:
    # A genre tag of ".." (or "." / leading-dot variants) must never survive as
    # a path segment that could escape the library root.
    assert sanitize_folder_name("..") == "Unknown"
    assert sanitize_folder_name(".") == "Unknown"
    assert sanitize_folder_name("  ..  ") == "Unknown"
    assert sanitize_folder_name("...") == "Unknown"
    # Separators collapse to "_", so an embedded traversal can't introduce a sep.
    assert "/" not in sanitize_folder_name("../../Windows")
    assert "\\" not in sanitize_folder_name("..\\..\\Windows")


def test_sanitize_strips_trailing_dots_and_spaces() -> None:
    # Windows silently drops trailing dots/spaces from folder names; normalize so
    # our intended name matches what lands on disk.
    assert sanitize_folder_name("House.") == "House"
    assert sanitize_folder_name("House ") == "House"
    assert sanitize_folder_name("House. . ") == "House"


def test_sanitize_maps_windows_reserved_device_names() -> None:
    # Reserved device names are uncreatable on Windows; they must be remapped to
    # a safe, creatable name rather than passed through verbatim.
    for reserved in ("CON", "con", "PRN", "AUX", "NUL", "COM1", "COM9", "LPT1", "LPT9"):
        out = sanitize_folder_name(reserved)
        assert out.lower() not in {
            "con", "prn", "aux", "nul",
            *(f"com{i}" for i in range(1, 10)),
            *(f"lpt{i}" for i in range(1, 10)),
        }
        assert out  # never empty
    # A reserved stem with an extension (e.g. a genre "nul.mp3") is just as
    # reserved on Windows and must also be remapped.
    assert sanitize_folder_name("nul.mp3").lower() != "nul.mp3"
    # Non-reserved lookalikes are untouched.
    assert sanitize_folder_name("Console") == "Console"
    assert sanitize_folder_name("COM10") == "COM10"


def test_find_audio_files_returns_audio_only(tiny_library: Path) -> None:
    files = find_audio_files(tiny_library)
    assert all(f.suffix.lower() in SUPPORTED_EXTENSIONS for f in files)
    assert any(f.name == "track1.mp3" for f in files)
    assert not any(f.name == "notes.txt" for f in files)


def test_find_audio_files_is_deterministic(tiny_library: Path) -> None:
    a = find_audio_files(tiny_library)
    b = find_audio_files(tiny_library)
    assert [str(p) for p in a] == [str(p) for p in b]


def test_find_audio_files_missing_path_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        find_audio_files(tmp_path / "does_not_exist")


def test_report_progress_swallows_callback_errors() -> None:
    def boom(*_args: object) -> None:
        raise RuntimeError("UI gone")

    # Should not raise — callback errors are isolated
    report_progress(boom, 1, 10, "msg")


def test_report_progress_no_callback() -> None:
    # Calling with None should be a no-op
    report_progress(None, 1, 10, "msg")


# ---------------------------------------------------------------------------
# library_state.load_state — defensive per-row parsing
# ---------------------------------------------------------------------------


def _write_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, records: list) -> Path:
    """Point library_state.STATE_FILE at a temp index containing `records`."""
    import json as _json

    from vibechek import library_state

    f = tmp_path / "library_state.json"
    f.write_text(_json.dumps({"recent": records}), encoding="utf-8")
    monkeypatch.setattr(library_state, "STATE_FILE", f)
    return f


def test_load_state_keeps_valid_records_when_one_has_unknown_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A single forward-compat record (extra key written by a newer build then
    downgraded) must NOT wipe the whole recent list — drop the unknown key and
    keep every record. Without the fix load_state() returned 0 records.
    """
    from vibechek import library_state

    records = [
        {"path": f"D:/lib{i}", "analysis_path": f"a{i}.json"} for i in range(3)
    ]
    records.append({"path": "D:/X", "analysis_path": "a.json", "color": "blue"})
    _write_state(tmp_path, monkeypatch, records)

    state = library_state.load_state()
    assert [r.path for r in state.recent] == ["D:/lib0", "D:/lib1", "D:/lib2", "D:/X"]
    # The unknown key is dropped, not preserved on the record.
    assert not hasattr(state.recent[-1], "color")


def test_load_state_skips_only_the_record_missing_a_required_field(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A record missing a required field (e.g. from an interrupted migration)
    is skipped on its own; every other valid record survives.
    """
    from vibechek import library_state

    records = [
        {"path": f"D:/lib{i}", "analysis_path": f"a{i}.json"} for i in range(9)
    ]
    records.append({"path": "D:/X"})  # missing required analysis_path
    _write_state(tmp_path, monkeypatch, records)

    state = library_state.load_state()
    assert len(state.recent) == 9
    assert all(r.analysis_path for r in state.recent)


def test_load_state_skips_non_dict_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stray valid-JSON-but-non-object row (array/scalar) is skipped rather
    than crashing the load.
    """
    from vibechek import library_state

    records = [
        ["stray", "array"],
        42,
        {"path": "D:/Y", "analysis_path": "y.json"},
    ]
    _write_state(tmp_path, monkeypatch, records)

    state = library_state.load_state()
    assert [r.path for r in state.recent] == ["D:/Y"]


def test_load_state_coerces_tags_string_to_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`tags` stored as a bare string (hand-edited/old file) must become a
    list[str] so the UI doesn't iterate it character-by-character.
    """
    from vibechek import library_state

    records = [{"path": "D:/Y", "analysis_path": "y.json", "tags": "Brunch"}]
    _write_state(tmp_path, monkeypatch, records)

    state = library_state.load_state()
    assert state.recent[0].tags == ["Brunch"]


def test_load_state_missing_file_returns_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vibechek import library_state

    monkeypatch.setattr(
        library_state, "STATE_FILE", tmp_path / "does_not_exist.json"
    )
    assert library_state.load_state().recent == []


def test_load_state_corrupt_json_returns_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vibechek import library_state

    f = tmp_path / "library_state.json"
    f.write_text("not json {", encoding="utf-8")
    monkeypatch.setattr(library_state, "STATE_FILE", f)
    assert library_state.load_state().recent == []


# ---------------------------------------------------------------------------
# find_executable — what we're willing to execute
#
# Shared by find_fpcalc, native_decode's ffmpeg lookups and cdj_export.
# ---------------------------------------------------------------------------


def test_find_executable_ignores_a_planted_copy_in_the_current_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tool sitting in the working folder must never win.

    On Windows `shutil.which` prepends the current directory to the search path
    (NeedCurrentDirectoryForExePath; passing `path=` doesn't suppress it), so an
    ffmpeg.exe unpacked alongside a downloaded sample pack would beat every real
    PATH entry — and then be run once per track.
    """
    from vibechek import utils

    planted = tmp_path / "ffmpeg.exe"
    planted.write_bytes(b"planted")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(utils.shutil, "which", lambda _cmd: str(planted))

    assert utils.find_executable("ffmpeg") is None


def test_find_executable_returns_an_absolute_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A relative hit is absolutized — a subprocess re-resolves relative argv[0]."""
    from vibechek import utils

    toolsdir = tmp_path / "tools"
    toolsdir.mkdir()
    real = toolsdir / "ffmpeg.exe"
    real.write_bytes(b"real")
    monkeypatch.chdir(tmp_path)          # cwd is the PARENT, not tools/
    monkeypatch.setattr(utils.shutil, "which", lambda _cmd: "tools/ffmpeg.exe")

    found = utils.find_executable("ffmpeg")

    assert found is not None
    assert Path(found).is_absolute()
    assert Path(found) == real


def test_find_executable_returns_none_when_not_on_path(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    from vibechek import utils

    monkeypatch.setattr(utils.shutil, "which", lambda _cmd: None)
    assert utils.find_executable("definitely-not-a-real-tool") is None


def test_find_executable_survives_an_unreadable_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An OSError probing the cwd is not evidence the hit is unsafe."""
    from vibechek import utils

    real = tmp_path / "tools" / "ffmpeg.exe"
    real.parent.mkdir()
    real.write_bytes(b"real")
    monkeypatch.setattr(utils.shutil, "which", lambda _cmd: str(real))

    # Subclass the CONCRETE flavour (`WindowsPath`/`PosixPath`), not `Path`:
    # before Python 3.12 a bare `Path` subclass has no `_flavour` and every
    # instantiation dies with AttributeError — which is what took the 3.10 CI
    # legs down when this test first shipped.
    class _NoCwd(type(Path())):
        @classmethod
        def cwd(cls):  # noqa: D102
            raise OSError("the working directory was deleted")

    # Swap the module's Path, not pathlib's — patching pathlib globally would
    # take pytest's own bookkeeping down with it.
    monkeypatch.setattr(utils, "Path", _NoCwd)

    assert utils.find_executable("ffmpeg") == str(real)


# ---------------------------------------------------------------------------
# find_fpcalc — what we're willing to execute
# ---------------------------------------------------------------------------


def test_find_fpcalc_ignores_a_hit_in_the_current_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An fpcalc sitting in the working folder must never win.

    On Windows `shutil.which` prepends the current directory to the search path
    (NeedCurrentDirectoryForExePath; passing `path=` doesn't suppress it), so an
    fpcalc.exe unpacked alongside a downloaded sample pack would beat both real
    PATH entries and the pinned, SHA256-verified staged copy — and then run once
    per track during dedupe.
    """
    from vibechek import utils

    planted = tmp_path / "fpcalc.exe"
    planted.write_bytes(b"planted")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(utils.shutil, "which", lambda _cmd: str(planted))
    # Keep the well-known-locations fallback hermetic and fast.
    monkeypatch.setattr(
        utils.subprocess, "run",
        lambda *_a, **_k: (_ for _ in ()).throw(FileNotFoundError()),
    )

    assert utils.find_fpcalc() is None


def test_find_fpcalc_returns_an_absolute_path_for_a_real_path_hit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vibechek import utils

    toolsdir = tmp_path / "tools"
    toolsdir.mkdir()
    real = toolsdir / "fpcalc.exe"
    real.write_bytes(b"real")
    monkeypatch.chdir(tmp_path)          # cwd is the PARENT, not tools/
    monkeypatch.setattr(utils.shutil, "which", lambda _cmd: str(real))

    found = utils.find_fpcalc()

    assert found == str(real)
    assert Path(found).is_absolute()


# ---------------------------------------------------------------------------
# backup_history.load — valid JSON of the wrong shape
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("body", ["null", "[]", '"hello"', "42", '{"records": null}',
                                  '{"records": "nope"}', '{"records": [1, 2]}'])
def test_backup_history_load_survives_wrong_shaped_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str
) -> None:
    """A hand-edited (or third-party-mangled) index must degrade to an empty
    history like a corrupt one does — not raise AttributeError/TypeError out of
    every caller, which also blocked `record()` from indexing NEW backups."""
    from vibechek import backup_history

    f = tmp_path / "backup_history.json"
    f.write_text(body, encoding="utf-8")
    monkeypatch.setattr(backup_history, "HISTORY_FILE", f)

    assert backup_history.load().records == []


def test_backup_history_record_recovers_a_wrong_shaped_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vibechek import backup_history

    f = tmp_path / "backup_history.json"
    f.write_text("null", encoding="utf-8")
    monkeypatch.setattr(backup_history, "HISTORY_FILE", f)
    backup = tmp_path / "backup.json"
    backup.write_text("{}", encoding="utf-8")

    rec = backup_history.record(tmp_path, backup, file_count=3)

    assert rec.backup_path == str(backup)
    assert [r.backup_path for r in backup_history.load().records] == [str(backup)]


def test_backup_history_keeps_the_good_rows_beside_a_bad_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One junk row must not wipe the list — the same rule library_state uses."""
    import json as _json

    from vibechek import backup_history

    good = {"backup_path": str(tmp_path / "b.json"), "library_path": str(tmp_path),
            "file_count": 1, "created_at": 0.0}
    f = tmp_path / "backup_history.json"
    f.write_text(_json.dumps({"records": ["junk", good]}), encoding="utf-8")
    monkeypatch.setattr(backup_history, "HISTORY_FILE", f)

    records = backup_history.load().records

    assert [r.backup_path for r in records] == [good["backup_path"]]
