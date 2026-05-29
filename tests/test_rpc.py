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


# ---------------------------------------------------------------------------
# install-path probe — warn on risky locations (My Drive, OneDrive,
# very long paths, paths with many spaces).
# ---------------------------------------------------------------------------


def test_probe_install_path_flags_google_drive() -> None:
    result = rpc._probe_install_path(
        r"C:\Users\dj\My Drive\Vibechek\.venv\Scripts\python.exe"
    )
    assert result is not None
    assert result["level"] == "warning"
    # Substring matches are case-insensitive
    assert any("my drive" in r for r in result["reasons"])
    # The full path is in the payload so the frontend can show it
    assert "My Drive" in result["path"]


def test_probe_install_path_flags_onedrive() -> None:
    result = rpc._probe_install_path(
        r"C:\Users\bob\OneDrive\apps\Vibechek\vibechek-sidecar.exe"
    )
    assert result is not None
    assert any("onedrive" in r for r in result["reasons"])


def test_probe_install_path_flags_many_spaces() -> None:
    result = rpc._probe_install_path(
        r"C:\Program Files\My Cool Apps\Vibechek\vibechek-sidecar.exe"
    )
    assert result is not None
    # 3 spaces in "Program Files" + "My Cool Apps" → triggers the > 2 rule
    assert any("spaces" in r for r in result["reasons"])


def test_probe_install_path_flags_long_path() -> None:
    # 201+ chars
    long_path = "C:\\" + "a" * 250 + "\\python.exe"
    result = rpc._probe_install_path(long_path)
    assert result is not None
    assert any("characters long" in r for r in result["reasons"])


def test_probe_install_path_passes_clean_path() -> None:
    # No risky substrings, normal length, ≤ 2 spaces
    assert rpc._probe_install_path(r"C:\Vibechek\python.exe") is None
    assert rpc._probe_install_path("/usr/local/bin/vibechek") is None
    assert rpc._probe_install_path(r"C:\Program Files\Vibechek\v.exe") is None


def test_probe_install_path_handles_empty() -> None:
    # PyInstaller frozen-without-resolvable-executable edge case
    assert rpc._probe_install_path("") is None
    assert rpc._probe_install_path(None) is None


def test_probe_install_path_substring_match_is_case_insensitive() -> None:
    # Real-world: Windows preserves case but our match shouldn't depend on it
    assert (
        rpc._probe_install_path(r"C:\Users\dj\MY DRIVE\app.exe") is not None
    )
    assert (
        rpc._probe_install_path(r"C:\Users\dj\my drive\app.exe") is not None
    )


def test_silence_native_logs_sets_env_vars() -> None:
    """TF_CPP_MIN_LOG_LEVEL must be set before TF imports."""
    import os
    # Use a custom env to avoid trashing the real test process env
    prior = {
        k: os.environ.pop(k, None)
        for k in ("TF_CPP_MIN_LOG_LEVEL", "CUDNN_LOGLEVEL_DBG", "CUDA_MODULE_LOADING")
    }
    try:
        rpc._silence_native_logs()
        assert os.environ.get("TF_CPP_MIN_LOG_LEVEL") == "3"
        assert os.environ.get("CUDNN_LOGLEVEL_DBG") == "0"
        assert os.environ.get("CUDA_MODULE_LOADING") == "LAZY"
    finally:
        # Restore — but only if the caller didn't already have them set,
        # because `setdefault` is what we actually call (preserves user values).
        for k, v in prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_silence_native_logs_preserves_existing_value() -> None:
    """setdefault semantics: if the user set TF_CPP_MIN_LOG_LEVEL=1 we keep it."""
    import os
    prior = os.environ.get("TF_CPP_MIN_LOG_LEVEL")
    os.environ["TF_CPP_MIN_LOG_LEVEL"] = "1"
    try:
        rpc._silence_native_logs()
        assert os.environ["TF_CPP_MIN_LOG_LEVEL"] == "1"
    finally:
        if prior is None:
            os.environ.pop("TF_CPP_MIN_LOG_LEVEL", None)
        else:
            os.environ["TF_CPP_MIN_LOG_LEVEL"] = prior


def test_serve_emits_install_path_warning_on_risky_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """serve() should emit a `notify` event when sys.executable is risky.

    We drive a fresh serve() in a thread with a fake sys.executable pointing
    at "My Drive", and verify the notify frame appears on stdout.
    """
    stdin = _QueueStdin()
    stdout = _SyncedStringIO()

    monkeypatch.setattr(
        "sys.executable",
        r"C:\Users\dj\My Drive\Vibechek\.venv\Scripts\python.exe",
    )

    server_thread = threading.Thread(
        target=rpc.serve,
        kwargs={"stdin": stdin, "stdout": stdout},
        daemon=True,
    )
    server_thread.start()

    deadline = time.time() + 5.0
    notify_seen = None
    while time.time() < deadline:
        for line in stdout.read_lines():
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("method") == "notify":
                notify_seen = msg
                break
        if notify_seen:
            break
        time.sleep(0.01)

    try:
        assert notify_seen is not None, "serve() did not emit notify event"
        assert notify_seen["params"]["level"] == "warning"
        assert "My Drive" in notify_seen["params"]["path"]
    finally:
        stdin.close()
        server_thread.join(timeout=5.0)


def test_serve_skips_install_path_warning_on_clean_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sanity: a clean install path should NOT trigger the notify warning."""
    stdin = _QueueStdin()
    stdout = _SyncedStringIO()

    monkeypatch.setattr("sys.executable", r"C:\Vibechek\python.exe")

    server_thread = threading.Thread(
        target=rpc.serve,
        kwargs={"stdin": stdin, "stdout": stdout},
        daemon=True,
    )
    server_thread.start()

    # Wait for ready then check no notify follows in a short window
    deadline = time.time() + 2.0
    ready_seen = False
    notify_seen = False
    while time.time() < deadline:
        for line in stdout.read_lines():
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("method") == "ready":
                ready_seen = True
            if msg.get("method") == "notify":
                notify_seen = True
        if ready_seen and (time.time() - deadline + 2.0) > 0.5:
            # Ready arrived and we've waited 500ms with no notify
            break
        time.sleep(0.05)

    try:
        assert ready_seen, "serve() never emitted ready"
        assert not notify_seen, "serve() emitted notify for a clean path"
    finally:
        stdin.close()
        server_thread.join(timeout=5.0)


def test_emit_progress_throttles_to_about_20_per_sec(rpc_server) -> None:
    """A burst of progress notifications must not flood stdout.

    Emit 1000 calls back-to-back; only ~5% should actually reach the wire
    (50 ms interval = ~20/sec; 1000 calls in <1ms = at most a handful land).
    """
    _stdin, stdout = rpc_server

    # Reset the throttle clock so we don't inherit state from an earlier test.
    rpc._LAST_PROGRESS_TIME = 0.0

    # Burst-emit. The final tick (current == total) is always allowed
    # through (one extra frame on top of whatever the throttle let in).
    for i in range(1000):
        rpc._emit_progress(i, 1000, f"step {i}")
    rpc._emit_progress(1000, 1000, "final tick")  # always emitted

    # Read everything stdout has. Most lines should be `ready`-or-other
    # protocol frames; we want the progress frames specifically.
    time.sleep(0.05)  # let writer flush
    progress_msgs = []
    for line in stdout.read_lines():
        try:
            m = json.loads(line)
        except ValueError:
            continue
        if m.get("method") == "progress":
            progress_msgs.append(m)

    # 1001 emissions in <1ms wall clock; throttle should drop nearly all.
    # Expect roughly: 1 (first frame) + 1 (final tick) = 2 at minimum.
    # Allow up to 20 to account for `last_emit` racing on slow CI runners.
    assert 2 <= len(progress_msgs) <= 20, (
        f"throttle let through {len(progress_msgs)} of 1001 emissions — "
        f"expected 2-20. Either the throttle is broken or the interval "
        f"({rpc._PROGRESS_MIN_INTERVAL_SEC}s) needs tuning."
    )
    # The final tick MUST be there — it's the GUI's completion signal.
    finals = [m for m in progress_msgs if m["params"]["current"] == 1000]
    assert finals, "final tick (current==total) must always be emitted"


# ---------------------------------------------------------------------------
# Multi-library + count_new_tracks RPCs (DJ-workflow surface)
# ---------------------------------------------------------------------------


@pytest.fixture
def _isolated_library_state(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """Redirect library_state's on-disk paths to tmp_path so the RPC tests
    never touch the user's real config dir."""
    from vibechek import library_state as ls
    monkeypatch.setattr(ls, "STATE_FILE", tmp_path / "library_state.json")
    monkeypatch.setattr(ls, "ANALYSES_DIR", tmp_path / "analyses")
    return tmp_path


def test_rename_library_via_rpc(rpc_server, _isolated_library_state) -> None:
    from vibechek import library_state as ls

    ls.record_open("/lib/main")

    stdin, stdout = rpc_server
    stdin.write_line({
        "jsonrpc": "2.0", "id": 401, "method": "rename_library",
        "params": {"path": "/lib/main", "name": "Friday Set"},
    })
    resp = _wait_for_response(stdout, 401)
    assert resp["result"]["renamed"] is True
    assert resp["result"]["record"]["name"] == "Friday Set"
    # Persisted to disk
    assert ls.load_state().recent[0].name == "Friday Set"


def test_rename_library_unknown_returns_renamed_false(rpc_server, _isolated_library_state) -> None:
    stdin, stdout = rpc_server
    stdin.write_line({
        "jsonrpc": "2.0", "id": 402, "method": "rename_library",
        "params": {"path": "/never/added", "name": "x"},
    })
    resp = _wait_for_response(stdout, 402)
    assert resp["result"] == {"renamed": False}


def test_tag_library_via_rpc(rpc_server, _isolated_library_state) -> None:
    from vibechek import library_state as ls

    ls.record_open("/lib/main")

    stdin, stdout = rpc_server
    stdin.write_line({
        "jsonrpc": "2.0", "id": 403, "method": "tag_library",
        "params": {"path": "/lib/main", "tags": ["Brunch", "brunch", "Wedding"]},
    })
    resp = _wait_for_response(stdout, 403)
    assert resp["result"]["tagged"] is True
    # Case-insensitive dedupe collapsed "Brunch"/"brunch"
    assert resp["result"]["record"]["tags"] == ["Brunch", "Wedding"]


def test_tag_library_rejects_non_list_tags(rpc_server, _isolated_library_state) -> None:
    """tags MUST be a list — passing a string should land as INVALID_PARAMS,
    not silently iterate over characters."""
    from vibechek import library_state as ls

    ls.record_open("/lib/main")

    stdin, stdout = rpc_server
    stdin.write_line({
        "jsonrpc": "2.0", "id": 404, "method": "tag_library",
        "params": {"path": "/lib/main", "tags": "Brunch"},
    })
    resp = _wait_for_response(stdout, 404)
    assert "error" in resp
    assert resp["error"]["code"] == rpc.INVALID_PARAMS


def test_count_new_tracks_no_prior_analysis(rpc_server, _isolated_library_state, tmp_path) -> None:
    """A library that's never been analyzed should report new_count=0 — we
    don't want a huge banner the first time the user opens a folder."""
    lib = tmp_path / "lib_fresh"
    lib.mkdir()
    (lib / "song1.mp3").write_bytes(b"")
    (lib / "song2.mp3").write_bytes(b"")

    stdin, stdout = rpc_server
    stdin.write_line({
        "jsonrpc": "2.0", "id": 410, "method": "count_new_tracks",
        "params": {"library_path": str(lib)},
    })
    resp = _wait_for_response(stdout, 410)
    assert resp["result"]["new_count"] == 0
    assert resp["result"]["total_count"] == 2
    assert resp["result"]["reason"] == "no prior analysis"


def test_count_new_tracks_with_partial_analysis(
    rpc_server, _isolated_library_state, tmp_path,
) -> None:
    """Library has 3 files on disk; saved analysis only knows about 1 →
    new_count should be 2."""
    from vibechek import library_state as ls

    lib = tmp_path / "lib_partial"
    lib.mkdir()
    (lib / "old.mp3").write_bytes(b"")
    (lib / "new1.mp3").write_bytes(b"")
    (lib / "new2.mp3").write_bytes(b"")

    # Pre-existing analysis only contains old.mp3
    ls.record_analysis(str(lib), {
        "summary": {"total_files": 1, "analyzed": 1},
        "tracks": [{"path": str(lib / "old.mp3")}],
    })

    stdin, stdout = rpc_server
    stdin.write_line({
        "jsonrpc": "2.0", "id": 411, "method": "count_new_tracks",
        "params": {"library_path": str(lib)},
    })
    resp = _wait_for_response(stdout, 411)
    assert resp["result"]["new_count"] == 2
    assert resp["result"]["total_count"] == 3
    assert resp["result"]["analyzed_count"] == 1


def test_count_new_tracks_missing_path(rpc_server, _isolated_library_state) -> None:
    """Don't blow up on an unplugged external drive — return zeroes."""
    stdin, stdout = rpc_server
    stdin.write_line({
        "jsonrpc": "2.0", "id": 412, "method": "count_new_tracks",
        "params": {"library_path": "/no/such/folder/ever"},
    })
    resp = _wait_for_response(stdout, 412)
    assert resp["result"]["new_count"] == 0
    assert resp["result"]["total_count"] == 0
    assert "missing" in resp["result"]["reason"]


def test_count_new_tracks_with_full_analysis_says_zero(
    rpc_server, _isolated_library_state, tmp_path,
) -> None:
    """Saved analysis covers every file on disk → no banner."""
    from vibechek import library_state as ls

    lib = tmp_path / "lib_full"
    lib.mkdir()
    paths = []
    for name in ("a.mp3", "b.mp3"):
        p = lib / name
        p.write_bytes(b"")
        paths.append(p)

    ls.record_analysis(str(lib), {
        "summary": {"total_files": 2, "analyzed": 2},
        "tracks": [{"path": str(p)} for p in paths],
    })

    stdin, stdout = rpc_server
    stdin.write_line({
        "jsonrpc": "2.0", "id": 413, "method": "count_new_tracks",
        "params": {"library_path": str(lib)},
    })
    resp = _wait_for_response(stdout, 413)
    assert resp["result"]["new_count"] == 0
    assert resp["result"]["total_count"] == 2
    assert resp["result"]["analyzed_count"] == 2


def test_new_rpc_methods_are_not_cancellable(rpc_server) -> None:
    """rename_library / tag_library / count_new_tracks are instant ops — they
    must NOT acquire the cancellation singleton. If they did, calling them
    while an analyze is running would falsely report 'another op in progress'.
    """
    assert "rename_library" not in rpc._CANCELLABLE_METHODS
    assert "tag_library" not in rpc._CANCELLABLE_METHODS
    assert "count_new_tracks" not in rpc._CANCELLABLE_METHODS


def test_long_op_lock_rejects_concurrent_cancellable_ops(rpc_server) -> None:
    """Two cancellable ops at once must NOT both grab the
    cancellation singleton. The second must get a clean 'busy' error
    instead of clobbering the first's _current_kind.
    """
    stdin, stdout = rpc_server

    # Fire two slow ops back-to-back. Use download_models which is cancellable
    # and runs long enough that the second request lands while the first is
    # still in flight. We monkey-patch the handler to sleep instead of
    # actually downloading.
    import time as _time

    orig_handler = rpc._download_models

    def slow_download(_params: dict) -> dict:
        _time.sleep(2.0)  # block long enough for op 2 to land
        return {"models_dir": "fake", "models": []}

    rpc.METHODS["download_models"] = slow_download
    try:
        stdin.write_line({"jsonrpc": "2.0", "id": 100, "method": "download_models"})
        _time.sleep(0.1)  # let op 1 start
        stdin.write_line({"jsonrpc": "2.0", "id": 101, "method": "download_models"})

        # Op 2 should fail FAST with the busy error, well before op 1 finishes.
        resp = _wait_for_response(stdout, 101, timeout=3.0)
        assert "error" in resp
        assert resp["error"].get("data", {}).get("busy") is True
        assert "already" in resp["error"]["message"].lower()

        # Op 1 should still complete successfully.
        resp1 = _wait_for_response(stdout, 100, timeout=5.0)
        assert "result" in resp1
    finally:
        rpc.METHODS["download_models"] = orig_handler
