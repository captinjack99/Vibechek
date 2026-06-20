"""Tests for the resolve_genre_conflicts RPC (trust-UX inline correction).

The handler lets the GUI batch-resolve reviewed genre conflicts: "approve"
accepts Vibechek's reconciled genre (marking the source "approved"), "revert"
puts the genre back to the file's existing tag. Either way the conflict flag is
cleared (the track leaves the review queue) and the decision is persisted to the
saved analysis so it survives a reload. It must NEVER write file tags — that
stays the separate apply_ml_tags flow.

State dirs are redirected to tmp_path so we never touch the user's real config.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from vibechek import library_state
from vibechek.rpc import _resolve_genre_conflicts


@pytest.fixture(autouse=True)
def _isolated_state_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(library_state, "STATE_FILE", tmp_path / "library_state.json")
    monkeypatch.setattr(library_state, "ANALYSES_DIR", tmp_path / "analyses")
    return tmp_path


def _conflict_track(
    path: str,
    *,
    tag: str = "Tech House",
    ml_genre: str = "Techno",
    source: str = "ml_override",
) -> dict:
    """A track whose audio read overrode a disagreeing tag (conflict=True)."""
    return {
        "path": path,
        "existing_tags": {"genre": tag},
        "ml_analysis": {
            "ml_genre": ml_genre,
            "ml_subgenre": ml_genre,
            "ml_genre_source": source,
            "ml_genre_conflict": True,
            "ml_genre_confidence": 0.9,
        },
    }


def _seed(library_path: str, tracks: list[dict]) -> str:
    """Persist a report under `library_path` and register it in the recents."""
    report = {
        "tracks": tracks,
        "summary": {"total_files": len(tracks), "analyzed": len(tracks)},
    }
    library_state.record_analysis(library_path, report)
    return library_path


def _reload_tracks(library_path: str) -> dict[str, dict]:
    """Read the on-disk analysis back, keyed by track path."""
    record = next(r for r in library_state.load_state().recent if r.path == library_path)
    report = library_state.load_analysis(record)
    assert report is not None
    return {t["path"]: t for t in report["tracks"]}


# ---------------------------------------------------------------------------
# approve
# ---------------------------------------------------------------------------


def test_approve_marks_approved_and_clears_conflict() -> None:
    _seed("/lib", [_conflict_track("/lib/t1.mp3")])
    out = _resolve_genre_conflicts(
        {"library_path": "/lib", "items": [{"path": "/lib/t1.mp3", "action": "approve"}]},
    )
    assert out["ok"] is True
    assert out["updated"] == 1
    ml = out["tracks"][0]["ml_analysis"]
    assert ml["ml_genre_source"] == "approved"
    assert ml["ml_genre_conflict"] is False
    # Approve keeps the reconciled value — it doesn't touch the genre itself.
    assert ml["ml_genre"] == "Techno"


def test_approve_persists_across_reload() -> None:
    _seed("/lib", [_conflict_track("/lib/t1.mp3")])
    _resolve_genre_conflicts(
        {"library_path": "/lib", "items": [{"path": "/lib/t1.mp3", "action": "approve"}]},
    )
    ml = _reload_tracks("/lib")["/lib/t1.mp3"]["ml_analysis"]
    assert ml["ml_genre_conflict"] is False
    assert ml["ml_genre_source"] == "approved"


# ---------------------------------------------------------------------------
# revert
# ---------------------------------------------------------------------------


def test_revert_restores_tag_genre() -> None:
    _seed("/lib", [_conflict_track("/lib/t1.mp3", tag="Tech House", ml_genre="Techno")])
    out = _resolve_genre_conflicts(
        {"library_path": "/lib", "items": [{"path": "/lib/t1.mp3", "action": "revert"}]},
    )
    ml = out["tracks"][0]["ml_analysis"]
    # split_tag_genre("Tech House") -> ("House", "Tech House")
    assert ml["ml_genre"] == "House"
    assert ml["ml_subgenre"] == "Tech House"
    assert ml["ml_genre_source"] == "tag"
    assert ml["ml_genre_conflict"] is False


def test_revert_without_tag_just_clears_conflict() -> None:
    """No tag to revert to → leave the genre as-is, but still clear the flag."""
    _seed("/lib", [_conflict_track("/lib/t1.mp3", tag="")])
    out = _resolve_genre_conflicts(
        {"library_path": "/lib", "items": [{"path": "/lib/t1.mp3", "action": "revert"}]},
    )
    assert out["updated"] == 1
    ml = out["tracks"][0]["ml_analysis"]
    assert ml["ml_genre_conflict"] is False
    assert ml["ml_genre"] == "Techno"  # unchanged — nothing to revert to


# ---------------------------------------------------------------------------
# scoping / partial selection
# ---------------------------------------------------------------------------


def test_only_listed_tracks_are_touched() -> None:
    _seed(
        "/lib",
        [_conflict_track("/lib/t1.mp3"), _conflict_track("/lib/t2.mp3")],
    )
    out = _resolve_genre_conflicts(
        {"library_path": "/lib", "items": [{"path": "/lib/t1.mp3", "action": "approve"}]},
    )
    assert out["updated"] == 1
    by_path = _reload_tracks("/lib")
    assert by_path["/lib/t1.mp3"]["ml_analysis"]["ml_genre_conflict"] is False
    # The unlisted track is left exactly as it was.
    assert by_path["/lib/t2.mp3"]["ml_analysis"]["ml_genre_conflict"] is True


def test_track_without_ml_is_skipped() -> None:
    _seed(
        "/lib",
        [{"path": "/lib/raw.mp3", "existing_tags": {"genre": "House"}, "ml_analysis": None}],
    )
    out = _resolve_genre_conflicts(
        {"library_path": "/lib", "items": [{"path": "/lib/raw.mp3", "action": "approve"}]},
    )
    assert out["ok"] is True
    assert out["updated"] == 0


def test_unknown_action_defaults_to_approve() -> None:
    """A garbage action string isn't a revert — it's treated as approve."""
    _seed("/lib", [_conflict_track("/lib/t1.mp3")])
    out = _resolve_genre_conflicts(
        {"library_path": "/lib", "items": [{"path": "/lib/t1.mp3", "action": "nonsense"}]},
    )
    assert out["updated"] == 1
    assert out["tracks"][0]["ml_analysis"]["ml_genre_source"] == "approved"


# ---------------------------------------------------------------------------
# error paths
# ---------------------------------------------------------------------------


def test_missing_library_path_is_rejected() -> None:
    out = _resolve_genre_conflicts({"items": [{"path": "/x", "action": "approve"}]})
    assert out["ok"] is False
    assert out["updated"] == 0
    assert out["tracks"] == []


def test_empty_items_is_rejected() -> None:
    _seed("/lib", [_conflict_track("/lib/t1.mp3")])
    out = _resolve_genre_conflicts({"library_path": "/lib", "items": []})
    assert out["ok"] is False


def test_library_not_in_recents_is_rejected() -> None:
    out = _resolve_genre_conflicts(
        {"library_path": "/nope", "items": [{"path": "/nope/t.mp3", "action": "approve"}]},
    )
    assert out["ok"] is False
    assert out["reason"] == "library not in recents"


def test_no_saved_analysis_is_rejected(tmp_path: Path) -> None:
    """Recent record exists but its analysis JSON is gone → graceful failure."""
    library_state.save_state(
        library_state.LibraryState(
            recent=[
                library_state.LibraryRecord(
                    path="/lib",
                    analysis_path=str(tmp_path / "missing.json"),
                ),
            ],
        ),
    )
    out = _resolve_genre_conflicts(
        {"library_path": "/lib", "items": [{"path": "/lib/t1.mp3", "action": "approve"}]},
    )
    assert out["ok"] is False
    assert out["reason"] == "no saved analysis for library"
