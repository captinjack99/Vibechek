"""Tests for vibechek.library_state.

Always monkey-patch STATE_FILE and ANALYSES_DIR onto tmp_path so we never
touch the user's real config/data dirs.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vibechek import library_state


@pytest.fixture(autouse=True)
def _isolated_state_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the module's STATE_FILE and ANALYSES_DIR to tmp_path."""
    state_file = tmp_path / "library_state.json"
    analyses_dir = tmp_path / "analyses"
    monkeypatch.setattr(library_state, "STATE_FILE", state_file)
    monkeypatch.setattr(library_state, "ANALYSES_DIR", analyses_dir)
    return tmp_path


# ---------------------------------------------------------------------------
# load_state / save_state
# ---------------------------------------------------------------------------


def test_load_state_returns_empty_when_file_missing() -> None:
    state = library_state.load_state()
    assert state.recent == []


def test_load_state_round_trip() -> None:
    state = library_state.LibraryState(
        recent=[
            library_state.LibraryRecord(
                path="/lib/a",
                analysis_path="/data/a.json",
                track_count=42,
                analyzed_count=10,
                last_opened=1700.0,
                last_analyzed=1800.0,
            ),
        ],
    )
    library_state.save_state(state)
    reloaded = library_state.load_state()
    assert len(reloaded.recent) == 1
    r = reloaded.recent[0]
    assert r.path == "/lib/a"
    assert r.track_count == 42
    assert r.last_analyzed == 1800.0


def test_load_state_corrupted_json_returns_empty(_isolated_state_dirs: Path) -> None:
    library_state.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    library_state.STATE_FILE.write_text("{not json at all", encoding="utf-8")

    state = library_state.load_state()
    assert state.recent == []


def test_load_state_unknown_field_returns_empty(_isolated_state_dirs: Path) -> None:
    """Schema drift — a record with a TypeError-causing field shouldn't crash."""
    library_state.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    library_state.STATE_FILE.write_text(
        json.dumps({"recent": [{"path": "/x", "analysis_path": "/y", "bogus_field": 1}]}),
        encoding="utf-8",
    )
    state = library_state.load_state()
    assert state.recent == []  # TypeError → fallback


def test_save_state_creates_parent_dir(_isolated_state_dirs: Path, tmp_path: Path) -> None:
    nested = tmp_path / "deeply" / "nested" / "library_state.json"
    # Patch to a fresh nested path
    import vibechek.library_state as ls_mod

    ls_mod.STATE_FILE = nested
    library_state.save_state(library_state.LibraryState())
    assert nested.exists()


# ---------------------------------------------------------------------------
# record_open
# ---------------------------------------------------------------------------


def test_record_open_creates_new_entry() -> None:
    record = library_state.record_open("/path/to/library")
    assert record.path == "/path/to/library"
    assert record.last_opened > 0
    assert record.last_analyzed == 0.0

    state = library_state.load_state()
    assert len(state.recent) == 1
    assert state.recent[0].path == "/path/to/library"


def test_record_open_existing_bumps_to_front() -> None:
    library_state.record_open("/a")
    library_state.record_open("/b")
    library_state.record_open("/c")
    library_state.record_open("/a")  # touch /a again

    state = library_state.load_state()
    assert [r.path for r in state.recent] == ["/a", "/c", "/b"]


def test_record_open_truncates_to_max_recent() -> None:
    for i in range(library_state.MAX_RECENT + 5):
        library_state.record_open(f"/lib{i}")
    state = library_state.load_state()
    assert len(state.recent) == library_state.MAX_RECENT
    # The most recent (largest index) is at the front
    assert state.recent[0].path == f"/lib{library_state.MAX_RECENT + 4}"


# ---------------------------------------------------------------------------
# record_analysis
# ---------------------------------------------------------------------------


def _fake_report(total: int = 5, analyzed: int = 3) -> dict:
    return {
        "summary": {"total_files": total, "analyzed": analyzed},
        "tracks": [],
    }


def test_record_analysis_writes_analysis_file(tmp_path: Path) -> None:
    record = library_state.record_analysis("/lib/main", _fake_report(total=12, analyzed=10))
    analysis_file = Path(record.analysis_path)
    assert analysis_file.exists()

    reloaded = json.loads(analysis_file.read_text(encoding="utf-8"))
    assert reloaded["summary"]["total_files"] == 12


def test_record_analysis_updates_counts() -> None:
    record = library_state.record_analysis("/lib/main", _fake_report(total=12, analyzed=10))
    assert record.track_count == 12
    assert record.analyzed_count == 10
    assert record.last_analyzed > 0
    assert record.last_opened > 0


def test_record_analysis_after_record_open_promotes_existing() -> None:
    library_state.record_open("/lib/a")
    library_state.record_open("/lib/b")  # b is at front
    library_state.record_analysis("/lib/a", _fake_report())

    state = library_state.load_state()
    assert state.recent[0].path == "/lib/a"
    # /a should now have analyzed counts; /b should not
    assert state.recent[0].last_analyzed > 0
    assert state.recent[1].last_analyzed == 0.0


def test_record_analysis_handles_missing_summary() -> None:
    """summary might be missing on old/odd reports — should not crash."""
    record = library_state.record_analysis("/lib", {"tracks": []})
    assert record.track_count == 0
    assert record.analyzed_count == 0


def test_record_analysis_stable_path_for_same_library() -> None:
    r1 = library_state.record_analysis("/lib/foo", _fake_report())
    r2 = library_state.record_analysis("/lib/foo", _fake_report(total=99))
    assert r1.analysis_path == r2.analysis_path


# ---------------------------------------------------------------------------
# forget
# ---------------------------------------------------------------------------


def test_forget_removes_known_library() -> None:
    library_state.record_open("/a")
    library_state.record_open("/b")
    removed = library_state.forget("/a")
    assert removed is True

    state = library_state.load_state()
    assert [r.path for r in state.recent] == ["/b"]


def test_forget_unknown_returns_false() -> None:
    library_state.record_open("/a")
    assert library_state.forget("/never-added") is False
    # Original entry still present
    assert len(library_state.load_state().recent) == 1


# ---------------------------------------------------------------------------
# load_analysis
# ---------------------------------------------------------------------------


def test_load_analysis_returns_report_after_record() -> None:
    record = library_state.record_analysis("/lib", _fake_report(total=4))
    loaded = library_state.load_analysis(record)
    assert loaded is not None
    assert loaded["summary"]["total_files"] == 4


def test_load_analysis_missing_file_returns_none() -> None:
    record = library_state.LibraryRecord(
        path="/lib",
        analysis_path="/does/not/exist.json",
    )
    assert library_state.load_analysis(record) is None


def test_load_analysis_corrupted_file_returns_none(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    record = library_state.LibraryRecord(path="/lib", analysis_path=str(bad))
    assert library_state.load_analysis(record) is None


# ---------------------------------------------------------------------------
# most_recent
# ---------------------------------------------------------------------------


def test_most_recent_empty_returns_none() -> None:
    assert library_state.LibraryState().most_recent() is None


def test_most_recent_returns_first() -> None:
    state = library_state.LibraryState(
        recent=[
            library_state.LibraryRecord(path="/a", analysis_path="/x"),
            library_state.LibraryRecord(path="/b", analysis_path="/y"),
        ],
    )
    assert state.most_recent().path == "/a"
