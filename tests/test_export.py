"""Tests for the `vibechek export` CLI subcommand."""

from __future__ import annotations

import csv
import json
from pathlib import Path

from click.testing import CliRunner

from vibechek.cli import _EXPORT_CSV_COLUMNS, _track_to_csv_row, main


def _sample_analysis(tmp_path: Path) -> Path:
    """Write a minimal analysis.json with a mix of complete + error tracks."""
    data = {
        "status": "complete",
        "tracks": [
            {
                "path": str(tmp_path / "a.mp3"),
                "filename": "a.mp3",
                "extension": ".mp3",
                "size_mb": 8.4,
                "existing_tags": {"genre": "House", "bpm": 124, "key": "8A"},
                "ml_analysis": {
                    "ml_genre": "House",
                    "ml_subgenre": "Deep House",
                    "ml_genre_confidence": 0.91,
                    "ml_bpm": 124.5,
                    "ml_key": "8A",
                    "ml_energy": 4,
                    "ml_mood": "Bright",
                    "ml_timeslot": "Peak",
                    "ml_direction": "Steady",
                    "ml_vocal": "Vocal",
                    "ml_danceability": 0.88,
                },
            },
            {
                "path": str(tmp_path / "b.flac"),
                "filename": "b.flac",
                "extension": ".flac",
                "size_mb": 22.1,
                "existing_tags": {},
                "ml_analysis": None,
                "error": "could not decode",
            },
        ],
    }
    fp = tmp_path / "analysis.json"
    fp.write_text(json.dumps(data), encoding="utf-8")
    return fp


def test_track_to_csv_row_flattens_nested_ml() -> None:
    row = _track_to_csv_row({
        "path": "/x/a.mp3", "filename": "a.mp3", "extension": ".mp3", "size_mb": 5,
        "existing_tags": {"genre": "House"},
        "ml_analysis": {"ml_genre": "House", "ml_subgenre": "Deep House"},
    })
    assert row["existing_genre"] == "House"
    assert row["ml_genre"] == "House"
    assert row["ml_subgenre"] == "Deep House"
    assert row["error"] == ""


def test_track_to_csv_row_surfaces_track_or_ml_error() -> None:
    row = _track_to_csv_row({"path": "/x/a.mp3", "filename": "a.mp3",
                             "extension": ".mp3", "size_mb": 0, "error": "missing"})
    assert row["error"] == "missing"

    ml_err = _track_to_csv_row({"path": "/x/a.mp3", "filename": "a.mp3",
                                "extension": ".mp3", "size_mb": 0,
                                "ml_analysis": {"ml_error": "decoder failed"}})
    assert ml_err["error"] == "decoder failed"


def test_export_csv(tmp_path: Path) -> None:
    analysis_file = _sample_analysis(tmp_path)
    output = tmp_path / "tracks.csv"

    runner = CliRunner()
    result = runner.invoke(main, ["export", str(analysis_file), "--format", "csv",
                                  "--output", str(output)])
    assert result.exit_code == 0, result.output
    assert output.exists()

    with output.open(encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        rows = list(reader)
        # Column ordering preserved
        assert reader.fieldnames == _EXPORT_CSV_COLUMNS
    assert len(rows) == 2
    assert rows[0]["filename"] == "a.mp3"
    assert rows[0]["ml_genre"] == "House"
    assert rows[1]["error"] == "could not decode"


def test_export_json_passthrough(tmp_path: Path) -> None:
    analysis_file = _sample_analysis(tmp_path)
    output = tmp_path / "out.json"

    runner = CliRunner()
    result = runner.invoke(main, ["export", str(analysis_file), "--format", "json",
                                  "--output", str(output)])
    assert result.exit_code == 0, result.output
    reloaded = json.loads(output.read_text(encoding="utf-8"))
    assert reloaded["tracks"][0]["filename"] == "a.mp3"


def test_export_m3u8_lists_paths(tmp_path: Path) -> None:
    analysis_file = _sample_analysis(tmp_path)
    output = tmp_path / "playlist.m3u8"

    runner = CliRunner()
    result = runner.invoke(main, ["export", str(analysis_file), "--format", "m3u8",
                                  "--output", str(output)])
    assert result.exit_code == 0, result.output
    text = output.read_text(encoding="utf-8")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    assert len(lines) == 2
    assert lines[0].endswith("a.mp3")
    assert lines[1].endswith("b.flac")


def test_export_defaults_output_path(tmp_path: Path) -> None:
    """If `--output` is omitted, the file is sibling to the input."""
    analysis_file = _sample_analysis(tmp_path)
    runner = CliRunner()
    result = runner.invoke(main, ["export", str(analysis_file), "--format", "csv"])
    assert result.exit_code == 0, result.output
    assert (tmp_path / "analysis.csv").exists()


# ---------------------------------------------------------------------------
# verify-models
# ---------------------------------------------------------------------------


def test_verify_models_prints_computed_hashes_when_no_expected_table(tmp_path: Path) -> None:
    """Without `MODEL_SHA256` defined in analyzer, we just print hashes.

    Exit code is non-zero because at least one model file is missing in the
    isolated test data dir — that's the expected behaviour (the CLI flags
    missing files as failures so CI scripts can detect them).
    """
    # Drop a couple of fake "model" files so we get OK-ish hash lines mixed
    # with the MISSING lines.
    models_dir = tmp_path / "models"
    models_dir.mkdir()
    (models_dir / "effnet.pb").write_bytes(b"fake weights")
    (models_dir / "effnet.json").write_text("{}", encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(main, ["verify-models", "--models-dir", str(models_dir)])
    # Other models are missing so we expect a non-zero exit.
    assert result.exit_code != 0
    assert "effnet.pb" in result.output
    assert "sha256=" in result.output  # printed computed hash for at least one file
    assert "MISSING" in result.output  # at least one other model file is absent
