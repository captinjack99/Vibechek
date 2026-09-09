"""Regression tests for `vibechek.analyzer`.

Covered:
- `_classify_direction` — the softmax-column bug (was "Steady" for every
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


def _stepped_aggressive(first: float, last: float, frames: int = 30):
    """An (frames, 2) array whose aggressive column is exactly `first` over the
    first third and `last` over the last third, so the helper's measured delta
    is precisely `last - first` (independent of ramp geometry)."""
    np = pytest.importorskip("numpy")
    third = frames // 3
    mid = frames - 2 * third
    agg = np.concatenate([
        np.full(third, first),
        np.full(mid, (first + last) / 2),
        np.full(third, last),
    ])
    return np.stack([agg, 1.0 - agg], axis=1)


def test_direction_calibration_marginal_trend_stays_steady() -> None:
    """Locks the empirically-validated ±0.08 calibration (measured on 40 real
    tracks): a sub-threshold delta (~0.05 — a marginal wind-up/down) must read
    "Steady". DIRECTION_DELTA is intentionally precision-leaning; a careless
    lowering that flips this is the regression this guards. See the constant's
    comment."""
    assert analyzer.DIRECTION_DELTA == 0.08
    assert analyzer._classify_direction(_stepped_aggressive(0.50, 0.55)) == "Steady"
    assert analyzer._classify_direction(_stepped_aggressive(0.55, 0.50)) == "Steady"


def test_direction_calibration_clear_trend_is_directional() -> None:
    """The flip side: a delta at the probe's ~p75 magnitude (|Δ|≈0.12) is a real
    build/breakdown and must be labeled — the gate flags clear trends, not none."""
    assert analyzer._classify_direction(_stepped_aggressive(0.40, 0.52)) == "Up"
    assert analyzer._classify_direction(_stepped_aggressive(0.52, 0.40)) == "Down"


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


# ---------------------------------------------------------------------------
# A run where every ML pass failed must NOT report "N/N, 0 errors"
# ---------------------------------------------------------------------------


def _ml_failed_record(name: str) -> dict:
    """The record shape `analyze_track` produces for an ML failure: a truthy
    `ml_analysis` carrying only `ml_error`, and a record-level `error` of None
    (nothing anywhere promotes one to the other)."""
    return {
        "path": f"/lib/{name}",
        "filename": name,
        "extension": ".mp3",
        "size_mb": 4.2,
        "error": None,
        "ml_analysis": {"ml_error": "Could not decode audio: boom"},
    }


def _ml_ok_record(name: str) -> dict:
    return {
        "path": f"/lib/{name}",
        "filename": name,
        "extension": ".mp3",
        "size_mb": 4.2,
        "error": None,
        "ml_analysis": {"ml_genre": "Techno", "ml_bpm": 130.0, "ml_energy": 4},
    }


def test_ml_error_records_count_as_errors_not_as_analyzed() -> None:
    """Every track failed to decode: the summary must say so, or the GUI shows a
    green "Analyzed 5/5" toast over a library with no BPM, key or genre."""
    results = [_ml_failed_record(f"t{i}.mp3") for i in range(5)]
    summary = analyzer._build_report(results, 5, in_progress=False)["summary"]
    assert summary == {"total_files": 5, "analyzed": 0, "errors": 5}


def test_partial_ml_failure_splits_the_counts() -> None:
    results = [_ml_ok_record("a.mp3"), _ml_failed_record("b.mp3"),
               _ml_ok_record("c.mp3")]
    summary = analyzer._build_report(results, 3, in_progress=False)["summary"]
    assert summary == {"total_files": 3, "analyzed": 2, "errors": 1}


def test_record_level_error_is_still_counted_once() -> None:
    """A worker-death record (record-level `error`, no ml_analysis) keeps its
    old meaning, and a record carrying BOTH is not double-counted."""
    both = _ml_failed_record("d.mp3")
    both["error"] = "analysis worker died repeatedly on this track — skipped"
    results = [
        {"path": "/lib/e.mp3", "filename": "e.mp3", "extension": ".mp3",
         "size_mb": 0.0, "error": "boom"},
        both,
    ]
    summary = analyzer._build_report(results, 2, in_progress=False)["summary"]
    assert summary == {"total_files": 2, "analyzed": 0, "errors": 2}


@pytest.mark.parametrize("corrupt", ["legacy string", 42, ["ml_error"]])
def test_a_non_dict_ml_analysis_is_a_failure_not_a_crash(corrupt: object) -> None:
    """A hand-edited or half-written analysis JSON can carry a truthy
    `ml_analysis` that is not a dict. `(x or {}).get(...)` raised AttributeError
    on it, and that escaped as far as the dedupe path's summary recompute — which
    reports a failure AFTER the duplicates were already trashed. A row we cannot
    read is a row we cannot call analyzed."""
    row = _ml_ok_record("weird.mp3")
    row["ml_analysis"] = corrupt
    assert analyzer._record_failed(row) is True


@pytest.mark.parametrize("corrupt", ["legacy string", 42, ["ml_error"]])
def test_reconcile_skips_a_non_dict_ml_analysis_instead_of_crashing(corrupt: object) -> None:
    """The offline re-reconcile (priors import, conflict resolution, the
    incremental-analyze merge) walks every saved row; one corrupt row must not
    abort the whole pass with AttributeError. It is left untouched."""
    row = _ml_ok_record("weird.mp3")
    row["ml_analysis"] = corrupt
    analyzer._reconcile_record_genre(row, "prefer_tag", 0.90)
    assert row["ml_analysis"] == corrupt


# ---------------------------------------------------------------------------
# The online genre tier must say when it never reached the web
# ---------------------------------------------------------------------------


def _web_record(name: str, artist: str, title: str) -> dict:
    return {
        "path": f"/lib/{name}",
        "filename": name,
        "extension": ".mp3",
        "size_mb": 4.2,
        "existing_tags": {"artist": artist, "title": title},
        "ml_analysis": {"ml_genre": "House", "ml_genre_raw_confidence": 0.5},
    }


def _build_web_report(monkeypatch, resolve, records):
    from vibechek import genre_web

    monkeypatch.setattr(genre_web, "resolver_ready", lambda: True)
    monkeypatch.setattr(genre_web, "resolve", resolve)
    return analyzer._build_report(
        records, len(records), in_progress=False,
        web_cfg={"enabled": True, "backend": "ollama"},
    )


_RATELIMIT = "RatelimitException: Ratelimit: 202 https://duckduckgo.com/"
_DNS = "gaierror: [Errno -3] Temporary failure in name resolution"


def test_zero_web_reads_sets_the_degradation_warning(monkeypatch) -> None:
    """Packages installed, every search rate-limited: `resolve` returns
    used_web=False plus a REASON for every track. The progress line claims the
    tier ran, so the report has to carry the correction — and name the reason,
    because "you're rate-limited" and "you're offline" need opposite responses.
    """
    def resolve(_artist, _title, _tag, _audio, **_kw):
        return {"genre": "", "subgenre": "", "source_matched": False,
                "used_web": False, "web_unavailable": _RATELIMIT}

    records = [_web_record(f"t{i}.mp3", f"Artist {i}", f"Title {i}")
               for i in range(3)]
    events: list[tuple] = []
    monkeypatch.setattr(analyzer, "_emit_event",
                        lambda kind, **kw: events.append((kind, kw)))

    report = _build_web_report(monkeypatch, resolve, records)

    warning = report["genre_web_unavailable_warning"]
    assert "3 tracks" in warning
    assert _RATELIMIT in warning
    degraded = [kw for _k, kw in events if kw.get("name") == "genre_web_degraded"]
    assert degraded and degraded[0]["reason"] == _RATELIMIT
    assert degraded[0]["attempted"] == 3 and degraded[0]["unavailable"] == 3


def test_degradation_warning_names_the_most_common_reason(monkeypatch) -> None:
    """Mixed failures still get ONE banner; it must name the reason that hit the
    most tracks rather than whichever happened to come last."""
    calls = {"n": 0}

    def resolve(_artist, _title, _tag, _audio, **_kw):
        calls["n"] += 1
        # 3 rate-limited, then 1 DNS failure — every track unavailable.
        reason = _DNS if calls["n"] == 4 else _RATELIMIT
        return {"genre": "", "subgenre": "", "source_matched": False,
                "used_web": False, "web_unavailable": reason}

    records = [_web_record(f"t{i}.mp3", f"Artist {i}", f"Title {i}")
               for i in range(4)]
    warning = _build_web_report(
        monkeypatch, resolve, records,
    )["genre_web_unavailable_warning"]
    assert _RATELIMIT in warning
    assert _DNS not in warning


def test_empty_searches_are_a_clean_miss_not_a_degradation(monkeypatch) -> None:
    """Every search RAN and came back empty (`web_unavailable=""`). The web was
    reachable, so telling the user to check their connection sends them to fix a
    network that is working — the tier simply found no evidence."""
    def resolve(_artist, _title, _tag, _audio, **_kw):
        return {"genre": "", "subgenre": "", "source_matched": False,
                "used_web": False, "web_unavailable": ""}

    records = [_web_record(f"t{i}.mp3", f"Artist {i}", f"Title {i}")
               for i in range(3)]
    report = _build_web_report(monkeypatch, resolve, records)
    assert "genre_web_unavailable_warning" not in report


def test_a_partial_degradation_is_reported_with_its_proportion(monkeypatch) -> None:
    """One lucky early read must NOT silence the banner for everything after it.

    This is the real rate-limit shape: DuckDuckGo answers the first few tracks of
    a long run and throttles the rest. Gating the banner on "every track failed"
    meant a 2000-track library where 1999 were throttled reported nothing at all,
    and the user believed the verified-web tier had run on the whole library. The
    wording has to name the proportion — "couldn't reach the web" over a run that
    partly worked would overclaim in the other direction.
    """
    calls = {"n": 0}

    def resolve(_artist, _title, _tag, _audio, **_kw):
        calls["n"] += 1
        used = calls["n"] == 1
        return {"genre": "", "subgenre": "", "source_matched": False,
                "used_web": used,
                "web_unavailable": "" if used else _RATELIMIT}

    records = [_web_record(f"t{i}.mp3", f"Artist {i}", f"Title {i}")
               for i in range(10)]
    events: list[tuple] = []
    monkeypatch.setattr(analyzer, "_emit_event",
                        lambda kind, **kw: events.append((kind, kw)))

    report = _build_web_report(monkeypatch, resolve, records)

    warning = report["genre_web_unavailable_warning"]
    assert "9 of 10 tracks" in warning
    assert "any of" not in warning          # 1 track DID reach the web
    assert _RATELIMIT in warning
    degraded = [kw for _k, kw in events if kw.get("name") == "genre_web_degraded"]
    assert degraded and degraded[0]["attempted"] == 10
    assert degraded[0]["unavailable"] == 9 and degraded[0]["used"] == 1


def test_one_blip_in_a_long_run_is_recorded_but_not_warned_about(monkeypatch) -> None:
    """The banner is folded into the GUI's `degraded` flag, which turns the whole
    completion toast to "warning". Firing it on ANY non-zero count meant a single
    transient failure in a 2000-track run reported the entire analysis as
    degraded. The count is still recorded honestly — it just isn't announced."""
    calls = {"n": 0}

    def resolve(_artist, _title, _tag, _audio, **_kw):
        calls["n"] += 1
        failed = calls["n"] == 1
        return {"genre": "", "subgenre": "", "source_matched": False,
                "used_web": not failed,
                "web_unavailable": _RATELIMIT if failed else ""}

    records = [_web_record(f"t{i}.mp3", f"Artist {i}", f"Title {i}")
               for i in range(50)]
    events: list[tuple] = []
    monkeypatch.setattr(analyzer, "_emit_event",
                        lambda kind, **kw: events.append((kind, kw)))

    report = _build_web_report(monkeypatch, resolve, records)

    assert "genre_web_unavailable_warning" not in report
    assert not [kw for _k, kw in events if kw.get("name") == "genre_web_degraded"]
    # ...but the honest number is still in the report.
    assert report["genre_web_unavailable_count"] == 1
    assert report["genre_web_attempted"] == 50


@pytest.mark.parametrize(("unavailable", "attempted", "material"), [
    (1, 1, True),        # the whole (tiny) run failed
    (3, 3, True),
    (1, 50, False),      # one blip
    (2, 50, False),      # still under the floor of 3
    (3, 50, True),       # floor reached
    (2, 10, False),      # under the floor even though it is 20% of the run
    (5, 200, False),     # under 5%
    (10, 200, True),     # exactly 5%
    (0, 100, False),     # nothing failed
])
def test_the_degradation_threshold_is_a_share_with_a_floor(
    unavailable: int, attempted: int, material: bool,
) -> None:
    """Everything failing is always material; below that it takes 5% of the run,
    never fewer than three tracks."""
    assert analyzer._web_degradation_is_material(unavailable, attempted) is material


def test_web_lookup_off_never_claims_a_degradation(monkeypatch) -> None:
    records = [_web_record("t0.mp3", "A", "B")]
    report = analyzer._build_report(records, 1, in_progress=False)
    assert "genre_web_unavailable_warning" not in report


# ---------------------------------------------------------------------------
# Both confidence fields must describe the genre that was actually STORED
# ---------------------------------------------------------------------------


def _reconciled(ml: dict, tag: str | None, policy: str = "prefer_tag") -> dict:
    """Run one reconcile pass over a record built from `ml` + `tag`, in place."""
    rec = {"path": "/lib/a.mp3", "filename": "a.mp3",
           "existing_tags": {"genre": tag, "artist": "A", "title": "T"},
           "ml_analysis": ml}
    analyzer._reconcile_record_genre(rec, policy, 0.90)
    return rec["ml_analysis"]


def test_tag_sourced_genre_carries_the_tags_confidence_not_the_audios() -> None:
    """A curated tag is what gets stored, so `ml_genre_raw_confidence` must stop
    reporting the audio model's 0.2 for it — read as "how sure are we about this
    genre?" it was answering about a genre that isn't in the record."""
    ml = _reconciled(
        {"ml_genre": "House", "ml_subgenre": "Deep House",
         "ml_genre_confidence": 0.55, "ml_genre_raw_confidence": 0.2},
        "Techno",
    )
    assert ml["ml_genre_source"] == "tag"
    assert ml["ml_genre"] == "Techno"
    assert ml["ml_genre_raw_confidence"] == ml["ml_genre_confidence"] == 0.99
    # The audio read itself is not lost — it moves to the pure-audio stash.
    assert ml["ml_genre_audio"] == "House"
    assert ml["ml_genre_audio_confidence"] == 0.2


def test_audio_sourced_genre_keeps_the_audio_confidence() -> None:
    """No usable tag → the audio read IS the stored genre, so the field keeps
    describing it (and stays the single-class score the gate was tuned on)."""
    ml = _reconciled(
        {"ml_genre": "House", "ml_subgenre": "Deep House",
         "ml_genre_confidence": 0.55, "ml_genre_raw_confidence": 0.2},
        None,
    )
    assert ml["ml_genre_source"] == "ml"
    assert ml["ml_genre_raw_confidence"] == 0.2


def test_reconcile_is_idempotent_after_rewriting_the_confidence() -> None:
    """The rewritten field must never be fed back in as the AUDIO confidence: a
    tag's 0.99 clears the 0.90 ml_override gate, so a second pass (priors import,
    incremental-analyze merge, conflict resolution) would overturn the tag it
    just stored and re-open the review queue on a record nothing changed."""
    ml = {"ml_genre": "House", "ml_subgenre": "Deep House",
          "ml_genre_confidence": 0.55, "ml_genre_raw_confidence": 0.2}
    first = dict(_reconciled(ml, "Techno"))
    second = _reconciled(ml, "Techno")
    assert second["ml_genre_source"] == "tag"
    assert second["ml_genre"] == "Techno"
    assert second == first


def test_legacy_record_without_a_raw_confidence_never_gains_one() -> None:
    """Its ABSENCE is how tagger.py/genreGate.ts detect a pre-two-stage report
    and hold back the parent-genre fallback; inventing one here would silently
    write coarser genres to files on a plain re-apply."""
    ml = _reconciled(
        {"ml_genre": "House", "ml_subgenre": "Deep House",
         "ml_genre_confidence": 0.55},
        "Techno",
    )
    assert ml["ml_genre_source"] == "tag"
    assert "ml_genre_raw_confidence" not in ml
    assert "ml_genre_audio_confidence" not in ml


# ---------------------------------------------------------------------------
# Cancel / stall-abort must not throw away every completed track
# ---------------------------------------------------------------------------


def _cancel_after(n: int, records: list):
    """An `analyze_track` stand-in that requests cancellation after `n` files."""
    from vibechek import cancellation

    def fake(filepath, _models):
        rec = analyzer.TrackAnalysis(
            path=str(filepath), filename=filepath.name,
            extension=filepath.suffix.lower(), size_mb=1.0,
            ml_analysis={"ml_genre": "Techno", "ml_bpm": 130.0},
        )
        records.append(rec.path)
        if len(records) >= n:
            cancellation.cancel()
        return rec
    return fake


def _run_cancelled_analyze(tmp_path, monkeypatch, output_path):
    """Drive the real in-process single-worker loop until a cancel fires."""
    from unittest.mock import MagicMock, patch

    from vibechek import cancellation
    from vibechek.config import AnalysisConfig

    files = []
    for i in range(4):
        f = tmp_path / f"t{i}.flac"
        f.write_bytes(b"\x00")
        files.append(f)

    done: list[str] = []
    monkeypatch.setattr(analyzer, "load_models", lambda *a, **kw: {"effnet": object()})
    monkeypatch.setattr(analyzer, "analyze_track", _cancel_after(2, done))

    cancellation.begin("analyze", "op-test")
    try:
        with patch("vibechek.preflight.preflight",
                   return_value=MagicMock(ready=True, analyze_via="native",
                                          reasons_not_ready=[])), \
             patch("vibechek.utils.find_audio_files", return_value=files):
            with pytest.raises(cancellation.CancelledError) as excinfo:
                analyzer.analyze_directory(
                    tmp_path,
                    config=AnalysisConfig(workers=1, use_gpu="off",
                                          inference_engine="essentia_tf"),
                    output_path=output_path,
                )
    finally:
        cancellation.end()
    return excinfo.value, done


def test_cancel_writes_the_partial_report_to_output_path(tmp_path, monkeypatch) -> None:
    """The two finished tracks must be on disk before the exception unwinds —
    a checkpoint that only exists on the happy path is not a checkpoint."""
    import json

    output = tmp_path / "analysis.json"
    exc, done = _run_cancelled_analyze(tmp_path, monkeypatch, output)

    assert len(done) == 2
    assert output.exists(), "cancel discarded the finished tracks"
    saved = json.loads(output.read_text(encoding="utf-8"))
    assert saved["status"] == "in_progress"
    assert [t["path"] for t in saved["tracks"]] == done


def test_cancel_attaches_the_partial_report_even_without_an_output_path(
    tmp_path, monkeypatch,
) -> None:
    """The GUI's caller may not have configured a file; it still needs the
    finished records, so they ride out on the exception."""
    exc, done = _run_cancelled_analyze(tmp_path, monkeypatch, None)

    partial = getattr(exc, "partial_report", None)
    assert partial is not None, "no partial_report attached to the cancel"
    assert partial["status"] == "in_progress"
    assert [t["path"] for t in partial["tracks"]] == done
    assert partial["summary"]["total_files"] == 4


def test_partial_build_does_not_re_raise_the_cancel(tmp_path) -> None:
    """`_build_report(in_progress=True)` skips the reconcile loop's own
    cancellation.check(), so building the partial can't raise the very cancel
    we're already handling."""
    from vibechek import cancellation

    results = [_ml_ok_record("a.mp3")]
    cancellation.begin("analyze", "op-1")
    cancellation.cancel()
    try:
        report = analyzer._build_report(results, 3, in_progress=True)
        assert len(report["tracks"]) == 1
        # ...while the FINAL build is still a cancel point, as designed.
        with pytest.raises(cancellation.CancelledError):
            analyzer._build_report(results, 3, in_progress=False)
    finally:
        cancellation.end()


def test_abort_before_any_track_does_not_overwrite_an_existing_report(
    tmp_path,
) -> None:
    """An abort during model load has nothing to save. Writing a zero-track
    report over the user's previous analysis.json would be worse than the loss
    this whole path exists to prevent."""
    import json

    from vibechek import cancellation

    output = tmp_path / "analysis.json"
    output.write_text(json.dumps({"status": "complete", "tracks": [{"path": "old"}]}),
                      encoding="utf-8")

    exc = cancellation.CancelledError("cancelled")
    analyzer._persist_partial_on_abort(exc, output, [], 12)

    assert json.loads(output.read_text(encoding="utf-8"))["tracks"] == [{"path": "old"}]
    assert not hasattr(exc, "partial_report")


# ---------------------------------------------------------------------------
# A partial ML read is DEGRADED, not FAILED
# ---------------------------------------------------------------------------


def _ml_degraded_record(name: str) -> dict:
    """The shape `_finish_ml_result` produces when the EffNet embedding fails
    but the model-free BPM/key extractors succeeded."""
    return {
        "path": f"/lib/{name}",
        "filename": name,
        "extension": ".flac",
        "size_mb": 32.0,
        "error": None,
        "ml_analysis": {
            "ml_bpm": 128.0,
            "ml_key": "8A",
            "ml_error": None,
            "ml_degraded_heads": [analyzer.EMBEDDING_HEAD],
        },
    }


def test_embedding_failure_that_kept_bpm_and_key_is_degraded_not_failed() -> None:
    """The record `analyze_audio_features` goes out of its way to keep — real
    BPM, real key, no embedding — must NOT be counted as an error.

    It used to carry `ml_error`, which `_record_failed` counts as a failure and
    the library view mirrors as "never analyzed": every incremental "Analyze
    new" re-decoded the same broken file forever and the counter never drained.
    """
    row = _ml_degraded_record("broken.flac")
    assert analyzer._record_failed(row) is False

    summary = analyzer._build_report(
        [_ml_ok_record("a.mp3"), row], 2, in_progress=False,
    )["summary"]
    assert summary == {"total_files": 2, "analyzed": 2, "errors": 0}


def test_a_read_with_nothing_usable_still_counts_as_an_error() -> None:
    """The other half of the same rule: no BPM and no key means nothing came out
    of the file, so `ml_error` stands and the track is a failure."""
    row = _ml_failed_record("dead.flac")
    assert analyzer._record_failed(row) is True
    summary = analyzer._build_report([row], 1, in_progress=False)["summary"]
    assert summary == {"total_files": 1, "analyzed": 0, "errors": 1}


def test_finish_ml_result_keeps_bpm_and_names_the_missing_head() -> None:
    """The producer side of both shapes, straight through the helper."""
    partial = analyzer.MLResult(ml_bpm=128.0, ml_key="8A")
    out = analyzer._finish_ml_result(partial, {}, "EffNet embedding failed: boom")
    assert out.ml_error is None
    assert out.ml_degraded_heads == [analyzer.EMBEDDING_HEAD]

    total = analyzer.MLResult()
    out2 = analyzer._finish_ml_result(total, {}, "EffNet embedding failed: boom")
    assert out2.ml_error == "EffNet embedding failed: boom"
    assert out2.ml_degraded_heads is None


def test_finish_ml_result_still_stamps_worker_wide_load_failures() -> None:
    """Heads that failed to LOAD are worker-wide and must keep riding out on
    every track, embedding failure or not."""
    ok = analyzer._finish_ml_result(
        analyzer.MLResult(ml_bpm=120.0), {"_degraded_heads": ["happy"]}, None,
    )
    assert ok.ml_degraded_heads == ["happy"]
    assert ok.ml_error is None

    both = analyzer._finish_ml_result(
        analyzer.MLResult(ml_bpm=120.0), {"_degraded_heads": ["happy"]},
        "EffNet embedding model not loaded",
    )
    assert both.ml_degraded_heads == ["happy", analyzer.EMBEDDING_HEAD]


def test_per_track_embedding_failure_does_not_claim_every_track_used_a_fallback() -> None:
    """`degraded_heads` drives a "re-download your models" banner about the
    WHOLE run — the right message for a head that failed to load, and a lie for
    one damaged file in a library. The two are counted separately."""
    results = [_ml_ok_record(f"ok{i}.mp3") for i in range(4)]
    results.append(_ml_degraded_record("broken.flac"))
    report = analyzer._build_report(results, 5, in_progress=False)
    warning = report["model_degradation_warning"]
    assert "1 of 5 tracks couldn't be fully analyzed" in warning
    assert "Download models in Settings" not in warning
    # And it is a degradation notice, not an error count.
    assert report["summary"] == {"total_files": 5, "analyzed": 5, "errors": 0}


def test_a_head_that_failed_to_load_still_gets_its_own_banner() -> None:
    row = _ml_ok_record("a.mp3")
    row["ml_analysis"]["ml_degraded_heads"] = ["happy", "sad"]
    warning = analyzer._build_report(
        [row], 1, in_progress=False,
    )["model_degradation_warning"]
    assert "Download models in Settings" in warning
    assert "couldn't be fully analyzed" not in warning
