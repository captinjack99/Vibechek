"""Shared pytest fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest


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
