"""ML analysis pipeline.

Wraps Essentia's pre-trained Discogs-EffNet models to classify each track on
genre (400 classes), subgenre, energy (0-5), mood (Dark/Neutral/Bright),
timeslot (Opener/Warm-Up/Peak/Afterhours), direction (Up/Steady/Down), and
vocal content (Instrumental/Light Vocal/Vocal).

Source: port of `legacy/analyze_dj_tracks_v2.py`.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from vibechek.config import AnalysisConfig


@dataclass
class TrackAnalysis:
    path: Path
    genre: str | None
    genre_confidence: float
    subgenre: str | None
    bpm: float | None
    key: str | None  # Camelot notation
    energy: int  # 0-5
    mood: str  # Dark | Neutral | Bright
    timeslot: str  # Opener | Warm-Up | Peak | Afterhours
    direction: str  # Up | Steady | Down
    vocal: str  # Instrumental | Light Vocal | Vocal


def analyze_directory(
    path: Path,
    config: AnalysisConfig,
    on_progress=None,
) -> list[TrackAnalysis]:
    """Analyze every audio file under `path` and return results.

    Multi-process; uses `config.workers` independent processes to sidestep the
    Python GIL on CPU-bound ML inference.
    """
    # TODO(phase-1): port from legacy/analyze_dj_tracks_v2.py
    raise NotImplementedError("Analyzer porting tracked in docs/ROADMAP.md (Phase 1)")
