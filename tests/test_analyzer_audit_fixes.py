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


# ---------------------------------------------------------------------------
# Timeslot energy-0 coalesce (MED): energy 0 is a real value, not "missing"
# ---------------------------------------------------------------------------


def test_timeslot_energy_zero_is_not_treated_as_missing() -> None:
    """`_pick_timeslot` must honour a genuine energy of 0 (calmest tracks).

    The old call site used `result.ml_energy or 3`, so an energy-0 track was
    silently bumped to medium energy (3) and got the wrong timeslot.
    """
    # Generic energy-0 track: lowest energy → "Opener", not "Warm-Up".
    assert analyzer._pick_timeslot("Unknown", None, 0, 120.0) == "Opener"
    # The old `0 or 3` coalesce would have yielded "Warm-Up" — prove they differ.
    assert analyzer._pick_timeslot("Unknown", None, 3, 120.0) == "Warm-Up"
    assert analyzer._pick_timeslot("Unknown", None, 0, 120.0) != analyzer._pick_timeslot(
        "Unknown", None, 0 or 3, 120.0
    )


def test_timeslot_ambient_energy_zero_is_opener_not_afterhours() -> None:
    """An Ambient/Downtempo energy-0 track opens a set ("Opener"); the old
    `0 or 3` coalesce pushed energy to 3 → "Afterhours"."""
    assert analyzer._pick_timeslot("Ambient", "Dark Ambient", 0, 90.0) == "Opener"
    # Energy 3 (what the old bug fabricated) on the same genre → "Afterhours".
    assert analyzer._pick_timeslot("Ambient", "Dark Ambient", 3, 90.0) == "Afterhours"


# ---------------------------------------------------------------------------
# Timeslot subgenre special-cases (LOW): match Hard Techno/Deep House etc. on
# the SUBGENRE, since ml_genre carries the DJ-friendly PARENT genre.
# ---------------------------------------------------------------------------


def test_timeslot_hard_techno_subgenre_forces_peak() -> None:
    """A Hard Techno track surfaces as parent genre "Techno" + subgenre
    "Hard Techno". The Peak special-case must fire off the subgenre — matching
    the parent ("Techno") never would, so it used to fall through to "Warm-Up".
    """
    assert analyzer._pick_timeslot("Techno", "Hard Techno", 3, 130.0) == "Peak"
    # Plain (non-hard) Techno at the same energy stays "Warm-Up".
    assert analyzer._pick_timeslot("Techno", "Minimal Techno", 3, 130.0) == "Warm-Up"


def test_timeslot_gabber_hardstyle_subgenres_force_peak() -> None:
    """Gabber/Hardstyle arrive as subgenres under the "Hardcore" parent."""
    assert analyzer._pick_timeslot("Hardcore", "Gabber", 2, 180.0) == "Peak"
    assert analyzer._pick_timeslot("Hardcore", "Hardstyle", 2, 150.0) == "Peak"
    # The producible parent "Hardcore" itself is still peak material.
    assert analyzer._pick_timeslot("Hardcore", None, 2, 180.0) == "Peak"


def test_timeslot_trip_hop_subgenre_is_chill() -> None:
    """Trip Hop arrives as a subgenre under the "Downtempo" parent and should
    take the chill (Opener/Afterhours) branch."""
    assert analyzer._pick_timeslot("Downtempo", "Trip Hop", 1, 90.0) == "Opener"
    assert analyzer._pick_timeslot("Downtempo", "Trip Hop", 4, 90.0) == "Afterhours"


def test_timeslot_old_parent_match_was_dead() -> None:
    """Proof the fix matters: matching the hard styles against the parent genre
    (the old code) never fired, because get_best_genre yields the parent."""
    get_best_genre = pytest.importorskip("vibechek.genres").get_best_genre
    # A Hard Techno Discogs label resolves to parent "Techno", subgenre "Hard Techno".
    res = get_best_genre([0.9], ["Electronic---Hard Techno"])
    assert res.genre == "Techno"
    assert res.subgenre == "Hard Techno"
    # Matching the parent against the old ("Hard Techno",) tuple is a no-op,
    # but matching the subgenre forces Peak.
    assert analyzer._pick_timeslot(res.genre, res.subgenre, 3, 130.0) == "Peak"
