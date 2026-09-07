"""Tests for the `vibechek export` CLI subcommand."""

from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
from unittest import mock

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


def test_export_json_refuses_to_overwrite_its_own_input(tmp_path: Path) -> None:
    """`export analysis.json --format json` must not truncate analysis.json.

    `Path("analysis.json").with_suffix(".json")` is the input path, so the
    default-output json export opened the report for writing — 30+ min of
    analysis gone if that write is interrupted.
    """
    analysis_file = _sample_analysis(tmp_path)
    before = analysis_file.read_bytes()

    runner = CliRunner()
    result = runner.invoke(main, ["export", str(analysis_file), "--format", "json"])
    assert result.exit_code != 0
    assert "Refusing to write over the source" in result.output
    assert analysis_file.read_bytes() == before


def test_export_counts_only_the_rows_it_wrote(tmp_path: Path) -> None:
    """Non-dict entries are skipped by the writers, so they mustn't be counted."""
    analysis_file = tmp_path / "mixed.json"
    analysis_file.write_text(
        json.dumps({"tracks": [123, "s", {"path": "/x.mp3"}, {"nopath": 1}]}),
        encoding="utf-8",
    )
    output = tmp_path / "mixed.m3u8"

    runner = CliRunner()
    result = runner.invoke(main, ["export", str(analysis_file), "--format", "m3u8",
                                  "--output", str(output)])
    assert result.exit_code == 0, result.output
    # rich wraps the console at 80 columns and macOS's pytest tmp paths are
    # long enough to push "skipped" onto the next line — compare on collapsed
    # whitespace so the assertion is about the words, not the line breaks.
    flat = " ".join(result.output.split())
    assert "Exported 1 tracks" in flat
    assert "3 unusable entries skipped" in flat


def test_export_rejects_a_non_list_tracks_field(tmp_path: Path) -> None:
    """A hand-edited `"tracks": "pending"` used to export 0 rows and claim 7."""
    analysis_file = tmp_path / "analysis.json"
    analysis_file.write_text(json.dumps({"tracks": "pending"}), encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(main, ["export", str(analysis_file), "--format", "csv"])
    assert result.exit_code != 0
    assert "'tracks' field must be a list" in result.output
    assert not (tmp_path / "analysis.csv").exists()


# ---------------------------------------------------------------------------
# verify-models
# ---------------------------------------------------------------------------


def test_verify_models_flags_tampered_weights(tmp_path: Path) -> None:
    """A `.pb` whose content doesn't match the pinned digest must say MISMATCH.

    The command looked its expected hashes up by FILENAME ("effnet.pb") in a
    table keyed by model NAME, so every comparison silently returned None and
    the tamper check this command exists for never ran — 16 benign
    "sha256=..." lines and exit 0 over a fully poisoned model set.
    """
    models_dir = tmp_path / "models"
    models_dir.mkdir()
    (models_dir / "effnet.pb").write_bytes(b"fake weights")
    (models_dir / "effnet.json").write_text("{}", encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(
        main,
        ["verify-models", "--models-dir", str(models_dir), "--engine", "essentia_tf"],
    )
    assert result.exit_code != 0
    assert "effnet.pb" in result.output
    assert "MISMATCH" in result.output  # the pinned digest WAS compared
    assert "MISSING" in result.output  # the other models are absent


def test_verify_models_reports_ok_for_a_file_matching_its_pin(tmp_path: Path) -> None:
    """A file whose bytes hash to the pinned digest reports OK, not a bare hash."""
    from vibechek.analyzer import MODEL_SHA256

    models_dir = tmp_path / "models"
    models_dir.mkdir()
    payload = b"pretend this is the real graph"
    digest = hashlib.sha256(payload).hexdigest()
    (models_dir / "effnet.pb").write_bytes(payload)

    runner = CliRunner()
    with mock.patch.dict(
        MODEL_SHA256, {"effnet": {**MODEL_SHA256["effnet"], "pb": digest}}, clear=False
    ):
        result = runner.invoke(
            main,
            ["verify-models", "--models-dir", str(models_dir), "--engine", "essentia_tf"],
        )
    assert "effnet.pb: OK" in result.output


def test_verify_models_onnx_engine_checks_the_onnx_subdir(tmp_path: Path) -> None:
    """`--engine onnx/native` must verify <models>/onnx, not the essentia .pb set.

    `download_models` skips the `.pb` set entirely for the ONNX family, so
    checking it reported all 16 files MISSING on a healthy Windows (native)
    install — the platform default.
    """
    from vibechek.analyzer import _ONNX_HEAD_STEMS, _ONNX_SUBDIR
    from vibechek.onnx_backend import BACKBONE_ONNX_FILENAME

    models_dir = tmp_path / "models"
    onnx_dir = models_dir / _ONNX_SUBDIR
    onnx_dir.mkdir(parents=True)
    (onnx_dir / BACKBONE_ONNX_FILENAME).write_bytes(b"backbone")
    for stem in _ONNX_HEAD_STEMS:
        (onnx_dir / f"{stem}.onnx").write_bytes(b"head")
        (onnx_dir / f"{stem}.json").write_text("{}", encoding="utf-8")

    runner = CliRunner()
    for engine in ("onnx", "native"):
        result = runner.invoke(
            main, ["verify-models", "--models-dir", str(models_dir), "--engine", engine]
        )
        # Every ONNX file is present, so nothing is MISSING; the contents are
        # wrong, so they're MISMATCHes (proving the hashes are compared).
        assert "MISSING" not in result.output, result.output
        assert "effnet.pb" not in result.output, result.output
        assert BACKBONE_ONNX_FILENAME in result.output
        assert "MISMATCH" in result.output


def test_verify_models_prints_computed_hashes_when_no_expected_table(tmp_path: Path) -> None:
    """With no pinned digests we print the computed hashes and say so.

    Exit code is non-zero because at least one model file is missing in the
    isolated test data dir — that's the expected behaviour (the CLI flags
    missing files as failures so CI scripts can detect them).
    """
    from vibechek.analyzer import MODEL_SHA256

    models_dir = tmp_path / "models"
    models_dir.mkdir()
    (models_dir / "effnet.pb").write_bytes(b"fake weights")
    (models_dir / "effnet.json").write_text("{}", encoding="utf-8")

    runner = CliRunner()
    with mock.patch.dict(MODEL_SHA256, {}, clear=True):
        result = runner.invoke(
            main,
            ["verify-models", "--models-dir", str(models_dir), "--engine", "essentia_tf"],
        )
    # Other models are missing so we expect a non-zero exit.
    assert result.exit_code != 0
    assert "No pinned hashes" in result.output
    assert "effnet.pb" in result.output
    assert "sha256=" in result.output  # printed computed hash for at least one file
    assert "MISSING" in result.output  # at least one other model file is absent


# ---------------------------------------------------------------------------
# cdj-export — the summary must surface what the export already recorded
# ---------------------------------------------------------------------------


def _flat(text: str) -> str:
    """Collapse Rich's soft wrapping so assertions can name a whole phrase."""
    return " ".join(text.split())


def test_cdj_export_summary_warns_about_downsampled_tracks(tmp_path: Path) -> None:
    """`result.resampled` records an IRREVERSIBLE quality reduction (a hi-res
    source, or an unprobeable one that got the CDJ-safe 44.1 kHz forced on it).
    The CLI dropped the list on the floor, so a DJ exporting a 96 kHz library
    was never told their AIFFs aren't the masters."""
    from vibechek.cdj_export import CdjExportResult

    xml = tmp_path / "collection.xml"
    xml.write_text("<DJ_PLAYLISTS/>", encoding="utf-8")
    out_dir = tmp_path / "out"
    result_obj = CdjExportResult(
        flac_converted=2, flac_planned=2, output_xml=out_dir / "rekordbox_cdj.xml",
        out_dir=out_dir, resampled=["hires.flac", "unprobeable.flac"],
    )
    with mock.patch("vibechek.cdj_export.export_for_cdj", return_value=result_obj):
        result = CliRunner().invoke(
            main, ["cdj-export", str(xml), "--out", str(out_dir)]
        )
    assert result.exit_code == 0, result.output
    out = _flat(result.output)
    assert "Down-sampled 2 track(s)" in out
    assert "hires.flac" in out
    assert "unprobeable.flac" in out


def test_cdj_export_summary_lists_disambiguated_names(tmp_path: Path) -> None:
    """`result.renamed` holds destinations that had to become `_1`/`_2` because
    out_dir already held the name (or two sources share a stem). The XML points
    at the new name, so silence left the user unable to explain the mismatch —
    and worried something was overwritten. Nothing was."""
    from vibechek.cdj_export import CdjExportResult

    xml = tmp_path / "collection.xml"
    xml.write_text("<DJ_PLAYLISTS/>", encoding="utf-8")
    out_dir = tmp_path / "out"
    result_obj = CdjExportResult(
        flac_converted=1, flac_planned=1, output_xml=out_dir / "rekordbox_cdj.xml",
        out_dir=out_dir, renamed=["Track_1.aiff"],
    )
    with mock.patch("vibechek.cdj_export.export_for_cdj", return_value=result_obj):
        result = CliRunner().invoke(
            main, ["cdj-export", str(xml), "--out", str(out_dir)]
        )
    assert result.exit_code == 0, result.output
    out = _flat(result.output)
    assert "Renamed 1 destination file(s)" in out
    assert "nothing was overwritten" in out.lower()
    assert "Track_1.aiff" in out


def test_cdj_export_summary_silent_when_nothing_resampled_or_renamed(
    tmp_path: Path,
) -> None:
    """A clean run must not grow two empty sections."""
    from vibechek.cdj_export import CdjExportResult

    xml = tmp_path / "collection.xml"
    xml.write_text("<DJ_PLAYLISTS/>", encoding="utf-8")
    out_dir = tmp_path / "out"
    result_obj = CdjExportResult(
        flac_converted=1, flac_planned=1, output_xml=out_dir / "rekordbox_cdj.xml",
        out_dir=out_dir,
    )
    with mock.patch("vibechek.cdj_export.export_for_cdj", return_value=result_obj):
        result = CliRunner().invoke(
            main, ["cdj-export", str(xml), "--out", str(out_dir)]
        )
    assert result.exit_code == 0, result.output
    out = _flat(result.output)
    assert "Down-sampled" not in out
    assert "Renamed" not in out


def test_cdj_export_dry_run_still_reports_planned_renames(tmp_path: Path) -> None:
    """Renames are decided in the planning pass, so a dry run knows them — and
    "which names will change" is exactly what a dry run is for."""
    from vibechek.cdj_export import CdjExportResult

    xml = tmp_path / "collection.xml"
    xml.write_text("<DJ_PLAYLISTS/>", encoding="utf-8")
    out_dir = tmp_path / "out"
    result_obj = CdjExportResult(flac_planned=1, out_dir=out_dir, renamed=["Track_1.aiff"])
    with mock.patch("vibechek.cdj_export.export_for_cdj", return_value=result_obj):
        result = CliRunner().invoke(
            main, ["cdj-export", str(xml), "--out", str(out_dir), "--dry-run"]
        )
    assert result.exit_code == 0, result.output
    assert "Track_1.aiff" in _flat(result.output)


def test_verify_models_calls_best_effort_onnx_files_optional(tmp_path: Path) -> None:
    """`download-models` marks some ONNX files best-effort — a failed fetch of
    genre_discogs400.onnx or any non-genre head's class-label .json does NOT
    make the download fail. Verifying them as required meant a healthy install
    reported MISSING and exited 1 for files it was never going to have.
    """
    from vibechek.analyzer import _ONNX_HEAD_STEMS, _ONNX_SUBDIR, MODEL_SHA256_ONNX
    from vibechek.cli import _optional_onnx_filenames
    from vibechek.onnx_backend import BACKBONE_ONNX_FILENAME

    models_dir = tmp_path / "models"
    onnx_dir = models_dir / _ONNX_SUBDIR
    onnx_dir.mkdir(parents=True)

    optional = _optional_onnx_filenames()
    # Write every REQUIRED file; leave every optional one absent.
    written: dict[str, str] = {}
    for fname in [
        BACKBONE_ONNX_FILENAME,
        *(f"{s}.{x}" for s in _ONNX_HEAD_STEMS for x in ("onnx", "json")),
    ]:
        if fname in optional:
            continue
        payload = f"bytes of {fname}".encode()
        (onnx_dir / fname).write_bytes(payload)
        written[fname] = hashlib.sha256(payload).hexdigest()

    pins = {**MODEL_SHA256_ONNX, **{k: v for k, v in written.items()}}
    with (
        mock.patch.dict(MODEL_SHA256_ONNX, pins, clear=True),
        mock.patch("vibechek.model_download.BACKBONE_ONNX_SHA256",
                   written[BACKBONE_ONNX_FILENAME]),
    ):
        result = CliRunner().invoke(
            main,
            ["verify-models", "--models-dir", str(models_dir), "--engine", "onnx"],
        )

    assert result.exit_code == 0, result.output
    assert "MISSING" not in result.output
    assert "optional-missing" in result.output
    # ...and the required set really was checked, not skipped.
    assert f"{BACKBONE_ONNX_FILENAME}: OK" in result.output
    assert "genre_discogs400.json: OK" in result.output


def test_verify_models_still_fails_for_a_missing_required_onnx_file(
    tmp_path: Path,
) -> None:
    """Only the best-effort set is forgiven; a required head is still a failure."""
    from vibechek.analyzer import _ONNX_SUBDIR

    models_dir = tmp_path / "models"
    (models_dir / _ONNX_SUBDIR).mkdir(parents=True)

    result = CliRunner().invoke(
        main, ["verify-models", "--models-dir", str(models_dir), "--engine", "onnx"]
    )
    assert result.exit_code != 0
    assert "mood_happy.onnx: MISSING" in result.output
    # ...while its class-label json is the best-effort one.
    assert "mood_happy.json: optional-missing" in result.output


def test_cli_and_rpc_agree_on_the_optional_onnx_set() -> None:
    """The two verifiers must forgive exactly the same files, and exactly the
    ones model_download.py fetches best-effort — drift here means the GUI and
    the CLI disagree about whether an install is healthy."""
    from vibechek import rpc
    from vibechek.cli import _optional_onnx_filenames

    assert _optional_onnx_filenames() == rpc._optional_onnx_filenames()
    assert _optional_onnx_filenames() == {
        "genre_discogs400.onnx",
        "danceability.json",
        "voice_instrumental.json",
        "mood_aggressive.json",
        "mood_happy.json",
        "mood_relaxed.json",
        "mood_sad.json",
    }
