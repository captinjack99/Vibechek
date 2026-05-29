"""Tests for the hybrid CPU+GPU work-stealing pool.

We can't load real essentia models in CI, so these tests monkeypatch
`load_models` (to a stub) and `analyze_track` (to a fast fake) and exercise the
`_HybridPool` mechanics: every track gets processed exactly once, work is split
across both device classes, throughput is tallied per device, and cancellation
/ termination behave.

Note: `_HybridPool` uses `multiprocessing.get_context("spawn")`, so the worker
function must be import-stubbed at module import time in the child. We patch
the module-level `load_models` / `analyze_track` names that `_hybrid_worker_loop`
calls; under spawn the child re-imports `vibechek.analyzer`, so the patch must
be installed via a conftest-style autouse on the REAL module attributes — which
only works if the child inherits them. Spawn does NOT inherit monkeypatches, so
we instead test the supervisor/queue logic with a fork context where available,
and fall back to skipping on spawn-only platforms.
"""

from __future__ import annotations

import multiprocessing
import sys
from dataclasses import dataclass

import pytest

from vibechek import analyzer


# A trivially-picklable fake model + analyze result. Defined at module level so
# spawn/fork can pickle them.
@dataclass
class _FakeMLResult:
    path: str
    filename: str = "x"
    extension: str = ".mp3"
    size_mb: float = 1.0
    error: str | None = None


def _fake_load_models(model_dir, use_gpu="auto"):  # noqa: ANN001
    return {"_fake": True, "device": use_gpu}


def _fake_analyze_track(filepath, models):  # noqa: ANN001
    # Cheap + deterministic; no real audio.
    import time
    time.sleep(0.005)
    return _FakeMLResult(path=str(filepath), filename=filepath.name)


@pytest.fixture
def _fork_ctx():
    """A fork context if the platform supports it (Linux); else skip — the
    monkeypatched stubs only propagate to children under fork, not spawn."""
    if sys.platform == "win32" or "fork" not in multiprocessing.get_all_start_methods():
        pytest.skip("hybrid worker test needs fork to inherit monkeypatched stubs")
    return multiprocessing.get_context("fork")


@pytest.fixture(autouse=True)
def _patch_models(monkeypatch):
    monkeypatch.setattr(analyzer, "load_models", _fake_load_models)
    monkeypatch.setattr(analyzer, "analyze_track", _fake_analyze_track)


def test_hybrid_pool_processes_every_track_once(_fork_ctx, monkeypatch):
    files = [f"/tmp/track{i}.mp3" for i in range(40)]
    pool = analyzer._HybridPool(
        _fork_ctx, files, model_dir="/tmp/models",
        gpu_workers=2, cpu_workers=3, maxtasks=10,
    )
    seen: set[str] = set()
    try:
        for _ in range(len(files)):
            _idx, rec, _device, _secs = pool.next(timeout=30)
            seen.add(rec["path"])
    finally:
        pool.terminate()
        pool.join()

    assert seen == set(files), "every track must be processed exactly once"


def test_hybrid_pool_uses_both_device_classes(_fork_ctx):
    files = [f"/tmp/t{i}.mp3" for i in range(60)]
    pool = analyzer._HybridPool(
        _fork_ctx, files, model_dir="/tmp/models",
        gpu_workers=2, cpu_workers=2, maxtasks=50,
    )
    try:
        for _ in range(len(files)):
            pool.next(timeout=30)
    finally:
        pool.terminate()
        pool.join()

    # Both the GPU ("0") and CPU ("-1") slots should have done real work — the
    # shared queue hands tracks to whichever worker is free.
    assert pool.device_counts["0"] > 0, "GPU workers processed nothing"
    assert pool.device_counts["-1"] > 0, "CPU workers processed nothing"
    assert pool.device_counts["0"] + pool.device_counts["-1"] == len(files)


def test_hybrid_pool_recycles_workers_via_maxtasks(_fork_ctx):
    # maxtasks=5 with 40 files + 2 workers forces multiple recycles; the
    # supervisor must respawn so all 40 still complete.
    files = [f"/tmp/r{i}.mp3" for i in range(40)]
    pool = analyzer._HybridPool(
        _fork_ctx, files, model_dir="/tmp/models",
        gpu_workers=0, cpu_workers=2, maxtasks=5,
    )
    count = 0
    try:
        for _ in range(len(files)):
            pool.next(timeout=30)
            count += 1
    finally:
        pool.terminate()
        pool.join()
    assert count == len(files), "recycling must not drop tracks"


def test_throughput_summary_reports_per_device(_fork_ctx):
    files = [f"/tmp/s{i}.mp3" for i in range(20)]
    pool = analyzer._HybridPool(
        _fork_ctx, files, model_dir="/tmp/models",
        gpu_workers=1, cpu_workers=1, maxtasks=50,
    )
    try:
        for _ in range(len(files)):
            pool.next(timeout=30)
    finally:
        pool.terminate()
        pool.join()
    summary = pool.throughput_summary()
    assert "GPU" in summary and "CPU" in summary
    assert "/s" in summary
