"""Tests for vibechek.cancellation."""

from __future__ import annotations

import threading
import time

import pytest

from vibechek import cancellation


@pytest.fixture(autouse=True)
def _reset_cancellation_state() -> None:
    """Make sure each test starts with a clean slate. The module is a singleton."""
    cancellation.end()
    yield
    cancellation.end()


def test_initial_state_is_clean() -> None:
    assert not cancellation.is_cancelled()
    assert cancellation.current_kind() is None


def test_begin_sets_current_kind() -> None:
    cancellation.begin("analyze")
    assert cancellation.current_kind() == "analyze"
    assert not cancellation.is_cancelled()


def test_cancel_with_no_active_op_returns_none() -> None:
    assert cancellation.cancel() is None
    assert not cancellation.is_cancelled()


def test_cancel_returns_active_kind_and_sets_flag() -> None:
    cancellation.begin("dedupe")
    kind = cancellation.cancel()
    assert kind == "dedupe"
    assert cancellation.is_cancelled()


def test_check_raises_when_cancelled() -> None:
    cancellation.begin("organize")
    cancellation.cancel()
    with pytest.raises(cancellation.CancelledError) as exc_info:
        cancellation.check()
    assert "organize" in str(exc_info.value)


def test_check_does_not_raise_when_not_cancelled() -> None:
    cancellation.begin("analyze")
    # Should not raise
    cancellation.check()


def test_end_clears_flag_and_kind() -> None:
    cancellation.begin("analyze")
    cancellation.cancel()
    assert cancellation.is_cancelled()
    cancellation.end()
    assert not cancellation.is_cancelled()
    assert cancellation.current_kind() is None


def test_begin_clears_prior_cancel_state() -> None:
    cancellation.begin("op-1")
    cancellation.cancel()
    assert cancellation.is_cancelled()

    cancellation.begin("op-2")
    assert not cancellation.is_cancelled()
    assert cancellation.current_kind() == "op-2"


def test_cancel_visible_across_threads() -> None:
    """is_cancelled() must be readable from any thread."""
    cancellation.begin("analyze")
    seen: list[bool] = []
    barrier = threading.Barrier(2)

    def worker() -> None:
        barrier.wait()
        # Wait until the main thread cancels, then sample
        deadline = time.time() + 2.0
        while time.time() < deadline:
            if cancellation.is_cancelled():
                seen.append(True)
                return
            time.sleep(0.005)
        seen.append(False)

    t = threading.Thread(target=worker)
    t.start()
    barrier.wait()
    cancellation.cancel()
    t.join(timeout=3.0)
    assert seen == [True]


def test_many_threads_observe_consistent_kind() -> None:
    """current_kind() under contention should always return the latest set value."""
    seen_kinds: list[str | None] = []
    lock = threading.Lock()

    def reader() -> None:
        for _ in range(50):
            with lock:
                seen_kinds.append(cancellation.current_kind())
            time.sleep(0.0005)

    cancellation.begin("analyze")
    threads = [threading.Thread(target=reader) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5.0)

    # All samples should be "analyze" since we never switched
    assert all(k == "analyze" for k in seen_kinds)
