"""Tests for the hybrid CPU+GPU work-stealing pool.

These exercise the REAL production machinery — `multiprocessing` spawn, the
shared work queue, work-stealing across GPU/CPU device slots, worker recycling
(maxtasks → process exit → supervisor respawn), per-device throughput tallying,
and clean termination — with essentia/TF swapped out for a fast fake via the
`VIBECHEK_FAKE_ANALYZE=1` env hook (`_hybrid_worker_loop` checks it at runtime).

Why the env hook instead of monkeypatching `load_models`/`analyze_track`:
spawn child processes re-import the module and lose any in-process monkeypatch,
but they DO inherit the parent's environment. The hook lets these tests run on
Windows (spawn-only) AND Linux, so the path that actually ships is covered on
every platform — no skips.
"""

from __future__ import annotations

import multiprocessing

import pytest

from vibechek import analyzer


@pytest.fixture(autouse=True)
def _fake_analyze(monkeypatch):
    # Inherited by spawn children; flips _hybrid_worker_loop to its fake path.
    monkeypatch.setenv("VIBECHEK_FAKE_ANALYZE", "1")


@pytest.fixture
def _spawn_ctx():
    return multiprocessing.get_context("spawn")


def _drain(pool, n):
    out = []
    for _ in range(n):
        out.append(pool.next(timeout=60))
    return out


def test_hybrid_pool_processes_every_track_once(_spawn_ctx):
    files = [f"/tmp/track{i}.mp3" for i in range(40)]
    pool = analyzer._HybridPool(
        _spawn_ctx, files, model_dir="/tmp/models",
        gpu_workers=2, cpu_workers=3, maxtasks=10,
    )
    try:
        seen = {rec["path"] for _idx, rec, _dev, _s in _drain(pool, len(files))}
    finally:
        pool.terminate()
        pool.join()
    assert seen == set(files), "every track must be processed exactly once"


def test_hybrid_pool_uses_both_device_classes(_spawn_ctx):
    files = [f"/tmp/t{i}.mp3" for i in range(80)]
    pool = analyzer._HybridPool(
        _spawn_ctx, files, model_dir="/tmp/models",
        gpu_workers=2, cpu_workers=2, maxtasks=100,
    )
    try:
        _drain(pool, len(files))
    finally:
        pool.terminate()
        pool.join()
    # The shared queue hands work to whichever slot is free, so both the GPU
    # ("0") and CPU ("-1") device classes should have done some of it.
    assert pool.device_counts["0"] > 0, "GPU workers processed nothing"
    assert pool.device_counts["-1"] > 0, "CPU workers processed nothing"
    assert pool.device_counts["0"] + pool.device_counts["-1"] == len(files)


def test_hybrid_pool_recycles_workers_via_maxtasks(_spawn_ctx):
    # maxtasks=5 with 40 files + 2 workers forces several recycles; the
    # supervisor must respawn replacements so all 40 still complete.
    files = [f"/tmp/r{i}.mp3" for i in range(40)]
    pool = analyzer._HybridPool(
        _spawn_ctx, files, model_dir="/tmp/models",
        gpu_workers=0, cpu_workers=2, maxtasks=5,
    )
    try:
        got = _drain(pool, len(files))
    finally:
        pool.terminate()
        pool.join()
    assert len({r["path"] for _i, r, _d, _s in got}) == len(files), \
        "recycling must not drop or duplicate tracks"


def test_throughput_summary_reports_per_device(_spawn_ctx):
    # Deterministic test of the summary FORMATTER: inject known per-device
    # tallies instead of depending on which worker wins the shared-queue race.
    # Whether a GPU vs CPU worker actually grabs work first is timing-dependent
    # (with sub-100ms fake tasks the faster-spawning worker can drain the whole
    # queue before the other finishes importing) — that distribution is already
    # covered by test_hybrid_pool_uses_both_device_classes. This test only needs
    # to prove throughput_summary() renders both device classes.
    files = [f"/tmp/s{i}.mp3" for i in range(4)]
    pool = analyzer._HybridPool(
        _spawn_ctx, files, model_dir="/tmp/models",
        gpu_workers=1, cpu_workers=1, maxtasks=100,
    )
    try:
        pool.device_counts = {"0": 12, "-1": 8}
        pool.device_seconds = {"0": 1.2, "-1": 1.6}
        summary = pool.throughput_summary()
    finally:
        pool.terminate()
        pool.join()
    assert "GPU" in summary and "CPU" in summary
    assert "track" in summary
