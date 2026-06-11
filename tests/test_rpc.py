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

    def __iter__(self) -> _QueueStdin:
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
        return [ln for ln in text.splitlines() if ln.strip()]


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


def test_missing_path_returns_clean_invalid_params_not_apperror(rpc_server) -> None:
    """A nonexistent / unmounted path is user input, not a server fault.

    find_audio_files raises FileNotFoundError (an OSError, so it bypasses the
    ValueError branch). Before the fix this fell through to the generic handler
    → APP_ERROR (-32000) carrying a full traceback in `data`. The dispatch seam
    now maps FileNotFoundError/NotADirectoryError to a clean INVALID_PARAMS with
    no traceback. Reachable in the GUI when a recent library lives on a removed
    USB / unmounted network share.
    """
    stdin, stdout = rpc_server
    stdin.write_line({
        "jsonrpc": "2.0", "id": "nf1", "method": "scan_directory",
        "params": {"path": "/vibechek_no_such_dir_zzz_4f9a2"},
    })
    response = _wait_for_response(stdout, "nf1")
    assert "error" in response
    assert response["error"]["code"] == rpc.INVALID_PARAMS
    assert "data" not in response["error"], "no scary traceback should be attached"
    assert response["error"]["message"], "message should be non-empty"


def test_serve_forces_utf8_on_stdio() -> None:
    """serve() must pin the JSON-RPC channel to UTF-8, not trust the inherited
    locale. In the packaged console-less desktop app the sidecar otherwise picks
    cp1252 for stdin (even with PYTHONUTF8 set), so non-ASCII track paths the GUI
    sends back ("Tiësto", "Ultra Naté") arrive mojibake'd ("TiÃ«sto") and
    organize/tag report every accented file "not found"."""

    class _RecordingStream:
        def __init__(self) -> None:
            self.reconfigured: dict | None = None
            self._lines = iter(())  # EOF immediately so serve() returns

        def reconfigure(self, **kw):  # type: ignore[no-untyped-def]
            self.reconfigured = kw

        def __iter__(self):  # type: ignore[no-untyped-def]
            return self._lines

        def write(self, _s):  # type: ignore[no-untyped-def]
            pass

        def flush(self):  # type: ignore[no-untyped-def]
            pass

    s_in, s_out = _RecordingStream(), _RecordingStream()
    rpc.serve(stdin=s_in, stdout=s_out)
    assert s_in.reconfigured == {"encoding": "utf-8", "errors": "replace"}
    assert s_out.reconfigured == {"encoding": "utf-8", "errors": "replace"}


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


def test_handle_duplicates_is_cancellable(rpc_server) -> None:
    """handle_duplicates physically moves/trashes files and its loops call
    cancellation.check(). Those checks are inert unless the dispatcher begins a
    cancellation kind for the method — so the Cancel button on a destructive
    bulk move/trash only works if handle_duplicates is registered cancellable
    with a kind distinct from find_duplicates' 'dedupe' (so the busy/lock
    messaging is accurate).
    """
    assert "handle_duplicates" in rpc._CANCELLABLE_METHODS
    assert rpc._CANCELLABLE_METHODS["handle_duplicates"] != rpc._CANCELLABLE_METHODS[
        "find_duplicates"
    ]


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


# ---------------------------------------------------------------------------
# Non-dict params → clean INVALID_PARAMS (not a traceback APP_ERROR)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_params", [["not", "a", "dict"], "hello", 42, 3.14, True])
def test_non_dict_params_returns_invalid_params_not_apperror(rpc_server, bad_params) -> None:
    """A request whose `params` is a non-object (array/string/number) used to
    reach `params.get(...)` → AttributeError, which is NOT in the
    (TypeError, KeyError, ValueError) branch, so it fell through to APP_ERROR
    (-32000) carrying a full traceback (and absolute filesystem paths). The
    dispatch seam now rejects a non-dict params with a clean INVALID_PARAMS and
    no traceback, before any handler or cancellation state is touched.
    """
    stdin, stdout = rpc_server
    stdin.write_line({
        "jsonrpc": "2.0", "id": "npd1", "method": "find_duplicates",
        "params": bad_params,
    })
    resp = _wait_for_response(stdout, "npd1")
    assert "error" in resp
    assert resp["error"]["code"] == rpc.INVALID_PARAMS
    assert "data" not in resp["error"], "no scary traceback should be attached"


def test_non_dict_params_does_not_toggle_cancellation(rpc_server) -> None:
    """The params-shape rejection must run BEFORE the cancellable begin()/end()
    block, so a malformed cancellable request never leaves the cancellation
    singleton in a 'busy' state that would block real ops.
    """
    from vibechek import cancellation

    stdin, stdout = rpc_server
    # analyze_directory is cancellable; send it a non-dict params.
    stdin.write_line({
        "jsonrpc": "2.0", "id": "npd2", "method": "analyze_directory",
        "params": "oops",
    })
    resp = _wait_for_response(stdout, "npd2")
    assert resp["error"]["code"] == rpc.INVALID_PARAMS
    # The singleton must be clear — no op was begun for the rejected request.
    assert cancellation.current_kind() is None


# ---------------------------------------------------------------------------
# Notifications (no id) must never receive a reply, even in early-return branches
# ---------------------------------------------------------------------------


def _collect_frames(stdout, settle: float = 0.4) -> list[dict]:
    deadline = time.time() + settle
    frames: list[dict] = []
    while time.time() < deadline:
        for line in stdout.read_lines():
            try:
                frames.append(json.loads(line))
            except ValueError:
                continue
        time.sleep(0.02)
    return frames


def test_notification_unknown_method_gets_no_reply(rpc_server) -> None:
    """Per JSON-RPC 2.0 the server MUST NOT respond to a notification (no id),
    even for an unknown method. The METHOD_NOT_FOUND early return now guards on
    req_id is not None.
    """
    stdin, stdout = rpc_server
    before = len(stdout.read_lines())
    stdin.write_line({"jsonrpc": "2.0", "method": "this_method_does_not_exist"})
    frames = _collect_frames(stdout)
    # No error frame of any kind should have been emitted for the notification.
    errs = [f for f in frames if "error" in f]
    assert errs == [], f"notification got illegal reply(s): {errs}"
    # And specifically no {"id": null, "error": ...} frame.
    assert not any(f.get("id") is None and "error" in f for f in frames)
    _ = before


def test_notification_missing_method_gets_no_reply(rpc_server) -> None:
    """A notification lacking 'method' must also stay silent (the INVALID_REQUEST
    early return now guards req_id)."""
    stdin, stdout = rpc_server
    stdin.write_line({"jsonrpc": "2.0"})
    frames = _collect_frames(stdout)
    errs = [f for f in frames if "error" in f]
    assert errs == [], f"notification got illegal reply(s): {errs}"


def test_request_unknown_method_still_replies(rpc_server) -> None:
    """Sanity: a real request (with id) to an unknown method still gets the
    METHOD_NOT_FOUND error — the notification guard must not silence real
    requests."""
    stdin, stdout = rpc_server
    stdin.write_line({"jsonrpc": "2.0", "id": "rq1", "method": "nope_method"})
    resp = _wait_for_response(stdout, "rq1")
    assert resp["error"]["code"] == rpc.METHOD_NOT_FOUND


# ---------------------------------------------------------------------------
# _restore_tags forwards the user's tagging config (id3 encoding)
# ---------------------------------------------------------------------------


def test_restore_tags_forwards_config_encoding(monkeypatch: pytest.MonkeyPatch) -> None:
    """The plain restore_tags RPC must forward the user's tagging config (so
    id3_text_encoding is honoured), mirroring _restore_tags_with_remap. Before
    the fix it called restore_tags with no config → fresh UTF-8 default → empty
    text frames for Rekordbox-5/UTF-16 users.
    """
    import vibechek.tagger as tagger_mod
    from vibechek.config import TaggingConfig, VibechekConfig

    captured: dict = {}

    def fake_restore_tags(backup_path, on_progress=None, config=None):
        captured["config"] = config
        from vibechek.tagger import RestoreStats
        return RestoreStats()

    # A config with a non-default ID3 encoding (UTF-16 = 1, Rekordbox 5).
    cfg = VibechekConfig()
    cfg.tagging = TaggingConfig(id3_text_encoding=1)

    monkeypatch.setattr(tagger_mod, "restore_tags", fake_restore_tags)
    monkeypatch.setattr(VibechekConfig, "load", classmethod(lambda cls: cfg))

    rpc._restore_tags({"backup_path": "/tmp/whatever.json"})

    assert captured["config"] is not None, "config must be forwarded, not None"
    assert captured["config"].id3_text_encoding == 1


# ---------------------------------------------------------------------------
# verify_models is engine-aware (ONNX install no longer reported as 16 missing)
# ---------------------------------------------------------------------------


def test_verify_models_onnx_checks_onnx_dir_not_pb(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """For an ONNX-engine install verify_models must check the staged heads in
    <models>/onnx (.onnx + .json), NOT the flat .pb/.json set. Before the fix it
    hardcoded the .pb set, so a healthy ONNX-only install reported every model
    'missing' AND never integrity-checked the .onnx files actually loaded.
    """
    import vibechek.analyzer as analyzer_mod
    from vibechek import config as cfg_mod
    from vibechek.config import VibechekConfig
    from vibechek.onnx_backend import BACKBONE_ONNX_FILENAME

    # Point MODELS_DIR at a tmp tree with ONLY the onnx subdir populated.
    monkeypatch.setattr(cfg_mod, "MODELS_DIR", tmp_path)
    monkeypatch.setattr(rpc, "VibechekConfig", VibechekConfig)

    cfg = VibechekConfig()
    cfg.analysis.inference_engine = "onnx"
    monkeypatch.setattr(VibechekConfig, "load", classmethod(lambda cls: cfg))

    onnx_dir = tmp_path / "onnx"
    onnx_dir.mkdir()
    # Stage one head .onnx with a KNOWN-GOOD pinned hash, plus the backbone.
    stem = "danceability"
    good = analyzer_mod.MODEL_SHA256_ONNX[f"{stem}.onnx"]
    # Find content whose sha256 equals the pin? We can't — instead write a file
    # whose hash we DON'T control and assert the mismatch is reported as ok=False
    # (not 'missing'), proving the ONNX branch ran. Also write the backbone so it
    # shows up as ok=None (no pin) rather than missing.
    (onnx_dir / f"{stem}.onnx").write_bytes(b"not the real weights")
    (onnx_dir / BACKBONE_ONNX_FILENAME).write_bytes(b"fake backbone")

    out = rpc._verify_models({})
    assert out["engine"] == "onnx"
    names = {r["name"] for r in out["results"]}
    # ONNX filenames, NOT the essentia .pb model keys.
    assert f"{stem}.onnx" in names
    assert BACKBONE_ONNX_FILENAME in names
    assert "effnet" not in names  # the .pb model key must NOT appear

    by_name = {r["name"]: r for r in out["results"]}
    # The staged head with a pin but wrong content → ok=False with a digest,
    # NOT reason='missing' (proves it was actually hashed, not absent).
    dance = by_name[f"{stem}.onnx"]
    assert dance["ok"] is False
    assert dance.get("reason") != "missing"
    assert dance["expected"] == good
    # The backbone has no pin → ok=None (informational), not a scary failure.
    assert by_name[BACKBONE_ONNX_FILENAME]["ok"] is None


def test_verify_models_param_engine_overrides_config(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """An explicit `engine` param overrides config.inference_engine."""
    from vibechek import config as cfg_mod
    from vibechek.config import VibechekConfig

    monkeypatch.setattr(cfg_mod, "MODELS_DIR", tmp_path)
    monkeypatch.setattr(rpc, "VibechekConfig", VibechekConfig)
    cfg = VibechekConfig()
    cfg.analysis.inference_engine = "essentia_tf"
    monkeypatch.setattr(VibechekConfig, "load", classmethod(lambda cls: cfg))

    out = rpc._verify_models({"engine": "onnx"})
    assert out["engine"] == "onnx"


# ---------------------------------------------------------------------------
# _setup_onnx_engine surfaces the installer's failure reason (no swallow)
# ---------------------------------------------------------------------------


def test_setup_onnx_engine_forwards_install_failure_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the engine-install step fails (the installer RETURNS {ok:False,
    error:...} rather than raising), setup_onnx_engine must short-circuit and
    forward that specific reason in `error`, instead of running on to the
    preflight and replacing it with the generic 'not installed' string.
    """
    import vibechek.onnx_backend as onnx_mod

    # Stage step is local/fast — stub it to a benign result.
    monkeypatch.setattr(
        onnx_mod, "stage_bundled_onnx_heads",
        lambda models_dir, on_progress=None: {"staged": ["danceability.onnx"], "source": "bundled"},
    )

    # Force the native (non-Windows) branch so we don't depend on WSL, and make
    # the venv probe report "not installed" so the install path runs.
    import vibechek.platform as platform_mod
    monkeypatch.setattr(platform_mod, "IS_WINDOWS", False, raising=False)

    import vibechek.native_install as native_mod

    class _Probe:
        essentia_installed = False
        vibechek_installed = False

    monkeypatch.setattr(native_mod, "probe_native_venv", lambda engine: _Probe())
    monkeypatch.setattr(
        native_mod, "install_essentia_native",
        lambda engine=None, on_progress=None, vibechek_source=None: {
            "ok": False, "error": "No space left on device", "cancelled": False,
        },
    )

    # Preflight must NOT raise; return an unready report.
    import vibechek.preflight as pf_mod

    class _PF:
        ready = False
        reasons_not_ready = ["the ONNX engine is not installed"]
        analyze_via = "native"

    monkeypatch.setattr(pf_mod, "preflight", lambda *a, **k: _PF())

    out = rpc._setup_onnx_engine({})
    assert out["ok"] is False
    assert out["ready"] is False
    assert out["error"] == "No space left on device"
    # download_models step must have been SKIPPED (short-circuit), so the
    # specific reason survives rather than being replaced by preflight's generic
    # string only.
    assert "No space left on device" in str(out["error"])


# ---------------------------------------------------------------------------
# vibechek_source: the CI/dev install-source override on the install RPCs
# ---------------------------------------------------------------------------


def test_install_essentia_native_forwards_vibechek_source(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    """A valid local-directory `vibechek_source` reaches the installer — the
    native-smoke workflow relies on this to install the commit under test
    instead of GitHub main."""
    import vibechek.native_install as native_mod

    seen: dict = {}

    def _fake_install(on_progress=None, engine="essentia_tf", vibechek_source=None):
        seen["engine"] = engine
        seen["vibechek_source"] = vibechek_source
        return {"ok": True}

    monkeypatch.setattr(native_mod, "install_essentia_native", _fake_install)

    out = rpc._install_essentia_native(
        {"engine": "onnx", "vibechek_source": str(tmp_path)}
    )
    assert out == {"ok": True}
    assert seen == {"engine": "onnx", "vibechek_source": str(tmp_path)}


def test_install_essentia_native_defaults_source_to_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No param (the GUI path) → vibechek_source=None → the installer's
    hard-coded GitHub default applies."""
    import vibechek.native_install as native_mod

    seen: dict = {}

    def _fake_install(on_progress=None, engine="essentia_tf", vibechek_source="UNSET"):
        seen["vibechek_source"] = vibechek_source
        return {"ok": True}

    monkeypatch.setattr(native_mod, "install_essentia_native", _fake_install)

    rpc._install_essentia_native({})
    assert seen["vibechek_source"] is None


def test_install_essentia_native_rejects_nonexistent_source(tmp_path) -> None:
    """A vibechek_source that isn't an existing local directory must be
    rejected at the param seam (ValueError → INVALID_PARAMS) — the RPC must
    not become an arbitrary pip-source installer."""
    with pytest.raises(ValueError, match="existing local directory"):
        rpc._install_essentia_native({"vibechek_source": str(tmp_path / "nope")})


def test_install_essentia_native_rejects_url_source() -> None:
    """URLs are not local directories — explicitly rejected."""
    with pytest.raises(ValueError, match="existing local directory"):
        rpc._install_essentia_native(
            {"vibechek_source": "git+https://github.com/evil/package.git"}
        )


def test_setup_onnx_engine_forwards_vibechek_source(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    """The source override flows through setup_onnx_engine's native install
    branch (and a bad value fails fast, before staging)."""
    import vibechek.onnx_backend as onnx_mod

    monkeypatch.setattr(
        onnx_mod, "stage_bundled_onnx_heads",
        lambda models_dir, on_progress=None: {"staged": [], "source": "bundled"},
    )

    import vibechek.platform as platform_mod
    monkeypatch.setattr(platform_mod, "IS_WINDOWS", False, raising=False)

    import vibechek.native_install as native_mod

    class _Probe:
        essentia_installed = False
        vibechek_installed = False

    seen: dict = {}

    def _fake_install(engine=None, on_progress=None, vibechek_source=None):
        seen["vibechek_source"] = vibechek_source
        return {"ok": False, "error": "stub stops here", "cancelled": False}

    monkeypatch.setattr(native_mod, "probe_native_venv", lambda engine: _Probe())
    monkeypatch.setattr(native_mod, "install_essentia_native", _fake_install)

    import vibechek.preflight as pf_mod

    class _PF:
        ready = False
        reasons_not_ready = ["stub"]
        analyze_via = "native"

    monkeypatch.setattr(pf_mod, "preflight", lambda *a, **k: _PF())

    out = rpc._setup_onnx_engine({"vibechek_source": str(tmp_path)})
    assert seen["vibechek_source"] == str(tmp_path)
    assert out["error"] == "stub stops here"


def test_setup_onnx_engine_rejects_bad_source_before_staging(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    """Validation happens BEFORE the staging step touches the models dir."""
    import vibechek.onnx_backend as onnx_mod

    def _must_not_run(*a, **k):  # pragma: no cover - the assertion IS the test
        raise AssertionError("staging ran before param validation")

    monkeypatch.setattr(onnx_mod, "stage_bundled_onnx_heads", _must_not_run)

    with pytest.raises(ValueError, match="existing local directory"):
        rpc._setup_onnx_engine({"vibechek_source": str(tmp_path / "missing")})


# ---------------------------------------------------------------------------
# backup_before_write: the apply RPC must actually snapshot the apply set first.
# ---------------------------------------------------------------------------


def _silent_mp3(path) -> None:
    frame = bytes([0xFF, 0xFB, 0x90, 0xC0]) + bytes(413)
    path.write_bytes(frame * 40)


def test_apply_ml_tags_backup_before_write_creates_backup(tmp_path) -> None:
    """With backup_before_write on (default), _apply_ml_tags snapshots the exact
    files being tagged to a backup BEFORE mutating them, and reports its path.
    (Regression: the toggle was dead — default-on gave a false sense of safety.)"""
    track = tmp_path / "song.mp3"
    _silent_mp3(track)
    params = {
        "analysis": {"tracks": [{"path": str(track), "ml_analysis": {
            "ml_genre": "House", "ml_subgenre": "Deep House",
            "ml_genre_confidence": 0.95, "ml_genre_raw_confidence": 0.95,
        }}]},
        "backup_before_write": True,
    }
    result = rpc._apply_ml_tags(params)
    bp = result.get("backup_path")
    assert bp, "apply must report the pre-apply backup path"
    from pathlib import Path as _P
    assert _P(bp).exists(), "the backup file must actually exist on disk"


def test_apply_ml_tags_no_backup_when_disabled(tmp_path) -> None:
    track = tmp_path / "song.mp3"
    _silent_mp3(track)
    params = {
        "analysis": {"tracks": [{"path": str(track), "ml_analysis": {
            "ml_genre": "House", "ml_subgenre": "Deep House",
            "ml_genre_confidence": 0.95, "ml_genre_raw_confidence": 0.95,
        }}]},
        "backup_before_write": False,
    }
    result = rpc._apply_ml_tags(params)
    assert "backup_path" not in result


def test_organize_cancel_returns_partial_stats_with_journal(monkeypatch, tmp_path) -> None:
    """A cancelled organize must surface its partial stats + journal_path tagged
    cancelled=True (so the GUI can still offer Undo), not become an APP_ERROR
    that strands the half-moved files with no undo path."""
    from vibechek import cancellation
    from vibechek.organizer import OrganizeStats

    partial = OrganizeStats(planned=10, moved=3)
    partial.journal_path = str(tmp_path / "organize.jsonl")

    def fake_org(*_a, **_k):
        e = cancellation.CancelledError("cancelled by user")
        e.partial_stats = partial  # type: ignore[attr-defined]
        raise e

    monkeypatch.setattr("vibechek.organizer.organize_from_analysis", fake_org)
    res = rpc._organize({"analysis": {"tracks": []}, "target_root": str(tmp_path)})
    assert res["cancelled"] is True
    assert res["moved"] == 3
    assert res["journal_path"].endswith("organize.jsonl")
