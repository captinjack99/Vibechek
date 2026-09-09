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


# ---------------------------------------------------------------------------
# A clean maxtasks recycle is not a mid-track death
# ---------------------------------------------------------------------------
#
# These drive `_HybridPool`'s supervisor logic directly (no real processes) so
# the race the pool loses in production — the worker's last result still sitting
# in _out_q while its process is already gone — is deterministic here.


class _FakeProc:
    def __init__(self, pid: int, exitcode: int | None, alive: bool = False):
        self.pid = pid
        self.exitcode = exitcode
        self._alive = alive
        self._vibechek_device = "-1"

    def is_alive(self) -> bool:
        return self._alive

    def join(self, timeout=None) -> None:
        pass


class _FakeEvent:
    def __init__(self) -> None:
        self._set = False

    def is_set(self) -> bool:
        return self._set

    def set(self) -> None:
        self._set = True


class _FakeQueue:
    def __init__(self, items=()):
        self.items = list(items)

    def put(self, item) -> None:
        self.items.append(item)

    def get(self, timeout=None):
        import queue as _queue
        if not self.items:
            raise _queue.Empty
        return self.items.pop(0)


def _bare_pool(procs, claims, *, results_out=0, total=10):
    """A `_HybridPool` with its supervisor state set up but no child processes."""
    pool = object.__new__(analyzer._HybridPool)
    pool._done_event = _FakeEvent()
    pool._procs = list(procs)
    pool._claims = dict(claims)
    pool._retries = {}
    pool._delivered = set()
    pool._results_out = results_out
    pool.total = total
    pool._in_q = _FakeQueue()
    pool._out_q = _FakeQueue()
    pool.device_counts = {"0": 0, "-1": 0}
    pool.device_seconds = {"0": 0.0, "-1": 0.0}
    pool._spawn = lambda device: _FakeProc(999, None, alive=True)
    return pool


def test_clean_recycle_does_not_re_enqueue_a_finished_track() -> None:
    """exitcode 0 = the worker posted its maxtasks-th result and returned. The
    claim we still hold just means that result is behind us in _out_q."""
    dead = _FakeProc(pid=4242, exitcode=0)
    pool = _bare_pool([dead], {4242: (7, "/lib/t7.mp3")}, results_out=3)

    pool._reap_and_respawn()

    assert pool._in_q.items == [], "re-analyzed a track that had already finished"
    assert pool._out_q.items == []  # and no synthesized error record
    assert pool._retries == {}
    assert pool._procs[0] is not dead  # the slot is still refilled


def test_abnormal_exit_still_re_enqueues_the_claimed_track() -> None:
    """The crash-recovery this guard exists for must survive the fix: an
    OOM-kill (-9) really does take its in-flight track with it."""
    dead = _FakeProc(pid=4243, exitcode=-9)
    pool = _bare_pool([dead], {4243: (7, "/lib/t7.mp3")}, results_out=3)

    pool._reap_and_respawn()

    assert pool._in_q.items == [(7, "/lib/t7.mp3")]
    assert pool._retries == {7: 1}


def test_repeatedly_killed_track_still_gets_an_error_record() -> None:
    dead = _FakeProc(pid=4244, exitcode=-9)
    pool = _bare_pool([dead], {4244: (7, "/lib/t7.mp3")}, results_out=3)
    pool._retries = {7: 2}

    pool._reap_and_respawn()

    assert pool._in_q.items == []
    (idx, rec, _device, _secs) = pool._out_q.items[0]
    assert idx == 7
    assert "died repeatedly" in rec["error"]


def test_duplicate_result_never_displaces_a_real_track() -> None:
    """A re-enqueued item that wasn't actually lost produces a second result.
    Counting it would fill one of the caller's `total` slots with a duplicate
    and leave a real track out of the report."""
    pool = _bare_pool([], {}, total=2)
    rec0 = {"path": "/lib/t0.mp3"}
    rec1 = {"path": "/lib/t1.mp3"}
    pool._out_q = _FakeQueue([
        (0, rec0, "-1", 1.0),
        (0, rec0, "-1", 1.0),   # the duplicate
        (1, rec1, "-1", 1.0),
    ])

    assert pool.next(timeout=5)[0] == 0
    assert pool.next(timeout=5)[0] == 1
    assert pool._results_out == 2
    assert pool._done_event.is_set()
