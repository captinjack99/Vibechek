"""Tests for the progress-event operation-id contract (vibechek.rpc).

Long-op requests may carry a client-generated `op_id`; the dispatcher strips
it before the handler runs, records it (with the op kind) on the cancellation
singleton, and `_emit_progress` / `_emit_track_analyzed` echo both fields on
every notification emitted while the op runs. This is what lets the GUI
attribute events on the shared notification stream to the exact operation
instance instead of trusting the single-long-op invariant alone.
"""

from __future__ import annotations

import pytest

from vibechek import cancellation, rpc


@pytest.fixture(autouse=True)
def _clean_singleton() -> None:
    """The cancellation module is process-wide state — start and end clean."""
    cancellation.end()
    yield
    cancellation.end()


@pytest.fixture()
def frames(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Capture every wire frame instead of writing to stdout."""
    captured: list[dict] = []
    monkeypatch.setattr(rpc, "_write_message", captured.append)
    return captured


def _register(monkeypatch: pytest.MonkeyPatch, name: str, handler, kind: str | None = None) -> None:
    monkeypatch.setitem(rpc.METHODS, name, handler)
    if kind is not None:
        monkeypatch.setitem(rpc._CANCELLABLE_METHODS, name, kind)


def _progress(frames: list[dict]) -> list[dict]:
    return [f for f in frames if f.get("method") == "progress"]


def test_progress_stamps_kind_and_op_id_for_cancellable_op(
    monkeypatch: pytest.MonkeyPatch, frames: list[dict]
) -> None:
    seen_params: dict = {}

    def fake_op(params: dict) -> dict:
        seen_params.update(params)
        rpc._emit_progress(1, 1, "tick")  # final tick — bypasses the throttle
        return {"ok": True}

    _register(monkeypatch, "fake_op", fake_op, kind="faketest")
    rpc._dispatch({
        "jsonrpc": "2.0", "id": 7, "method": "fake_op",
        "params": {"x": 1, "op_id": "abc-123"},
    })

    # The protocol-level key never reaches the handler.
    assert seen_params == {"x": 1}
    prog = _progress(frames)
    assert prog, "no progress frame reached the wire"
    assert prog[-1]["params"]["op_id"] == "abc-123"
    assert prog[-1]["params"]["kind"] == "faketest"
    # The result still flows, and the singleton is cleared after the op.
    assert any(f.get("id") == 7 and f.get("result") == {"ok": True} for f in frames)
    assert cancellation.current_op() == (None, None)


def test_progress_stamps_kind_only_when_no_op_id_sent(
    monkeypatch: pytest.MonkeyPatch, frames: list[dict]
) -> None:
    def fake_op(_params: dict) -> dict:
        rpc._emit_progress(1, 1, "tick")
        return {}

    _register(monkeypatch, "fake_op", fake_op, kind="faketest")
    rpc._dispatch({"jsonrpc": "2.0", "id": 1, "method": "fake_op", "params": {}})

    prog = _progress(frames)
    assert prog[-1]["params"]["kind"] == "faketest"
    assert "op_id" not in prog[-1]["params"]


def test_progress_unstamped_outside_any_op(frames: list[dict]) -> None:
    """CLI-style direct emission (no cancellable op running) stays legacy-shaped."""
    rpc._emit_progress(5, 5, "idle tick")
    prog = _progress(frames)
    assert prog
    assert "kind" not in prog[-1]["params"]
    assert "op_id" not in prog[-1]["params"]


def test_non_string_op_id_is_coerced_and_capped(
    monkeypatch: pytest.MonkeyPatch, frames: list[dict]
) -> None:
    def fake_op(_params: dict) -> dict:
        rpc._emit_progress(1, 1, "tick")
        return {}

    _register(monkeypatch, "fake_op", fake_op, kind="faketest")

    # A buggy client sending a number must not kill the op — coerce to str.
    rpc._dispatch({"jsonrpc": "2.0", "id": 1, "method": "fake_op",
                   "params": {"op_id": 12345}})
    assert _progress(frames)[-1]["params"]["op_id"] == "12345"

    # A hostile/buggy oversized id is capped — it rides on every frame.
    frames.clear()
    rpc._dispatch({"jsonrpc": "2.0", "id": 2, "method": "fake_op",
                   "params": {"op_id": "x" * 500}})
    assert _progress(frames)[-1]["params"]["op_id"] == "x" * 128


def test_op_id_stripped_for_non_cancellable_methods(
    monkeypatch: pytest.MonkeyPatch, frames: list[dict]
) -> None:
    """op_id is protocol-level: even non-cancellable handlers must not see it,
    and it must NOT claim the process-wide singleton (short ops interleave)."""
    seen: dict = {}

    def fake_short(params: dict) -> dict:
        seen.update(params)
        assert cancellation.current_op() == (None, None)
        return {"ok": True}

    _register(monkeypatch, "fake_short", fake_short)  # NOT cancellable
    rpc._dispatch({"jsonrpc": "2.0", "id": 3, "method": "fake_short",
                   "params": {"a": 1, "op_id": "zzz"}})

    assert seen == {"a": 1}
    assert any(f.get("id") == 3 for f in frames)


def test_track_analyzed_stamps_kind_and_op_id(
    monkeypatch: pytest.MonkeyPatch, frames: list[dict]
) -> None:
    def fake_op(_params: dict) -> dict:
        rpc._emit_track_analyzed({"path": "x.flac"}, 1, 10)
        return {}

    _register(monkeypatch, "fake_op", fake_op, kind="analyze")
    rpc._dispatch({"jsonrpc": "2.0", "id": 4, "method": "fake_op",
                   "params": {"op_id": "run-9"}})

    ta = [f for f in frames if f.get("method") == "track_analyzed"]
    assert ta
    assert ta[-1]["params"]["op_id"] == "run-9"
    assert ta[-1]["params"]["kind"] == "analyze"
    assert ta[-1]["params"]["track"] == {"path": "x.flac"}
