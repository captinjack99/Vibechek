"""Shared pytest fixtures."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_user_dirs(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect every user-data / user-config / log path to a per-test tmp dir.

    Without this, importing `vibechek.rpc` and starting `serve()` (as test_rpc
    does) calls `logging_setup.configure()` which writes to
    `platformdirs.user_data_dir("Vibechek") / "logs/vibechek.log"` — the user's
    *real* log file. The same is true for `backup_history` (writes
    `<config_dir>/backup_history.json`) and `library_state` (writes
    `<data_dir>/analyses/...`). Tests would then leak handler errors and
    fixture chatter into the production log the GUI reads at runtime.

    We monkeypatch the module-level path constants in every module that owns
    one. Autouse so no test author has to remember to opt in.
    """
    base = tmp_path_factory.mktemp("vibechek-test-home")
    data_dir = base / "data"
    config_dir = base / "config"
    data_dir.mkdir(parents=True, exist_ok=True)
    config_dir.mkdir(parents=True, exist_ok=True)

    # Import lazily so we don't pay the cost when these modules aren't loaded.
    from vibechek import backup_history, config, journal, library_state, logging_setup

    monkeypatch.setattr(config, "DATA_DIR", data_dir, raising=True)
    monkeypatch.setattr(config, "CONFIG_DIR", config_dir, raising=True)
    monkeypatch.setattr(config, "MODELS_DIR", data_dir / "models", raising=True)
    monkeypatch.setattr(config, "CONFIG_FILE", config_dir / "config.toml", raising=True)

    monkeypatch.setattr(library_state, "ANALYSES_DIR", data_dir / "analyses", raising=True)
    monkeypatch.setattr(library_state, "STATE_FILE", config_dir / "library_state.json", raising=True)
    monkeypatch.setattr(backup_history, "HISTORY_FILE", config_dir / "backup_history.json", raising=True)
    # Undo journals too — tests exercising organize/dedupe used to write real
    # journals into the user's data dir, polluting the GUI's "Recent
    # operations" list with test entries.
    monkeypatch.setattr(journal, "JOURNALS_DIR", data_dir / "journals", raising=True)

    log_dir = data_dir / "logs"
    monkeypatch.setattr(logging_setup, "LOG_DIR", log_dir, raising=True)
    monkeypatch.setattr(logging_setup, "LOG_FILE", log_dir / "vibechek.log", raising=True)
    # Force re-configure: a previous test that called configure() set _configured=True
    # and added a file handler pointing at the OLD log file.
    monkeypatch.setattr(logging_setup, "_configured", False, raising=True)

    # Snapshot root logging state so we restore it after the test, otherwise
    # the handler we added stays attached pointing into tmp_path (which gets
    # deleted), and Windows refuses to remove the dir while the handle is open.
    root = logging.getLogger()
    original_handlers = list(root.handlers)
    original_level = root.level
    yield
    for h in list(root.handlers):
        if h not in original_handlers:
            root.removeHandler(h)
            try:
                h.close()
            except Exception:  # noqa: BLE001
                pass
    root.setLevel(original_level)


@pytest.fixture
def tiny_library(tmp_path: Path) -> Path:
    """A throwaway library directory with a handful of mock audio files.

    The files are NOT valid audio — they're just text with audio extensions.
    Use this for tests that exercise filesystem walking, MD5 hashing,
    folder organization planning, etc.: anything that doesn't need to
    actually decode audio.
    """
    files = {
        "House/Deep House/track1.mp3": "house track contents",
        "House/Deep House/track2.flac": "another house track",
        "Techno/track3.mp3": "techno content",
        "Techno/track3_dup.mp3": "techno content",  # exact dup of track3.mp3
        "track4.m4a": "m4a track",
        "notes.txt": "not an audio file",  # should be ignored by find_audio_files
    }
    for rel_path, content in files.items():
        f = tmp_path / rel_path
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(content, encoding="utf-8")
    return tmp_path


@pytest.fixture
def synthetic_analysis(tmp_path: Path) -> dict:
    """A small analysis.json-style payload backed by real (empty) files."""
    library = tmp_path / "library"
    library.mkdir()

    def make_track(rel: str, genre: str, subgenre: str, confidence: float) -> dict:
        path = library / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"")  # empty file; organizer just moves it
        return {
            "path": str(path),
            "filename": path.name,
            "extension": path.suffix.lower(),
            "size_mb": 0.0,
            "ml_analysis": {
                "ml_genre": genre,
                "ml_subgenre": subgenre,
                "ml_genre_confidence": confidence,
                # Modern reports carry raw (top-class) confidence too. The
                # two-stage tagger only applies the parent-genre fallback when
                # this field is present (a legacy report without it reproduces
                # the old strict-only behaviour). We mirror `confidence` here so
                # stage-1 (raw) and stage-2 (family) gate on the same value,
                # matching what these tests assert.
                "ml_genre_raw_confidence": confidence,
                "ml_energy": 3,
                "ml_mood": "Neutral",
                "ml_timeslot": "Warm-Up",
                "ml_direction": "Steady",
                "ml_vocal": "Instrumental",
            },
        }

    return {
        "status": "complete",
        "tracks": [
            # 4 House tracks → House gets its own folder
            make_track("a.mp3", "House", "Deep House", 0.92),
            make_track("b.mp3", "House", "Tech House", 0.88),
            make_track("c.mp3", "House", "Deep House", 0.75),
            make_track("d.mp3", "House", "Deep House", 0.50),
            # 2 Techno → above default min_genre_size of 10? No → Other/
            make_track("e.flac", "Techno", "Minimal Techno", 0.91),
            make_track("f.flac", "Techno", "Hard Techno", 0.42),
            # 1 Vaporwave → rare → Other/
            make_track("g.mp3", "Vaporwave", "Vaporwave", 0.65),
        ],
    }
