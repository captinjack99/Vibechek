"""Regression tests for the 2026-06-01 audit fixes in `vibechek.analyzer`.

Covered:
- `_classify_direction` — the HIGH softmax-column bug (was "Steady" for every
  track because it averaged BOTH 2-class softmax columns).
- `_snap_bpm_octave` — the DJ octave-error guard (70↔140, 87↔174) and
  filename-BPM reconciliation.
- `_wsl_install_is_outdated` — the version-drift guard must NOT reject a newer
  WSL install and must canonicalise pip/human version spellings.
- `_class_index` — resolving the voice column by label name with index fallback.

These exercise the pure helpers directly (no essentia / numpy-on-real-audio),
so they run anywhere numpy is importable.
"""

from __future__ import annotations

import pytest

from vibechek import analyzer

# ---------------------------------------------------------------------------
# Direction classifier (HIGH): index the aggressive column before averaging
# ---------------------------------------------------------------------------


def _ramp_aggressive(start: float, end: float, frames: int = 30):
    """Build an (frames, 2) softmax-like array whose AGGRESSIVE column (index 0)
    ramps linearly from `start` to `end`. Column 1 is the complement so each
    row sums to 1.0 — exactly the shape that defeated the old (no-column) mean.
    """
    np = pytest.importorskip("numpy")
    agg = np.linspace(start, end, frames, dtype=float)
    return np.stack([agg, 1.0 - agg], axis=1)


def test_direction_up_on_rising_aggression() -> None:
    arr = _ramp_aggressive(0.1, 0.9)
    assert analyzer._classify_direction(arr) == "Up"


def test_direction_down_on_falling_aggression() -> None:
    arr = _ramp_aggressive(0.9, 0.1)
    assert analyzer._classify_direction(arr) == "Down"


def test_direction_steady_on_flat_aggression() -> None:
    arr = _ramp_aggressive(0.5, 0.5)
    assert analyzer._classify_direction(arr) == "Steady"


def test_direction_old_bug_would_have_said_steady() -> None:
    """Proof the fix matters: averaging BOTH columns (the old code) of a clearly
    rising track yields ~0.5 start and end → 'Steady'. The fixed helper, which
    slices the aggressive column, returns 'Up' on the same input.
    """
    np = pytest.importorskip("numpy")
    arr = _ramp_aggressive(0.1, 0.9)
    # Reproduce the OLD buggy computation: mean over the whole slice (both cols).
    third = len(arr) // 3
    old_start = float(np.mean(arr[:third]))
    old_end = float(np.mean(arr[-third:]))
    assert abs(old_end - old_start) < 0.08  # old diff ~0 → "Steady"
    # New helper sees the real trend.
    assert analyzer._classify_direction(arr) == "Up"


def test_direction_handles_short_clip() -> None:
    np = pytest.importorskip("numpy")
    arr = np.array([[0.9, 0.1], [0.1, 0.9]], dtype=float)  # <3 frames → no thirds
    assert analyzer._classify_direction(arr) == "Steady"


def test_direction_handles_1d_input() -> None:
    """A 1-D energy curve is treated as the column directly (defensive path)."""
    np = pytest.importorskip("numpy")
    rising = np.linspace(0.1, 0.9, 30, dtype=float)
    assert analyzer._classify_direction(rising) == "Up"


def test_direction_never_raises_on_garbage() -> None:
    assert analyzer._classify_direction(None) == "Steady"
    assert analyzer._classify_direction("not an array") == "Steady"


# ---------------------------------------------------------------------------
# BPM octave-error guard (SOTA)
# ---------------------------------------------------------------------------


def test_bpm_none_passthrough() -> None:
    assert analyzer._snap_bpm_octave(None) is None
    assert analyzer._snap_bpm_octave(0) is None
    assert analyzer._snap_bpm_octave(-5) is None


def test_bpm_in_band_unchanged() -> None:
    assert analyzer._snap_bpm_octave(128.0) == 128.0
    assert analyzer._snap_bpm_octave(174.0) == 174.0


def test_bpm_folds_low_octave_into_band() -> None:
    # 65 BPM (below the 70 band floor) folds up to 130.
    assert analyzer._snap_bpm_octave(65.0) == 130.0


def test_bpm_folds_high_octave_into_band() -> None:
    # 348 (≥200) folds down: 348/2=174.
    assert analyzer._snap_bpm_octave(348.0) == 174.0


def test_bpm_70_to_140_via_filename() -> None:
    """Detector said 70, filename says 140 -> snap to 140 (the DJ's intent)."""
    assert analyzer._snap_bpm_octave(70.0, filename_bpm=140) == 140.0


def test_bpm_140_to_70_via_filename() -> None:
    """Detector said 140, filename says 70 -> snap to 70."""
    assert analyzer._snap_bpm_octave(140.0, filename_bpm=70) == 70.0


def test_bpm_87_to_174_via_filename() -> None:
    assert analyzer._snap_bpm_octave(87.0, filename_bpm=174) == 174.0


def test_bpm_174_to_87_via_filename() -> None:
    assert analyzer._snap_bpm_octave(174.0, filename_bpm=87) == 87.0


def test_bpm_filename_does_not_override_a_close_detection() -> None:
    """Detected 128, filename 130 (rounding) -> keep the detected octave (128),
    don't yank it to an octave just because the filename differs slightly."""
    assert analyzer._snap_bpm_octave(128.0, filename_bpm=130) == 128.0


# ---------------------------------------------------------------------------
# WSL version-drift guard (LOW): only block when STRICTLY older
# ---------------------------------------------------------------------------


def test_drift_equal_versions_not_outdated() -> None:
    assert analyzer._wsl_install_is_outdated("0.4.0-beta.2", "0.4.0b2") is False


def test_drift_older_wsl_is_outdated() -> None:
    assert analyzer._wsl_install_is_outdated("0.4.0b1", "0.4.0b2") is True


def test_drift_newer_wsl_is_not_outdated() -> None:
    """The whole point of the fix: a NEWER WSL install must be accepted."""
    assert analyzer._wsl_install_is_outdated("0.5.0", "0.4.0b2") is False


def test_drift_unparseable_falls_back_to_equality() -> None:
    # Garbage versions can't be PEP 440 parsed → fall back to normalized
    # equality (block on mismatch, accept on match).
    assert analyzer._wsl_install_is_outdated("garbage!!", "0.4.0b2") is True
    assert analyzer._wsl_install_is_outdated("weird", "weird") is False


# ---------------------------------------------------------------------------
# Class-index resolution (MED): resolve "voice" column by name, fallback index
# ---------------------------------------------------------------------------


def test_class_index_resolves_by_name() -> None:
    assert analyzer._class_index(["instrumental", "voice"], "voice", fallback=1) == 1
    assert analyzer._class_index(["voice", "instrumental"], "voice", fallback=1) == 0


def test_class_index_substring_match() -> None:
    # Essentia sometimes labels classes verbosely.
    assert analyzer._class_index(["Instrumental", "Voice / vocal"], "voice", fallback=0) == 1


def test_class_index_falls_back_when_missing() -> None:
    assert analyzer._class_index(None, "voice", fallback=1) == 1
    assert analyzer._class_index(["a", "b"], "voice", fallback=1) == 1
