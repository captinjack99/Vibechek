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


def test_load_state_unknown_field_is_dropped_record_kept(_isolated_state_dirs: Path) -> None:
    """Forward-compat / schema drift: a record carrying an UNKNOWN field (e.g.
    written by a newer version) must NOT discard the whole recent list — the
    record is kept with the unknown field ignored. (Regression: a single such
    record used to wipe every recent library.)"""
    library_state.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    library_state.STATE_FILE.write_text(
        json.dumps({"recent": [
            {"path": "/x", "analysis_path": "/y", "bogus_field": 1},
            {"path": "/a", "analysis_path": "/b"},
        ]}),
        encoding="utf-8",
    )
    state = library_state.load_state()
    assert [r.path for r in state.recent] == ["/x", "/a"]  # both kept
    assert not hasattr(state.recent[0], "bogus_field")     # unknown field dropped


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


# ---------------------------------------------------------------------------
# Multi-library support: display name + tags
# ---------------------------------------------------------------------------


def test_record_defaults_for_new_fields() -> None:
    """New `name` / `tags` fields default cleanly so old call sites still work."""
    r = library_state.LibraryRecord(path="/lib", analysis_path="/x")
    assert r.name == ""
    assert r.tags == []
    # display_name falls back to basename when name is empty
    assert r.display_name() == "lib"


def test_display_name_uses_override_when_set() -> None:
    r = library_state.LibraryRecord(path="/lib/foo", analysis_path="/x", name="Friday Set")
    assert r.display_name() == "Friday Set"


def test_display_name_falls_back_to_path_for_root() -> None:
    """Path with no basename (just "/") should still produce something."""
    r = library_state.LibraryRecord(path="/", analysis_path="/x")
    assert r.display_name() == "/"


def test_record_with_name_and_tags_round_trips() -> None:
    library_state.record_open("/lib/foo")
    library_state.rename_library("/lib/foo", "Brunch Set")
    library_state.tag_library("/lib/foo", ["Brunch", "Outdoor"])

    state = library_state.load_state()
    assert state.recent[0].name == "Brunch Set"
    assert state.recent[0].tags == ["Brunch", "Outdoor"]


def test_rename_library_strips_whitespace() -> None:
    library_state.record_open("/lib/foo")
    r = library_state.rename_library("/lib/foo", "   Wedding   ")
    assert r is not None
    assert r.name == "Wedding"


def test_rename_library_empty_clears_override() -> None:
    library_state.record_open("/lib/foo")
    library_state.rename_library("/lib/foo", "Custom")
    library_state.rename_library("/lib/foo", "")  # clear
    r = library_state.load_state().recent[0]
    assert r.name == ""
    assert r.display_name() == "foo"


def test_rename_library_unknown_returns_none() -> None:
    """Renaming a non-recent library must NOT add it (avoids ghost rows)."""
    assert library_state.rename_library("/never/added", "Whatever") is None
    assert library_state.load_state().recent == []


def test_tag_library_dedupes_case_insensitive() -> None:
    library_state.record_open("/lib/foo")
    r = library_state.tag_library("/lib/foo", ["Brunch", "brunch", "BRUNCH", "Wedding"])
    assert r is not None
    # First-occurrence wins for the casing
    assert r.tags == ["Brunch", "Wedding"]


def test_tag_library_strips_and_drops_empty() -> None:
    library_state.record_open("/lib/foo")
    r = library_state.tag_library("/lib/foo", [" Brunch ", "", "   ", "Wedding"])
    assert r is not None
    assert r.tags == ["Brunch", "Wedding"]


def test_tag_library_unknown_returns_none() -> None:
    assert library_state.tag_library("/never/added", ["x"]) is None
    assert library_state.load_state().recent == []


def test_tag_library_replaces_existing() -> None:
    """tag_library is a set-operation, not an append."""
    library_state.record_open("/lib/foo")
    library_state.tag_library("/lib/foo", ["A", "B"])
    r = library_state.tag_library("/lib/foo", ["C"])
    assert r is not None
    assert r.tags == ["C"]


def test_old_state_file_without_new_fields_loads() -> None:
    """Backward compat: a state file written by an older Vibechek (no name /
    tags fields) must round-trip without crashing — the dataclass defaults
    fill them in."""
    library_state.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    library_state.STATE_FILE.write_text(
        json.dumps({
            "recent": [
                {
                    "path": "/lib/old",
                    "analysis_path": "/data/old.json",
                    "track_count": 5,
                    "analyzed_count": 5,
                    "last_opened": 1.0,
                    "last_analyzed": 1.0,
                },
            ],
        }),
        encoding="utf-8",
    )
    state = library_state.load_state()
    assert len(state.recent) == 1
    assert state.recent[0].name == ""
    assert state.recent[0].tags == []
    assert state.recent[0].display_name() == "old"
