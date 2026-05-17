"""Tests for vibechek.rpc — JSON-RPC dispatch correctness, error codes,
concurrency.

We drive `serve()` with in-memory stdin/stdout streams (a queue-backed reader
for stdin so we can feed requests after startup, and StringIO for stdout).
"""

from __future__ import annotations

import io
import json
import queue
import threading
import time
from typing import Any

import pytest

from vibechek import rpc


# ---------------------------------------------------------------------------
# A pipe-like stdin we can write to from the test thread
# ---------------------------------------------------------------------------


class _QueueStdin:
    """File-like object that yields lines from an internal queue.

    `serve()` iterates over this with `for line in stdin`. We block on the
    queue until a line is available or a sentinel `None` is pushed to signal
    EOF.
    """

    def __init__(self) -> None:
        self._q: queue.Queue[str | None] = queue.Queue()

    def write_line(self, payload: dict[str, Any] | str) -> None:
        line = payload if isinstance(payload, str) else json.dumps(payload)
        if not line.endswith("\n"):
            line += "\n"
        self._q.put(line)

    def close(self) -> None:
        self._q.put(None)

    def __iter__(self) -> "_QueueStdin":
        return self

    def __next__(self) -> str:
        item = self._q.get()
        if item is None:
            raise StopIteration
        return item


class _SyncedStringIO(io.StringIO):
    """StringIO with a lock-friendly write+flush. The rpc._StdoutWriter
    already takes its own lock, but we read getvalue() from the test thread
    while serve() may still be writing, so we add an extra read-time lock."""

    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.Lock()

    def write(self, s: str) -> int:  # type: ignore[override]
        with self._lock:
            return super().write(s)

    def read_lines(self) -> list[str]:
        with self._lock:
            text = self.getvalue()
        return [l for l in text.splitlines() if l.strip()]


# ---------------------------------------------------------------------------
# Harness fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def rpc_server():
    """Start `rpc.serve()` in a background thread; return (stdin, stdout, stop)."""
    stdin = _QueueStdin()
    stdout = _SyncedStringIO()

    server_thread = threading.Thread(
        target=rpc.serve,
        kwargs={"stdin": stdin, "stdout": stdout},
        daemon=True,
    )
    server_thread.start()

    # Wait for the `ready` notification so we know the writer is bootstrapped.
    deadline = time.time() + 5.0
    ready_seen = False
    while time.time() < deadline:
        for line in stdout.read_lines():
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("method") == "ready":
                ready_seen = True
                break
        if ready_seen:
            break
        time.sleep(0.01)

    assert ready_seen, "Server never emitted ready notification"

    yield stdin, stdout

    stdin.close()
    server_thread.join(timeout=5.0)


def _wait_for_response(stdout: _SyncedStringIO, req_id: Any, timeout: float = 5.0) -> dict:
    """Poll the stdout stream until we see a response matching req_id."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        for line in stdout.read_lines():
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("id") == req_id and ("result" in msg or "error" in msg):
                return msg
        time.sleep(0.01)
    raise TimeoutError(f"No response for id={req_id} within {timeout}s")


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_ping_returns_result(rpc_server) -> None:
    stdin, stdout = rpc_server
    stdin.write_line({"jsonrpc": "2.0", "id": 1, "method": "ping"})
    response = _wait_for_response(stdout, 1)
    assert response["jsonrpc"] == "2.0"
    assert response["result"]["pong"] is True
    assert "version" in response["result"]


def test_version_returns_string(rpc_server) -> None:
    stdin, stdout = rpc_server
    stdin.write_line({"jsonrpc": "2.0", "id": "v1", "method": "version"})
    response = _wait_for_response(stdout, "v1")
    assert isinstance(response["result"]["version"], str)


def test_ready_notification_lists_methods(rpc_server) -> None:
    _stdin, stdout = rpc_server
    # The ready message should have been recorded during fixture setup
    ready = None
    for line in stdout.read_lines():
        msg = json.loads(line)
        if msg.get("method") == "ready":
            ready = msg
            break
    assert ready is not None
    methods = ready["params"]["methods"]
    # A few sanity-check methods that should always be present
    assert "ping" in methods
    assert "preflight" in methods
    assert "cancel_operation" in methods


# ---------------------------------------------------------------------------
# Error codes
# ---------------------------------------------------------------------------


def test_method_not_found_returns_correct_code(rpc_server) -> None:
    stdin, stdout = rpc_server
    stdin.write_line({"jsonrpc": "2.0", "id": 7, "method": "no_such_method"})
    response = _wait_for_response(stdout, 7)
    assert "error" in response
    assert response["error"]["code"] == rpc.METHOD_NOT_FOUND
    assert "no_such_method" in response["error"]["message"]


def test_parse_error_returns_correct_code(rpc_server) -> None:
    stdin, stdout = rpc_server
    stdin.write_line("{this is not json\n")
    # Parse errors have id=None
    deadline = time.time() + 3.0
    parse_err = None
    while time.time() < deadline:
        for line in stdout.read_lines():
            msg = json.loads(line)
            if msg.get("error", {}).get("code") == rpc.PARSE_ERROR:
                parse_err = msg
                break
        if parse_err:
            break
        time.sleep(0.01)
    assert parse_err is not None
    assert parse_err["id"] is None


def test_invalid_request_not_jsonrpc_2(rpc_server) -> None:
    stdin, stdout = rpc_server
    stdin.write_line({"id": 11, "method": "ping"})  # missing jsonrpc
    response = _wait_for_response(stdout, 11)
    assert response["error"]["code"] == rpc.INVALID_REQUEST


def test_missing_method_returns_invalid_request(rpc_server) -> None:
    stdin, stdout = rpc_server
    stdin.write_line({"jsonrpc": "2.0", "id": 12})
    response = _wait_for_response(stdout, 12)
    assert response["error"]["code"] == rpc.INVALID_REQUEST


def test_invalid_params_returns_correct_code(rpc_server) -> None:
    """scan_directory requires a `path` param. Missing → KeyError → INVALID_PARAMS."""
    stdin, stdout = rpc_server
    stdin.write_line({"jsonrpc": "2.0", "id": 21, "method": "scan_directory", "params": {}})
    response = _wait_for_response(stdout, 21)
    assert response["error"]["code"] == rpc.INVALID_PARAMS


# ---------------------------------------------------------------------------
# Concurrency: long-running requests don't block fast ones
# ---------------------------------------------------------------------------


def test_concurrent_requests_interleave(rpc_server, monkeypatch: pytest.MonkeyPatch) -> None:
    """Submit a slow request, immediately submit a fast one. The fast one
    must respond first, proving the thread pool isn't serializing handlers.
    """
    slow_started = threading.Event()
    slow_release = threading.Event()

    def slow_handler(_params: dict) -> dict:
        slow_started.set()
        slow_release.wait(timeout=5.0)
        return {"slow": "done"}

    # Inject a fake slow method directly into METHODS
    monkeypatch.setitem(rpc.METHODS, "_test_slow", slow_handler)
    try:
        stdin, stdout = rpc_server
        stdin.write_line({"jsonrpc": "2.0", "id": "slow", "method": "_test_slow"})

        # Wait until the slow handler is parked in the pool
        assert slow_started.wait(timeout=3.0), "Slow handler never started"

        # Fast call should complete *while* slow is still parked
        stdin.write_line({"jsonrpc": "2.0", "id": "fast", "method": "ping"})
        fast_resp = _wait_for_response(stdout, "fast", timeout=3.0)
        assert fast_resp["result"]["pong"] is True

        # Release the slow handler and confirm it eventually responds too
        slow_release.set()
        slow_resp = _wait_for_response(stdout, "slow", timeout=3.0)
        assert slow_resp["result"]["slow"] == "done"
    finally:
        slow_release.set()
        rpc.METHODS.pop("_test_slow", None)


# ---------------------------------------------------------------------------
# Handler exceptions translate to APP_ERROR
# ---------------------------------------------------------------------------


def test_handler_runtime_error_becomes_app_error(rpc_server, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_params: dict) -> dict:
        raise RuntimeError("kaboom")

    monkeypatch.setitem(rpc.METHODS, "_test_boom", boom)
    try:
        stdin, stdout = rpc_server
        stdin.write_line({"jsonrpc": "2.0", "id": 91, "method": "_test_boom"})
        response = _wait_for_response(stdout, 91)
        assert response["error"]["code"] == rpc.APP_ERROR
        assert "kaboom" in response["error"]["message"]
        # Traceback attached for debugging
        assert "traceback" in response["error"]["data"]
    finally:
        rpc.METHODS.pop("_test_boom", None)


def test_cancellation_error_includes_cancelled_flag(rpc_server, monkeypatch: pytest.MonkeyPatch) -> None:
    from vibechek import cancellation as cm

    def cancelled_handler(_params: dict) -> dict:
        raise cm.CancelledError("user clicked cancel")

    monkeypatch.setitem(rpc.METHODS, "_test_cancelled", cancelled_handler)
    try:
        stdin, stdout = rpc_server
        stdin.write_line({"jsonrpc": "2.0", "id": 92, "method": "_test_cancelled"})
        response = _wait_for_response(stdout, 92)
        assert response["error"]["code"] == rpc.APP_ERROR
        assert response["error"]["data"]["cancelled"] is True
    finally:
        rpc.METHODS.pop("_test_cancelled", None)


# ---------------------------------------------------------------------------
# _json_default handles Path, dataclass, enum
# ---------------------------------------------------------------------------


def test_json_default_path() -> None:
    from pathlib import Path
    p = Path("/tmp/x")
    assert rpc._json_default(p) == str(p)


def test_json_default_dataclass() -> None:
    from dataclasses import dataclass

    @dataclass
    class Foo:
        x: int
        y: str

    assert rpc._json_default(Foo(1, "hi")) == {"x": 1, "y": "hi"}


def test_json_default_enum() -> None:
    from enum import IntEnum

    class Color(IntEnum):
        RED = 1
        BLUE = 2

    assert rpc._json_default(Color.RED) == 1


def test_json_default_unknown_raises() -> None:
    with pytest.raises(TypeError):
        rpc._json_default(object())
