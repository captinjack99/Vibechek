"""WP9: the durable analyze-run log (`run_history.jsonl`) and the WSL engine
self-heal notices threaded onto the analyze report.

Both exist so a degradation the user would otherwise never see — a GPU that
silently fell back to CPU after an auto-repair, or "what did my last run
actually decide?" — has a place to be read back from (the completion toast and
`doctor`'s last-run section).
"""

from __future__ import annotations

from vibechek import logging_setup
from vibechek.config import AnalysisConfig

# ---------------------------------------------------------------------------
# run-history writer (logging_setup)
# ---------------------------------------------------------------------------


def test_append_run_summary_caps_to_last_n() -> None:
    for i in range(60):
        logging_setup.append_run_summary({"n": i}, cap=50)
    hist = logging_setup.read_run_history(1000)
    assert len(hist) == 50
    assert hist[0]["n"] == 10  # oldest 10 rotated out
    assert hist[-1]["n"] == 59
    assert logging_setup.last_run_summary()["n"] == 59


def test_read_run_history_skips_corrupt_lines() -> None:
    logging_setup.append_run_summary({"n": 1})
    # A partially-written / hand-mangled line must not sink the whole file.
    with open(logging_setup.RUN_HISTORY_FILE, "a", encoding="utf-8") as f:
        f.write("not json at all\n")
    logging_setup.append_run_summary({"n": 2})
    hist = logging_setup.read_run_history()
    assert [h["n"] for h in hist] == [1, 2]


def test_append_run_summary_never_raises(tmp_path, monkeypatch) -> None:
    # Point the file UNDER a regular file so mkdir(parents=True) fails — the
    # write must be swallowed (a diagnostics log must never break the analyze
    # it summarizes).
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setattr(logging_setup, "RUN_HISTORY_FILE", blocker / "sub" / "rh.jsonl")
    logging_setup.append_run_summary({"a": 1})  # no exception
    assert logging_setup.last_run_summary() is None


def test_last_run_summary_none_when_empty() -> None:
    assert logging_setup.last_run_summary() is None


# ---------------------------------------------------------------------------
# rpc analyze handler writes a summary on success (WP9 #20)
# ---------------------------------------------------------------------------


def test_record_run_history_prefers_run_meta_and_records_warnings() -> None:
    from vibechek.rpc import _record_run_history

    report = {
        "summary": {"total_files": 10, "analyzed": 8, "errors": 2},
        "run_meta": {
            "engine": "onnx",
            "genre_classifier": "clap",
            "requested_workers": 8,
            "effective_workers": 4,
            "gpu_workers": 0,
            "cpu_workers": 4,
            "gpu_reason": "low VRAM",
        },
        "runtime_heal_warning": "GPU libraries could not be restored — ran on CPU.",
    }
    _record_run_history(report, AnalysisConfig(inference_engine="onnx"), 12.3)

    last = logging_setup.last_run_summary()
    assert last is not None
    assert last["engine"] == "onnx"
    assert last["effective_workers"] == 4
    assert last["gpu_reason"] == "low VRAM"
    assert last["analyzed"] == 8
    assert last["errors"] == 2
    assert last["duration_sec"] == 12.3
    assert "runtime_heal_warning" in last["warnings"]


def test_record_run_history_falls_back_without_run_meta() -> None:
    """An older WSL analyzer won't stamp run_meta — the summary must still record
    what the RPC handler knows (requested workers + engine) rather than crash."""
    from vibechek.rpc import _record_run_history

    report = {"summary": {"total_files": 3, "analyzed": 3, "errors": 0}}
    _record_run_history(report, AnalysisConfig(workers=6, inference_engine="essentia_tf"), 1.0)

    last = logging_setup.last_run_summary()
    assert last is not None
    assert last["engine"] == "essentia_tf"
    assert last["requested_workers"] == 6
    assert last["effective_workers"] is None  # unknown without run_meta
    assert last["warnings"] == {}


# ---------------------------------------------------------------------------
# incremental rebuild preserves the out-of-band fields (WP9 support)
# ---------------------------------------------------------------------------


def test_reattach_preserves_run_meta_and_heal_notes(tmp_path) -> None:
    from vibechek import library_state as ls
    from vibechek.rpc import _reattach_skipped_records

    lib = tmp_path / "lib"
    lib.mkdir()
    old = lib / "old.mp3"
    old.write_bytes(b"")
    new = lib / "new.mp3"
    new.write_bytes(b"")

    # Prior full analysis knows about old.mp3 (the skipped track).
    ls.record_analysis(str(lib), {
        "status": "complete",
        "summary": {"total_files": 1, "analyzed": 1, "errors": 0},
        "tracks": [{"path": str(old)}],
    })

    # Fresh incremental report: only the new track, carrying the fields that
    # _build_report doesn't know about.
    fresh = {
        "status": "complete",
        "summary": {"total_files": 1, "analyzed": 1, "errors": 0},
        "tracks": [{"path": str(new)}],
        "run_meta": {"engine": "native", "effective_workers": 2},
        "runtime_heal_warning": "ran on CPU",
        "runtime_healed": "repaired",
    }

    merged = _reattach_skipped_records(fresh, {str(old)}, str(lib))

    paths = {t["path"] for t in merged["tracks"]}
    assert paths == {str(old), str(new)}  # rebuilt over the full library
    # ...and the fresh run's out-of-band fields survived the rebuild.
    assert merged["run_meta"] == {"engine": "native", "effective_workers": 2}
    assert merged["runtime_heal_warning"] == "ran on CPU"
    assert merged["runtime_healed"] == "repaired"
