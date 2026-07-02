"""Gold-corpus gate: the pure checker logic + the committed manifest's integrity.

The real end-to-end run (analyze the fixture clips, compare against the
manifest) happens in CI via `selftest-native --gold-dir` (Windows release
build) and scripts/rpc_smoke.py step 11 (native-smoke full tier). These tests
keep the shared checker honest without needing an ML engine, and pin the
fixtures/manifest contract so a renamed clip or malformed manifest fails fast
in the regular test matrix.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from vibechek.gold_gate import check_gold_report, load_manifest

FIXTURES = Path(__file__).parent / "fixtures" / "gold"


def _manifest(spec: dict) -> dict:
    return {"tracks": {"track.mp3": spec}}


def _report(ml: dict | None, *, error: str | None = None, filename: str = "track.mp3") -> dict:
    rec: dict = {"filename": filename, "ml_analysis": ml}
    if error:
        rec["error"] = error
    return {"tracks": [rec]}


GOOD_ML = {
    "ml_genre": "Trance",
    "ml_subgenre": "Trance",
    "ml_bpm": 139.9,
    "ml_key": "1A",
    "ml_energy": 3,
    "ml_mood": "Neutral",
    "ml_vocal": "Instrumental",
    "ml_danceability": 0.998,
}

FULL_SPEC = {
    "expect": {"ml_genre": "Trance", "ml_key": "1A"},
    "close": {"ml_bpm": {"value": 139.9, "tol": 1.0}, "ml_energy": {"value": 3, "tol": 1}},
    "min": {"ml_danceability": 0.85},
    "present": ["ml_mood", "ml_vocal"],
}


class TestCheckGoldReport:
    def test_all_assertions_pass(self):
        assert check_gold_report(_report(GOOD_ML), _manifest(FULL_SPEC)) == []

    def test_missing_track_fails(self):
        failures = check_gold_report({"tracks": []}, _manifest(FULL_SPEC))
        assert len(failures) == 1
        assert "not analyzed" in failures[0]

    def test_track_error_fails_and_short_circuits(self):
        failures = check_gold_report(
            _report(None, error="decode exploded"), _manifest(FULL_SPEC)
        )
        assert failures == ["track.mp3: track error: decode exploded"]

    def test_ml_error_fails(self):
        ml = {**GOOD_ML, "ml_error": "worker died"}
        failures = check_gold_report(_report(ml), _manifest(FULL_SPEC))
        assert failures == ["track.mp3: ml_error: worker died"]

    def test_expect_mismatch(self):
        ml = {**GOOD_ML, "ml_key": "2A"}
        failures = check_gold_report(_report(ml), _manifest(FULL_SPEC))
        assert failures == ["track.mp3: ml_key = '2A', expected '1A'"]

    def test_close_within_tolerance_passes(self):
        ml = {**GOOD_ML, "ml_bpm": 140.7}
        assert check_gold_report(_report(ml), _manifest(FULL_SPEC)) == []

    def test_close_out_of_tolerance_fails(self):
        # The classic regression this gate exists for: a half/double-tempo flip.
        ml = {**GOOD_ML, "ml_bpm": 69.95}
        failures = check_gold_report(_report(ml), _manifest(FULL_SPEC))
        assert len(failures) == 1
        assert "ml_bpm" in failures[0]

    def test_close_non_numeric_fails(self):
        ml = {**GOOD_ML, "ml_bpm": None}
        failures = check_gold_report(_report(ml), _manifest(FULL_SPEC))
        assert len(failures) == 1
        assert "ml_bpm" in failures[0]

    def test_min_floor(self):
        ml = {**GOOD_ML, "ml_danceability": 0.2}
        failures = check_gold_report(_report(ml), _manifest(FULL_SPEC))
        assert failures == ["track.mp3: ml_danceability = 0.2, expected >= 0.85"]

    def test_present_missing_and_empty(self):
        ml = {**GOOD_ML, "ml_mood": None, "ml_vocal": "  "}
        failures = check_gold_report(_report(ml), _manifest(FULL_SPEC))
        assert len(failures) == 2

    def test_multiple_failures_all_reported(self):
        ml = {**GOOD_ML, "ml_key": "9B", "ml_bpm": 200.0, "ml_mood": None}
        failures = check_gold_report(_report(ml), _manifest(FULL_SPEC))
        assert len(failures) == 3

    def test_extra_report_tracks_ignored(self):
        report = _report(GOOD_ML)
        report["tracks"].append({"filename": "unrelated.mp3", "error": "whatever"})
        assert check_gold_report(report, _manifest(FULL_SPEC)) == []


class TestCommittedManifest:
    """The fixtures/manifest contract actually committed to the repo."""

    def test_manifest_loads(self):
        manifest = load_manifest(FIXTURES)
        assert len(manifest["tracks"]) == 3

    def test_every_manifest_track_exists_on_disk(self):
        manifest = load_manifest(FIXTURES)
        for fname in manifest["tracks"]:
            assert (FIXTURES / fname).is_file(), f"fixture file missing: {fname}"

    def test_every_audio_fixture_is_in_the_manifest(self):
        manifest = load_manifest(FIXTURES)
        on_disk = {p.name for p in FIXTURES.glob("*.mp3")}
        assert on_disk == set(manifest["tracks"])

    def test_assertions_cover_genre_bpm_and_key(self):
        """The gate's whole point: at least one exact genre, every track a key
        + a BPM tolerance. Loosening the manifest to nothing must fail here."""
        manifest = load_manifest(FIXTURES)
        genres = [
            s for s in manifest["tracks"].values() if "ml_genre" in (s.get("expect") or {})
        ]
        assert len(genres) >= 2, "at least two tracks must assert an exact genre"
        for fname, spec in manifest["tracks"].items():
            assert "ml_key" in (spec.get("expect") or {}), f"{fname}: no key assertion"
            assert "ml_bpm" in (spec.get("close") or {}), f"{fname}: no BPM assertion"

    def test_every_track_has_attribution(self):
        manifest = load_manifest(FIXTURES)
        for fname, spec in manifest["tracks"].items():
            att = spec.get("attribution") or {}
            assert att.get("license") and att.get("source"), f"{fname}: attribution missing"

    def test_load_manifest_rejects_empty(self, tmp_path):
        (tmp_path / "manifest.json").write_text(json.dumps({"tracks": {}}))
        with pytest.raises(ValueError, match="no tracks"):
            load_manifest(tmp_path)


@pytest.mark.skipif(
    os.environ.get("VIBECHEK_GOLD_GATE") != "1",
    reason="integration run needs a real ML engine; set VIBECHEK_GOLD_GATE=1",
)
def test_gold_gate_end_to_end():
    """Opt-in local integration: full analyze of the committed clips."""
    from vibechek.gold_gate import run_gold_gate

    failures = run_gold_gate(FIXTURES, engine=os.environ.get("VIBECHEK_GOLD_ENGINE", ""))
    assert failures == []
