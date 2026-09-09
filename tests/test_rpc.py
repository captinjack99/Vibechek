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


def test_internal_valueerror_is_internal_error_not_invalid_params(
    rpc_server, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ValueError raised DEEP in a handler (e.g. tagger reading a truncated
    tag-backup JSON) is an INTERNAL fault, not a caller 'bad params' problem —
    it must map to INTERNAL_ERROR, not INVALID_PARAMS which mislabels it."""
    def corrupt(_params: dict) -> dict:
        raise ValueError(
            "Backup file at /x is not valid JSON; it may be truncated or corrupted"
        )

    monkeypatch.setitem(rpc.METHODS, "_test_corrupt", corrupt)
    try:
        stdin, stdout = rpc_server
        stdin.write_line({"jsonrpc": "2.0", "id": 95, "method": "_test_corrupt"})
        response = _wait_for_response(stdout, 95)
        assert response["error"]["code"] == rpc.INTERNAL_ERROR
        assert "truncated or corrupted" in response["error"]["message"]
    finally:
        rpc.METHODS.pop("_test_corrupt", None)


def test_invalid_params_marker_still_maps_to_invalid_params(
    rpc_server, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """rpc's explicit InvalidParams marker (a genuine caller error) is unchanged:
    it still maps to INVALID_PARAMS even though bare ValueError now doesn't."""
    def bad(_params: dict) -> dict:
        raise rpc.InvalidParams("you sent nonsense")

    monkeypatch.setitem(rpc.METHODS, "_test_bad_params", bad)
    try:
        stdin, stdout = rpc_server
        stdin.write_line({"jsonrpc": "2.0", "id": 96, "method": "_test_bad_params"})
        response = _wait_for_response(stdout, 96)
        assert response["error"]["code"] == rpc.INVALID_PARAMS
        assert "you sent nonsense" in response["error"]["message"]
    finally:
        rpc.METHODS.pop("_test_bad_params", None)


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
    # (not 'missing'), proving the ONNX branch ran. Same for the backbone, which
    # IS pinned (in model_download, not MODEL_SHA256_ONNX).
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
    # The backbone IS pinned — wrong content must fail the integrity check, not
    # sail through as an informational "no pin".
    from vibechek.model_download import BACKBONE_ONNX_SHA256
    backbone = by_name[BACKBONE_ONNX_FILENAME]
    assert backbone["ok"] is False
    assert backbone["expected"] == BACKBONE_ONNX_SHA256


def test_verify_models_reports_best_effort_onnx_files_as_optional_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """model_download fetches genre_discogs400.onnx and the non-genre heads'
    class-label .json best-effort — a failed fetch does not fail the download.
    Reporting them as a hard `missing` told a healthy install it was broken;
    the required files must still fail.
    """
    from vibechek import config as cfg_mod
    from vibechek.config import VibechekConfig

    monkeypatch.setattr(cfg_mod, "MODELS_DIR", tmp_path)
    monkeypatch.setattr(rpc, "VibechekConfig", VibechekConfig)
    cfg = VibechekConfig()
    cfg.analysis.inference_engine = "onnx"
    monkeypatch.setattr(VibechekConfig, "load", classmethod(lambda cls: cfg))
    (tmp_path / "onnx").mkdir()   # empty: nothing has been downloaded at all

    out = rpc._verify_models({})
    by_name = {r["name"]: r for r in out["results"]}

    for fname in rpc._optional_onnx_filenames():
        assert by_name[fname]["ok"] is None, fname
        assert by_name[fname]["reason"] == "optional-missing", fname

    # Everything else is still a hard failure.
    assert by_name["mood_happy.onnx"]["ok"] is False
    assert by_name["mood_happy.onnx"]["reason"] == "missing"
    assert by_name["genre_discogs400.json"]["ok"] is False
    assert by_name["genre_discogs400.json"]["reason"] == "missing"


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
# _setup_genre_engine: the non-Windows branch dispatches to the NATIVE setups
# (Linux/macOS used to get a flat "Windows-only" error).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "native_attr"),
    [("clap", "setup_clap_native"), ("resolver", "setup_resolver_native")],
)
def test_setup_genre_engine_dispatches_native_on_non_windows(
    monkeypatch: pytest.MonkeyPatch, kind: str, native_attr: str,
) -> None:
    import vibechek.native_install as native_mod
    import vibechek.platform as platform_mod

    monkeypatch.setattr(platform_mod, "IS_WINDOWS", False, raising=False)

    seen: dict = {}

    def _fake_setup(on_progress=None, engine="essentia_tf"):
        seen["engine"] = engine
        return {"ok": True, "tail": "done"}

    # Neutralize the OTHER kind so a dispatch bug fails loudly.
    def _wrong_kind(on_progress=None, engine="essentia_tf"):  # pragma: no cover
        raise AssertionError(f"dispatched to the wrong native setup for kind={kind!r}")

    monkeypatch.setattr(native_mod, native_attr, _fake_setup)
    other = "setup_resolver_native" if native_attr == "setup_clap_native" else "setup_clap_native"
    monkeypatch.setattr(native_mod, other, _wrong_kind)

    out = rpc._setup_genre_engine({"inference_engine": "onnx"}, kind=kind)
    assert out == {"ok": True, "ready": True, "error": None,
                   "cancelled": False, "tail": "done"}
    # The ACTIVE engine must reach the native setup (it picks venv vs venv-onnx).
    assert seen["engine"] == "onnx"


def test_setup_genre_engine_native_failure_passes_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import vibechek.native_install as native_mod
    import vibechek.platform as platform_mod

    monkeypatch.setattr(platform_mod, "IS_WINDOWS", False, raising=False)
    monkeypatch.setattr(
        native_mod, "setup_clap_native",
        lambda on_progress=None, engine="essentia_tf": {
            "ok": False, "error": "disk full", "cancelled": False, "tail": "x",
        },
    )

    out = rpc._setup_genre_engine({}, kind="clap")
    assert out["ok"] is False
    assert out["ready"] is False
    assert out["error"] == "disk full"


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

def test_revert_journal_cancel_returns_partial_summary(monkeypatch, tmp_path) -> None:
    """A cancelled undo has already moved SOME files back. Dropping the summary
    left the GUI's in-memory library pointing at the ORGANIZED paths for files
    that are now at their originals — silently stale until a re-scan. Mirror
    _organize/_handle_duplicates: return the partial tagged cancelled=True.
    """
    from vibechek import cancellation

    partial = {
        "reverted": 2, "skipped": 0, "errors": 0,
        "reverted_pairs": [("/lib/House/a.mp3", "/lib/a.mp3")],
        "error_messages": [],
    }

    def fake_revert(*_a, **_k):
        e = cancellation.CancelledError("Operation 'revert' cancelled by user")
        e.partial_summary = partial  # type: ignore[attr-defined]
        raise e

    monkeypatch.setattr("vibechek.journal.revert_journal", fake_revert)
    res = rpc._revert_journal({"journal_path": str(tmp_path / "organize.jsonl")})
    assert res["cancelled"] is True
    assert res["reverted"] == 2
    assert res["reverted_pairs"] == [("/lib/House/a.mp3", "/lib/a.mp3")]


def test_revert_journal_cancel_keeps_every_summary_key_and_stays_resumable(
    monkeypatch, tmp_path
) -> None:
    """End-to-end (real journal, real cancel): the cancelled result must be a
    COMPLETE summary — every key the finished shape has, plus cancelled=True —
    and the journal must not be latched as "already reverted", so a second
    Undo finishes the rest.
    """
    import shutil

    from vibechek import cancellation, journal

    monkeypatch.setattr(journal, "JOURNALS_DIR", tmp_path / "journals")
    lib = tmp_path / "lib"
    org = tmp_path / "org" / "House"
    lib.mkdir()
    org.mkdir(parents=True)
    writer = journal.start_journal(journal.KIND_ORGANIZE, root=tmp_path / "org")
    for i in range(4):
        src = lib / f"t{i}.mp3"
        dst = org / f"t{i}.mp3"
        src.write_bytes(b"x")
        shutil.move(str(src), str(dst))
        writer.record_move(src, dst)
    writer.close()

    def cancel_on_the_second_file(current: int, _total: int, _message: str = "") -> None:
        if current == 2:
            cancellation.cancel()

    monkeypatch.setattr(rpc, "_emit_progress", cancel_on_the_second_file)
    cancellation.begin("revert")
    try:
        res = rpc._revert_journal({"journal_path": str(writer.path)})
    finally:
        cancellation.end()

    assert res["cancelled"] is True
    # A partial that is missing keys the GUI reads is a different bug than the
    # one this fix closed; pin the whole shape.
    assert set(res) == {
        "reverted", "skipped", "errors", "trashed_not_reverted",
        "error_messages", "reverted_pairs", "cancelled",
    }
    assert res["reverted"] == 2
    assert len(res["reverted_pairs"]) == 2
    # The pairs describe the REAL filesystem state, newest-first.
    assert res["reverted_pairs"] == [
        (str(org / "t3.mp3"), str(lib / "t3.mp3")),
        (str(org / "t2.mp3"), str(lib / "t2.mp3")),
    ]

    # Nothing marked the journal as spent: it is still listed, and re-running
    # the revert picks up exactly the files the cancel left behind.
    listed = journal.list_journals()
    assert [j["path"] for j in listed] == [str(writer.path)]

    monkeypatch.setattr(rpc, "_emit_progress", lambda *_a, **_k: None)
    res2 = rpc._revert_journal({"journal_path": str(writer.path)})
    assert "cancelled" not in res2
    assert res2["reverted"] == 2      # t1 and t0
    assert res2["skipped"] == 2       # t3/t2 already home — dst is gone
    assert res2["errors"] == 0
    assert sorted(p.name for p in lib.iterdir()) == [
        "t0.mp3", "t1.mp3", "t2.mp3", "t3.mp3",
    ]
    assert list(org.iterdir()) == []


def test_revert_journal_cancel_without_partial_still_raises(monkeypatch, tmp_path) -> None:
    """No partial to salvage → the cancel must still surface, not be swallowed."""
    from vibechek import cancellation

    def fake_revert(*_a, **_k):
        raise cancellation.CancelledError("cancelled by user")

    monkeypatch.setattr("vibechek.journal.revert_journal", fake_revert)
    with pytest.raises(cancellation.CancelledError):
        rpc._revert_journal({"journal_path": str(tmp_path / "organize.jsonl")})


# ---------------------------------------------------------------------------
# organize: what gets VALIDATED is what MOVES the files
# ---------------------------------------------------------------------------


def _one_track_plan_params(lib, **extra) -> dict:
    track = lib / "a.mp3"
    track.write_bytes(b"audio")
    params = {
        "analysis": {"tracks": [{
            "path": str(track),
            "ml_analysis": {
                "ml_genre": "House", "ml_subgenre": "House",
                "ml_genre_confidence": 0.95,
            },
        }]},
        "min_genre_size": 1,
        "use_subgenres": False,
    }
    params.update(extra)
    return params


def test_whitespace_target_root_plans_against_the_library_not_the_cwd(tmp_path) -> None:
    """validate_organize_target reads a whitespace-only target as "blank, use
    the default" — but the config that drives the move took the raw string, and
    Path("   ") is RELATIVE, so the whole genre tree was planned under the
    sidecar's working directory while validation reported ok.
    """
    from pathlib import Path as _P

    lib = tmp_path / "lib"
    lib.mkdir()
    out = rpc._plan_organization(
        _one_track_plan_params(lib, library_path=str(lib), target_root="   "),
    )
    assert out["base_dir"] == str(lib)
    assert out["moves"], "the track should still be planned for a move"
    for m in out["moves"]:
        assert _P(m["destination"]).is_absolute()


def test_blank_library_path_does_not_anchor_the_plan_at_the_cwd(tmp_path) -> None:
    """An empty string is not None, and plan_organization tests
    `library_root is not None` — so a blank library_path meant
    base_dir = Path("") = the sidecar's working directory.
    """
    from pathlib import Path as _P

    lib = tmp_path / "lib"
    lib.mkdir()
    out = rpc._plan_organization(_one_track_plan_params(lib, library_path=""))
    assert out["base_dir"] != "."
    assert _P(out["base_dir"]).is_absolute()


def test_plan_organization_move_carries_relative_destination(tmp_path) -> None:
    """generated.ts declares relative_destination as a REQUIRED field of
    PlannedMove and the destructive confirm modal renders it, but the hand-built
    wire dict dropped it — so it was undefined in production.
    """
    lib = tmp_path / "lib"
    lib.mkdir()
    out = rpc._plan_organization(
        _one_track_plan_params(lib, library_path=str(lib)),
    )
    move = out["moves"][0]
    assert set(move) == {
        "source", "destination", "relative_destination", "original_source",
        "genre", "subgenre", "reason",
    }
    assert move["relative_destination"]
    assert move["relative_destination"] != move["destination"]
    # `original_source` is the caller's OWN spelling of the path — `source` is
    # whichever NFC/NFD form resolve_existing_path found on disk, and a client
    # keying its store off the plan has to match on what it sent.
    assert move["original_source"]


# ---------------------------------------------------------------------------
# scan_directory survives one unreadable file (find_audio_files already does)
# ---------------------------------------------------------------------------


def test_scan_directory_survives_a_file_that_vanished_mid_scan(monkeypatch, tmp_path) -> None:
    """The whole walk finishes before the stat pass runs, so a sync client
    deleting a file in that window raised FileNotFoundError out of the handler —
    which _dispatch reports as INVALID_PARAMS, i.e. the folder the user picked
    is missing — and lost all 12k entries.
    """
    good = tmp_path / "a.mp3"
    good.write_bytes(b"x" * 200_000)   # big enough to round to a non-zero size_mb
    gone = tmp_path / "b.mp3"

    monkeypatch.setattr(
        "vibechek.utils.find_audio_files", lambda *_a, **_k: [good, gone],
    )
    out = rpc._scan_directory({"path": str(tmp_path)})
    assert out["count"] == 2
    by_name = {f["filename"]: f for f in out["files"]}
    assert by_name["a.mp3"]["size_mb"] > 0
    assert "error" not in by_name["a.mp3"]
    # The vanished one is reported, loudly, per-file — like _scan_only does.
    assert by_name["b.mp3"]["size_mb"] == 0.0
    assert by_name["b.mp3"]["error"]


# ---------------------------------------------------------------------------
# verify_models: `native` runs the same ONNX bundle as `onnx`
# ---------------------------------------------------------------------------


def test_verify_models_native_engine_checks_the_onnx_bundle(monkeypatch, tmp_path) -> None:
    """`native` is the WINDOWS DEFAULT and download_models stages it under
    <models>/onnx exactly like `onnx` — so checking the flat .pb set for it
    reported all 16 models missing on a healthy default install.
    """
    from vibechek import config as cfg_mod
    from vibechek.config import VibechekConfig
    from vibechek.onnx_backend import BACKBONE_ONNX_FILENAME

    monkeypatch.setattr(cfg_mod, "MODELS_DIR", tmp_path)
    monkeypatch.setattr(rpc, "VibechekConfig", VibechekConfig)
    cfg = VibechekConfig()
    cfg.analysis.inference_engine = "native"
    monkeypatch.setattr(VibechekConfig, "load", classmethod(lambda cls: cfg))

    onnx_dir = tmp_path / "onnx"
    onnx_dir.mkdir()
    (onnx_dir / BACKBONE_ONNX_FILENAME).write_bytes(b"fake backbone")

    out = rpc._verify_models({})
    assert out["engine"] == "native"
    names = {r["name"] for r in out["results"]}
    assert BACKBONE_ONNX_FILENAME in names
    assert "effnet" not in names  # the essentia .pb model key must NOT appear
    by_name = {r["name"]: r for r in out["results"]}
    # Staged backbone → hashed against its real pin, NOT reported missing.
    assert by_name[BACKBONE_ONNX_FILENAME]["ok"] is False
    assert by_name[BACKBONE_ONNX_FILENAME].get("reason") != "missing"


# ---------------------------------------------------------------------------
# a cancelled / stalled analyze must not throw away the finished tracks
# ---------------------------------------------------------------------------


def test_analyze_cancel_persists_the_partial_report(monkeypatch, tmp_path) -> None:
    """The GUI never sent an output_path, so analyze_directory's every-50-tracks
    checkpoint was dead and every abort path discarded hours of GPU work:
    record_analysis sits AFTER the call and never ran.
    """
    from vibechek import cancellation, library_state

    monkeypatch.setattr(library_state, "STATE_FILE", tmp_path / "library_state.json")
    monkeypatch.setattr(library_state, "ANALYSES_DIR", tmp_path / "analyses")
    lib = tmp_path / "lib"
    lib.mkdir()

    captured: dict = {}

    def fake_analyze(_path, **kw):
        captured["output_path"] = kw.get("output_path")
        e = cancellation.CancelledError("Analysis cancelled by user")
        e.partial_report = {  # type: ignore[attr-defined]
            "status": "complete",   # must be re-stamped: this run did NOT finish
            "tracks": [{"path": str(lib / "a.mp3"), "ml_analysis": {"ml_genre": "House"}}],
            "summary": {"total_files": 2, "analyzed": 1},
        }
        raise e

    monkeypatch.setattr("vibechek.analyzer.analyze_directory", fake_analyze)

    with pytest.raises(cancellation.CancelledError):
        rpc._analyze_directory({"path": str(lib)})

    # A GUI run (no output_path param) now checkpoints to its own file, NOT over
    # the authoritative analysis.
    assert captured["output_path"] == library_state.checkpoint_path_for(str(lib))

    record = next(
        r for r in library_state.load_state().recent if r.path == str(lib)
    )
    saved = library_state.load_analysis(record)
    assert saved is not None
    assert saved["status"] == "in_progress"
    assert [t["path"] for t in saved["tracks"]] == [str(lib / "a.mp3")]


def test_cancelled_full_reanalyze_keeps_the_previously_saved_tracks(
    monkeypatch, tmp_path
) -> None:
    """A cancelled NON-incremental re-analyze must not replace a complete saved
    analysis with the handful of tracks it got through.

    The GUI's main Analyze button sends no skip_paths, so the abort merge used
    to be skipped entirely and the truncated partial was written straight over
    the authoritative file — a 3-track library became a 1-track one, taking the
    user-resolved genre decisions with it.
    """
    from vibechek import cancellation, library_state

    monkeypatch.setattr(library_state, "STATE_FILE", tmp_path / "library_state.json")
    monkeypatch.setattr(library_state, "ANALYSES_DIR", tmp_path / "analyses")
    lib = tmp_path / "lib"
    lib.mkdir()
    files = [lib / "a.mp3", lib / "b.mp3", lib / "c.mp3"]
    for f in files:
        f.write_bytes(b"x")

    library_state.record_analysis(lib, {
        "status": "complete",
        "tracks": [
            {
                "path": str(f),
                "ml_analysis": {"ml_genre": "House", "ml_genre_source": "approved"},
            }
            for f in files
        ],
        "summary": {"total_files": 3, "analyzed": 3},
    })

    def fake_analyze(_path, **_kw):
        e = cancellation.CancelledError("Analysis cancelled by user")
        e.partial_report = {  # type: ignore[attr-defined]
            "status": "complete",
            "tracks": [
                {"path": str(files[0]), "ml_analysis": {"ml_genre": "Techno"}}
            ],
            "summary": {"total_files": 3, "analyzed": 1},
        }
        raise e

    monkeypatch.setattr("vibechek.analyzer.analyze_directory", fake_analyze)

    with pytest.raises(cancellation.CancelledError):
        rpc._analyze_directory({"path": str(lib)})  # NO skip_paths: full re-analyze

    record = next(
        r for r in library_state.load_state().recent if r.path == str(lib)
    )
    saved = library_state.load_analysis(record)
    assert saved is not None
    assert saved["status"] == "in_progress"
    by_path = {t["path"]: t for t in saved["tracks"]}
    assert set(by_path) == {str(f) for f in files}
    # The one track this run reached carries the FRESH result...
    assert by_path[str(files[0])]["ml_analysis"]["ml_genre"] == "Techno"
    # ...and the two it never reached keep their saved records, including the
    # user's resolved genre decision.
    for f in files[1:]:
        assert by_path[str(f)]["ml_analysis"]["ml_genre"] == "House"
        assert by_path[str(f)]["ml_analysis"]["ml_genre_source"] == "approved"


def test_cancelled_reanalyze_keeps_records_for_files_it_cannot_see(
    monkeypatch, tmp_path
) -> None:
    """On the ABORT contract a path we cannot see is UNKNOWN, not deleted.

    The merge used to apply the completed-run rule here and drop every saved
    record whose file was invisible at abort time — but the commonest reason a
    run aborts is the library's volume going away (drive unplugged, NAS share
    dropped), which makes ALL of them invisible at once. The merge then
    re-attached nothing and the truncated partial was written over the complete
    saved analysis. A run that did not finish never gets to delete results; the
    next completed run applies the deleted-means-gone rule."""
    from vibechek import cancellation, library_state

    monkeypatch.setattr(library_state, "STATE_FILE", tmp_path / "library_state.json")
    monkeypatch.setattr(library_state, "ANALYSES_DIR", tmp_path / "analyses")
    lib = tmp_path / "lib"
    lib.mkdir()
    kept = lib / "kept.mp3"
    kept.write_bytes(b"x")
    gone = lib / "gone.mp3"  # never created

    library_state.record_analysis(lib, {
        "status": "complete",
        "tracks": [
            {"path": str(kept), "ml_analysis": {"ml_genre": "House"}},
            {"path": str(gone), "ml_analysis": {"ml_genre": "House"}},
        ],
        "summary": {"total_files": 2, "analyzed": 2},
    })

    def fake_analyze(_path, **_kw):
        e = cancellation.CancelledError("Analysis cancelled by user")
        e.partial_report = {  # type: ignore[attr-defined]
            "status": "complete", "tracks": [], "summary": {},
        }
        raise e

    monkeypatch.setattr("vibechek.analyzer.analyze_directory", fake_analyze)
    with pytest.raises(cancellation.CancelledError):
        rpc._analyze_directory({"path": str(lib)})

    record = next(
        r for r in library_state.load_state().recent if r.path == str(lib)
    )
    saved = library_state.load_analysis(record)
    assert {t["path"] for t in saved["tracks"]} == {str(kept), str(gone)}


def test_cancelled_reanalyze_survives_the_library_volume_disappearing(
    monkeypatch, tmp_path
) -> None:
    """The finding's own scenario: a complete 5-track analysis, then a run that
    aborts BECAUSE the library went away. Every saved path is invisible, so the
    existence filter re-attached nothing and record_analysis wrote the 1-track
    partial over the complete file — the abort destroyed four finished tracks."""
    from vibechek import cancellation, library_state

    monkeypatch.setattr(library_state, "STATE_FILE", tmp_path / "library_state.json")
    monkeypatch.setattr(library_state, "ANALYSES_DIR", tmp_path / "analyses")
    lib = tmp_path / "lib"
    lib.mkdir()
    files = [lib / f"t{i}.mp3" for i in range(5)]
    for f in files:
        f.write_bytes(b"x")

    library_state.record_analysis(lib, {
        "status": "complete",
        "tracks": [{"path": str(f), "ml_analysis": {"ml_genre": "House"}} for f in files],
        "summary": {"total_files": 5, "analyzed": 5},
    })

    def fake_analyze(_path, **_kw):
        # The volume goes away mid-run: that IS what aborted it.
        for f in files:
            f.unlink()
        e = cancellation.CancelledError("Analysis stalled and was stopped")
        e.partial_report = {  # type: ignore[attr-defined]
            "status": "complete",
            "tracks": [{"path": str(files[0]), "ml_analysis": {"ml_genre": "Techno"}}],
            "summary": {"total_files": 5, "analyzed": 1},
        }
        raise e

    monkeypatch.setattr("vibechek.analyzer.analyze_directory", fake_analyze)
    with pytest.raises(cancellation.CancelledError):
        rpc._analyze_directory({"path": str(lib)})

    record = next(
        r for r in library_state.load_state().recent if r.path == str(lib)
    )
    saved = library_state.load_analysis(record)
    by_path = {t["path"]: t for t in saved["tracks"]}
    assert set(by_path) == {str(f) for f in files}
    assert by_path[str(files[0])]["ml_analysis"]["ml_genre"] == "Techno"


def test_incremental_analyze_still_drops_records_for_files_gone_from_disk(
    monkeypatch, tmp_path
) -> None:
    """The completed-run contract is unchanged: when the run FINISHED, a saved
    record whose file is gone really is a deleted track and must not be
    resurrected. Only the abort path treats invisible as unknown."""
    from vibechek import library_state

    monkeypatch.setattr(library_state, "STATE_FILE", tmp_path / "library_state.json")
    monkeypatch.setattr(library_state, "ANALYSES_DIR", tmp_path / "analyses")
    lib = tmp_path / "lib"
    lib.mkdir()
    kept = lib / "kept.mp3"
    kept.write_bytes(b"x")
    gone = lib / "gone.mp3"  # never created

    library_state.record_analysis(lib, {
        "status": "complete",
        "tracks": [
            {"path": str(kept), "ml_analysis": {"ml_genre": "House"}},
            {"path": str(gone), "ml_analysis": {"ml_genre": "House"}},
        ],
        "summary": {"total_files": 2, "analyzed": 2},
    })

    fresh = {"status": "complete", "tracks": [], "summary": {}}
    merged = rpc._reattach_skipped_records(fresh, {str(kept), str(gone)}, str(lib))

    assert [t["path"] for t in merged["tracks"]] == [str(kept)]


def test_analyze_cancel_without_partial_persists_nothing(monkeypatch, tmp_path) -> None:
    """No partial attached → nothing is invented and the cancel still surfaces."""
    from vibechek import cancellation, library_state

    monkeypatch.setattr(library_state, "STATE_FILE", tmp_path / "library_state.json")
    monkeypatch.setattr(library_state, "ANALYSES_DIR", tmp_path / "analyses")
    lib = tmp_path / "lib"
    lib.mkdir()

    def fake_analyze(_path, **_kw):
        raise cancellation.CancelledError("Analysis cancelled by user")

    monkeypatch.setattr("vibechek.analyzer.analyze_directory", fake_analyze)
    with pytest.raises(cancellation.CancelledError):
        rpc._analyze_directory({"path": str(lib)})
    assert library_state.load_state().recent == []


def test_analyze_auto_save_false_gets_no_checkpoint(monkeypatch, tmp_path) -> None:
    """auto_save=False callers (CLI one-offs with their own --output) asked us
    not to touch library state; don't quietly start writing into it."""
    from vibechek import library_state

    monkeypatch.setattr(library_state, "STATE_FILE", tmp_path / "library_state.json")
    monkeypatch.setattr(library_state, "ANALYSES_DIR", tmp_path / "analyses")
    lib = tmp_path / "lib"
    lib.mkdir()

    captured: dict = {}

    def fake_analyze(_path, **kw):
        captured["output_path"] = kw.get("output_path")
        return {"status": "complete", "tracks": [], "summary": {}}

    monkeypatch.setattr("vibechek.analyzer.analyze_directory", fake_analyze)
    rpc._analyze_directory({"path": str(lib), "auto_save": False})
    assert captured["output_path"] is None


# ---------------------------------------------------------------------------
# organize: "no common ancestor" is a caller problem, not a server crash
# ---------------------------------------------------------------------------


def _tracks_with_no_common_ancestor() -> list[dict]:
    """Two analyzed tracks `os.path.commonpath` cannot reconcile.

    Windows fails on mixed DRIVES; POSIX has a single root, so the only way to
    fail there is mixing an absolute path with a relative one (which a
    hand-edited or relocated analysis.json really can carry).
    """
    import os as _os

    pair = (
        (r"C:\Music\a.mp3", r"D:\Music\b.mp3") if _os.name == "nt"
        else ("/music/a.mp3", "music/b.mp3")
    )
    return [
        {"path": p, "ml_analysis": {"ml_genre": "House", "ml_subgenre": "House",
                                    "ml_genre_confidence": 0.9}}
        for p in pair
    ]


def test_plan_organization_without_a_common_root_is_invalid_params() -> None:
    """organizer refuses to guess a library root it can't infer and raises a
    finished, user-facing sentence. A plain ValueError out of a handler is
    INTERNAL_ERROR + a traceback at the dispatch seam, so the user saw
    "Vibechek crashed" for what is really "pick a destination folder".
    """
    with pytest.raises(rpc.InvalidParams) as excinfo:
        rpc._plan_organization({
            "analysis": {"tracks": _tracks_with_no_common_ancestor()},
            "min_genre_size": 1,
        })
    assert "different drives or roots" in str(excinfo.value)


def test_organize_without_a_common_root_is_invalid_params() -> None:
    """Same seam on the EXECUTE path — it must fail before moving anything."""
    with pytest.raises(rpc.InvalidParams) as excinfo:
        rpc._organize({
            "analysis": {"tracks": _tracks_with_no_common_ancestor()},
            "min_genre_size": 1,
        })
    assert "different drives or roots" in str(excinfo.value)


def test_organize_empty_analysis_is_also_invalid_params() -> None:
    """Sibling of the same ValueError seam: no tracks AND no target is a caller
    error too, not an internal fault.
    """
    with pytest.raises(rpc.InvalidParams):
        rpc._organize({"analysis": {"tracks": []}})


# ---------------------------------------------------------------------------
# config: an UNREADABLE config.json must never be silently overwritten
# ---------------------------------------------------------------------------


def _corrupt_the_config_file():
    """Leave unparseable bytes where VibechekConfig.load() looks; return the path.

    conftest's autouse fixture already points CONFIG_FILE at a tmp dir.
    """
    from vibechek import config as cfg_mod

    cfg_mod.CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    cfg_mod.CONFIG_FILE.write_text("{ this is not json", encoding="utf-8")
    return cfg_mod.CONFIG_FILE


def test_get_config_flags_a_config_file_it_could_not_read() -> None:
    """load() answers an unreadable file with pristine DEFAULTS, so without this
    flag the GUI adopts factory settings as the user's baseline and its
    debounced autosave writes them straight over the real ones.
    """
    _corrupt_the_config_file()
    payload = rpc._get_config({})
    assert payload["load_failed"] is True
    assert payload["config_warnings"], "the reason must travel with the flag"


def test_get_config_omits_load_failed_on_a_healthy_config() -> None:
    from vibechek.config import VibechekConfig

    VibechekConfig().save()
    payload = rpc._get_config({})
    assert "load_failed" not in payload


def test_save_config_refuses_to_overwrite_an_unreadable_file() -> None:
    """The config being saved is built from the CLIENT's dict, so it carries
    none of the markers VibechekConfig.save() checks — that guard is
    structurally blind to this path and the autosave would flatten the user's
    settings.
    """
    path = _corrupt_the_config_file()
    before = path.read_text(encoding="utf-8")

    with pytest.raises(rpc.InvalidParams) as excinfo:
        rpc._save_config({"config": {"analysis": {"workers": 3}}})

    message = str(excinfo.value)
    assert "could not be read" in message
    assert str(path) in message, "the user needs to know WHICH file to fix"
    assert path.read_text(encoding="utf-8") == before, "must not have written"


def test_save_config_force_writes_and_quarantines_the_original() -> None:
    """`force` is the deliberate "take the defaults" path — but the unreadable
    bytes must survive as `<name>.corrupt-<ts>` for a hand-repair.
    """
    path = _corrupt_the_config_file()
    out = rpc._save_config({"config": {"analysis": {"workers": 3}}, "force": True})

    assert out["saved_to"] == str(path)
    assert json.loads(path.read_text(encoding="utf-8"))["analysis"]["workers"] == 3
    quarantined = list(path.parent.glob(f"{path.name}.corrupt-*"))
    assert quarantined, "the original bytes must be kept aside, not destroyed"
    assert quarantined[0].read_text(encoding="utf-8") == "{ this is not json"


def test_save_config_still_writes_when_the_file_reads_fine() -> None:
    from pathlib import Path as _Path

    out = rpc._save_config({"config": {"analysis": {"workers": 2}}})
    saved = json.loads(_Path(out["saved_to"]).read_text(encoding="utf-8"))
    assert saved["analysis"]["workers"] == 2


def test_restore_defaults_quarantines_instead_of_destroying() -> None:
    """Restore Defaults IS the "overwrite it anyway" button, so it never
    refuses — but a bare VibechekConfig().save() carried no marker, so the
    quarantine branch never ran and the user's only copy was destroyed by the
    button that promises to hand it back.
    """
    path = _corrupt_the_config_file()
    out = rpc._restore_default_config({})

    assert out["saved_to"] == str(path)
    quarantined = list(path.parent.glob(f"{path.name}.corrupt-*"))
    assert quarantined
    assert quarantined[0].read_text(encoding="utf-8") == "{ this is not json"


# ---------------------------------------------------------------------------
# handle_duplicates: the saved analysis must follow the files
# ---------------------------------------------------------------------------


_EMPTY_DUPE_REPORT = {"summary": {}, "exact_duplicates": [], "audio_duplicates": []}


def _seed_analysis(lib, paths: list):
    """Record a saved analysis for `lib` holding one row per path."""
    from vibechek import library_state

    report = {
        "tracks": [
            {"path": str(p), "filename": p.name, "extension": ".mp3",
             "size_mb": 1.0, "ml_analysis": {"ml_genre": "House"}}
            for p in paths
        ],
        "summary": {"total_files": len(paths), "analyzed": len(paths)},
    }
    return library_state.record_analysis(str(lib), report)


def _saved_paths(record) -> list[str]:
    from vibechek import library_state

    return [t["path"] for t in library_state.load_analysis(record)["tracks"]]


def _stub_handle_duplicates(monkeypatch, summary: dict) -> None:
    monkeypatch.setattr(
        "vibechek.duplicates.handle_duplicates",
        lambda *_a, **_k: summary,
    )


def test_handle_duplicates_passes_the_per_file_lists_through(monkeypatch, tmp_path) -> None:
    """deleted_paths / moved_pairs / journal_incomplete are the contract the GUI
    reads to show what actually happened (and whether the undo journal is
    complete). The RPC must not reshape or drop them.
    """
    summary = {
        "moved": 1, "deleted": 1, "errors": 0, "error_messages": [],
        "deleted_paths": [str(tmp_path / "lib" / "gone.mp3")],
        "moved_pairs": [
            [str(tmp_path / "lib" / "m.mp3"), str(tmp_path / "review" / "m.mp3")],
        ],
        "journal_incomplete": True,
    }
    _stub_handle_duplicates(monkeypatch, summary)

    out = rpc._handle_duplicates({"report": _EMPTY_DUPE_REPORT, "action": "trash"})
    assert out["deleted_paths"] == summary["deleted_paths"]
    assert out["moved_pairs"] == summary["moved_pairs"]
    assert out["journal_incomplete"] is True


def test_handle_duplicates_drops_trashed_tracks_from_the_saved_analysis(
    monkeypatch, tmp_path,
) -> None:
    """A trashed duplicate left in the saved analysis comes back as a ghost row
    on the next launch, pointing at a file that no longer exists.
    """
    from vibechek import library_state

    lib = tmp_path / "lib"
    lib.mkdir()
    keep, gone = lib / "keep.mp3", lib / "gone.mp3"
    record = _seed_analysis(lib, [keep, gone])

    _stub_handle_duplicates(monkeypatch, {
        "moved": 0, "deleted": 1, "errors": 0, "error_messages": [],
        "deleted_paths": [str(gone)], "journal_incomplete": False,
    })
    rpc._handle_duplicates({
        "report": _EMPTY_DUPE_REPORT, "action": "trash", "library_path": str(lib),
    })

    assert _saved_paths(record) == [str(keep)]
    summary = library_state.load_analysis(record)["summary"]
    assert summary["total_files"] == 1, "the header count must match the rows"
    assert summary["analyzed"] == 1


def test_handle_duplicates_repaths_a_move_that_stayed_inside_the_library(
    monkeypatch, tmp_path,
) -> None:
    """A review folder INSIDE the library keeps the file in the library, so the
    row must FOLLOW it — a stale pre-move path misses in tagging, organize and
    the conflict queue alike. The basename can change too (`_unique_path`
    renames a collision).
    """
    from vibechek import library_state

    lib = tmp_path / "lib"
    lib.mkdir()
    keep, moved = lib / "keep.mp3", lib / "dupe.mp3"
    record = _seed_analysis(lib, [keep, moved])
    dst = lib / "_review" / "dupe (1).mp3"

    _stub_handle_duplicates(monkeypatch, {
        "moved": 1, "deleted": 0, "errors": 0, "error_messages": [],
        "moved_pairs": [[str(moved), str(dst)]], "journal_incomplete": False,
    })
    rpc._handle_duplicates({
        "report": _EMPTY_DUPE_REPORT, "action": "move",
        "review_folder": str(lib / "_review"), "library_path": str(lib),
    })

    saved = library_state.load_analysis(record)
    by_path = {t["path"]: t for t in saved["tracks"]}
    assert set(by_path) == {str(keep), str(dst)}
    assert by_path[str(dst)]["filename"] == "dupe (1).mp3"
    # A re-path is not a removal — the count must NOT drop.
    assert saved["summary"]["total_files"] == 2


def test_handle_duplicates_drops_rows_a_move_took_out_of_the_library(
    monkeypatch, tmp_path,
) -> None:
    """The review folder is normally OUTSIDE the library (`D:/Dupes`), and a
    row re-pointed there is a track the analysis still claims is in the library.

    `plan_organization` applies no containment filter: it plans EVERY row it
    finds into `<base>/<Genre>/`. So the next Organize picked up all 300
    quarantined duplicates at `D:/Dupes/*`, moved them back under
    `D:/Music/House/...` — where they no longer even collide with their keepers,
    so a re-scan wouldn't flag them again — and the user's whole dedupe pass was
    silently undone. A file that left the library leaves the analysis.
    """
    from vibechek import library_state

    lib = tmp_path / "lib"
    lib.mkdir()
    keep, moved = lib / "keep.mp3", lib / "dupe.mp3"
    record = _seed_analysis(lib, [keep, moved])
    dst = tmp_path / "review" / "dupe.mp3"

    _stub_handle_duplicates(monkeypatch, {
        "moved": 1, "deleted": 0, "errors": 0, "error_messages": [],
        "moved_pairs": [[str(moved), str(dst)]], "journal_incomplete": False,
    })
    rpc._handle_duplicates({
        "report": _EMPTY_DUPE_REPORT, "action": "move",
        "review_folder": str(tmp_path / "review"), "library_path": str(lib),
    })

    saved = library_state.load_analysis(record)
    assert [t["path"] for t in saved["tracks"]] == [str(keep)]
    assert saved["summary"]["total_files"] == 1


def test_split_moves_by_root_keys_containment_off_the_library_root(tmp_path) -> None:
    """The unit behind both cases, including the no-root fallback and (on
    Windows) the case/separator spellings the rest of the sync path normalizes.
    """
    import os

    lib = tmp_path / "lib"
    inside, outside = rpc._split_moves_by_root(
        {"a": str(lib / "House" / "a.mp3"), "b": str(tmp_path / "Dupes" / "b.mp3")},
        str(lib),
    )
    assert set(inside) == {"a"}
    assert outside == {"b"}

    # A sibling directory that merely SHARES a prefix is outside.
    inside2, outside2 = rpc._split_moves_by_root(
        {"c": str(tmp_path / "lib2" / "c.mp3")}, str(lib),
    )
    assert inside2 == {} and outside2 == {"c"}

    if os.name == "nt":
        # The same folder reaches us spelled several ways; a bare `==` would
        # match nothing in the common case.
        inside3, outside3 = rpc._split_moves_by_root(
            {"a": str(lib / "House" / "a.mp3")},
            str(lib).upper().replace("\\", "/"),
        )
        assert set(inside3) == {"a"} and outside3 == set()

    # No root to compare against → everything is treated as inside, which is
    # what this path did before the split existed. (`normpath("")` is ".", so
    # the blank check has to happen before normalization.)
    assert rpc._split_moves_by_root({"d": "/x/d.mp3"}, "") == ({"d": "/x/d.mp3"}, set())
    assert rpc._split_moves_by_root({"d": "/x/d.mp3"}, None) == ({"d": "/x/d.mp3"}, set())


def test_handle_duplicates_finds_the_library_without_an_explicit_path(
    monkeypatch, tmp_path,
) -> None:
    """The GUI now sends library_path, but a client that doesn't (an older
    build, the CLI) must still be served: a file we just trashed can only be a
    ghost in a library that CONTAINS it, so match by ancestry.
    """
    lib = tmp_path / "lib"
    lib.mkdir()
    keep, gone = lib / "keep.mp3", lib / "gone.mp3"
    record = _seed_analysis(lib, [keep, gone])

    _stub_handle_duplicates(monkeypatch, {
        "moved": 0, "deleted": 1, "errors": 0, "error_messages": [],
        "deleted_paths": [str(gone)],
    })
    rpc._handle_duplicates({"report": _EMPTY_DUPE_REPORT, "action": "trash"})

    assert _saved_paths(record) == [str(keep)]


def test_handle_duplicates_leaves_other_libraries_alone(monkeypatch, tmp_path) -> None:
    """Ancestry matching must not reach into a library the dedupe never touched."""
    lib, other = tmp_path / "lib", tmp_path / "other"
    lib.mkdir()
    other.mkdir()
    gone = lib / "gone.mp3"
    _seed_analysis(lib, [gone])
    untouched = _seed_analysis(other, [other / "gone.mp3"])

    _stub_handle_duplicates(monkeypatch, {
        "moved": 0, "deleted": 1, "errors": 0, "error_messages": [],
        "deleted_paths": [str(gone)],
    })
    rpc._handle_duplicates({"report": _EMPTY_DUPE_REPORT, "action": "trash"})

    assert _saved_paths(untouched) == [str(other / "gone.mp3")]


def test_handle_duplicates_survives_a_summary_without_the_new_keys(
    monkeypatch, tmp_path,
) -> None:
    """An older duplicates.py reports counts only. That must be a no-op, not an
    AttributeError that reports a completed trash as a failure.
    """
    lib = tmp_path / "lib"
    lib.mkdir()
    record = _seed_analysis(lib, [lib / "a.mp3"])

    _stub_handle_duplicates(monkeypatch, {
        "moved": 0, "deleted": 1, "errors": 0, "error_messages": [],
    })
    out = rpc._handle_duplicates({
        "report": _EMPTY_DUPE_REPORT, "action": "trash", "library_path": str(lib),
    })

    assert out["deleted"] == 1
    assert _saved_paths(record) == [str(lib / "a.mp3")]


def test_handle_duplicates_prunes_after_a_cancel_too(monkeypatch, tmp_path) -> None:
    """A cancelled run still trashed real files, so the saved analysis is
    exactly as stale as after a completed one.
    """
    from vibechek import cancellation

    lib = tmp_path / "lib"
    lib.mkdir()
    keep, gone = lib / "keep.mp3", lib / "gone.mp3"
    record = _seed_analysis(lib, [keep, gone])

    def fake_handle(*_a, **_k):
        e = cancellation.CancelledError("cancelled")
        e.partial_summary = {
            "moved": 0, "deleted": 1, "errors": 0, "error_messages": [],
            "deleted_paths": [str(gone)], "journal_path": None,
        }
        raise e

    monkeypatch.setattr("vibechek.duplicates.handle_duplicates", fake_handle)
    out = rpc._handle_duplicates({
        "report": _EMPTY_DUPE_REPORT, "action": "trash", "library_path": str(lib),
    })

    assert out["cancelled"] is True
    assert _saved_paths(record) == [str(keep)]


def test_handle_duplicates_still_returns_when_the_analysis_is_unreadable(
    monkeypatch, tmp_path,
) -> None:
    """The destructive step has ALREADY succeeded by the time we prune. Turning
    best-effort housekeeping into a failed handle_duplicates would tell the user
    nothing happened when their files really moved.
    """
    from pathlib import Path as _Path

    from vibechek import library_state

    lib = tmp_path / "lib"
    lib.mkdir()
    gone = lib / "gone.mp3"
    record = _seed_analysis(lib, [gone])
    _Path(record.analysis_path).write_text("{ truncated", encoding="utf-8")

    _stub_handle_duplicates(monkeypatch, {
        "moved": 0, "deleted": 1, "errors": 0, "error_messages": [],
        "deleted_paths": [str(gone)],
    })
    out = rpc._handle_duplicates({
        "report": _EMPTY_DUPE_REPORT, "action": "trash", "library_path": str(lib),
    })

    assert out["deleted"] == 1
    # Untouched — a corrupt file must not be half-rewritten by the prune.
    assert _Path(record.analysis_path).read_text(encoding="utf-8") == "{ truncated"
    with pytest.raises(library_state.AnalysisUnreadable):
        library_state.load_analysis(record)


def test_handle_duplicates_refreshes_the_recents_counts(monkeypatch, tmp_path) -> None:
    """The recents row's counts must follow the prune.

    `save_analysis` deliberately leaves the index alone, so a prune that dropped
    50 ghost rows still left the startup screen advertising the pre-prune
    "1,200 tracks · 1,200 analyzed" for a library whose saved report now holds
    1,150 — the number the user picks the library BY, wrong until the next full
    analyze rewrote it.
    """
    from vibechek import library_state

    lib = tmp_path / "lib"
    lib.mkdir()
    keep, gone = lib / "keep.mp3", lib / "gone.mp3"
    record = _seed_analysis(lib, [keep, gone])
    assert (record.track_count, record.analyzed_count) == (2, 2)

    _stub_handle_duplicates(monkeypatch, {
        "moved": 0, "deleted": 1, "errors": 0, "error_messages": [],
        "deleted_paths": [str(gone)],
    })
    rpc._handle_duplicates({
        "report": _EMPTY_DUPE_REPORT, "action": "trash", "library_path": str(lib),
    })

    row = next(r for r in library_state.load_state().recent if r.path == str(lib))
    assert (row.track_count, row.analyzed_count) == (1, 1)


def test_handle_duplicates_count_refresh_is_housekeeping_not_a_new_analysis(
    monkeypatch, tmp_path,
) -> None:
    """Only the counts move: no re-ordering of recents, no `last_analyzed` bump.

    A dedupe is not an analyze, and silently promoting a library to the top of
    the startup list (or claiming it was just analyzed) is its own bug.
    """
    from vibechek import library_state

    lib, newer = tmp_path / "lib", tmp_path / "newer"
    lib.mkdir()
    newer.mkdir()
    gone = lib / "gone.mp3"
    _seed_analysis(lib, [lib / "keep.mp3", gone])
    _seed_analysis(newer, [newer / "a.mp3"])  # recorded last → front of recents

    before = next(r for r in library_state.load_state().recent if r.path == str(lib))
    analyzed_at = before.last_analyzed

    _stub_handle_duplicates(monkeypatch, {
        "moved": 0, "deleted": 1, "errors": 0, "error_messages": [],
        "deleted_paths": [str(gone)],
    })
    rpc._handle_duplicates({
        "report": _EMPTY_DUPE_REPORT, "action": "trash", "library_path": str(lib),
    })

    recent = library_state.load_state().recent
    assert [r.path for r in recent] == [str(newer), str(lib)]
    row = next(r for r in recent if r.path == str(lib))
    assert row.last_analyzed == analyzed_at
    assert row.track_count == 1


def test_handle_duplicates_leaves_the_counts_alone_when_nothing_changed(
    monkeypatch, tmp_path,
) -> None:
    """No row changed → no index write. The prune must not touch a library whose
    saved analysis it did not rewrite.
    """
    from vibechek import library_state

    lib = tmp_path / "lib"
    lib.mkdir()
    _seed_analysis(lib, [lib / "keep.mp3", lib / "other.mp3"])

    _stub_handle_duplicates(monkeypatch, {
        "moved": 0, "deleted": 1, "errors": 0, "error_messages": [],
        # A path that is not in the saved analysis at all.
        "deleted_paths": [str(tmp_path / "elsewhere" / "gone.mp3")],
    })
    rpc._handle_duplicates({
        "report": _EMPTY_DUPE_REPORT, "action": "trash", "library_path": str(lib),
    })

    row = next(r for r in library_state.load_state().recent if r.path == str(lib))
    assert (row.track_count, row.analyzed_count) == (2, 2)


def test_verify_models_pins_the_onnx_backbone(monkeypatch, tmp_path) -> None:
    """The backbone's pin lives in model_download (it is FETCHED upstream, not
    converted here), so looking it up in MODEL_SHA256_ONNX always missed and the
    GUI reported the first file the ONNX stack loads as an unpinned "no pin" —
    the tamper check never ran on it. cli.py's verify-models already used the
    real pin; the two must agree.
    """
    from vibechek import config as cfg_mod
    from vibechek.config import VibechekConfig
    from vibechek.model_download import BACKBONE_ONNX_SHA256
    from vibechek.onnx_backend import BACKBONE_ONNX_FILENAME

    monkeypatch.setattr(cfg_mod, "MODELS_DIR", tmp_path)
    monkeypatch.setattr(rpc, "VibechekConfig", VibechekConfig)
    cfg = VibechekConfig()
    cfg.analysis.inference_engine = "onnx"
    monkeypatch.setattr(VibechekConfig, "load", classmethod(lambda cls: cfg))

    onnx_dir = tmp_path / "onnx"
    onnx_dir.mkdir()
    (onnx_dir / BACKBONE_ONNX_FILENAME).write_bytes(b"tampered backbone")

    by_name = {r["name"]: r for r in rpc._verify_models({})["results"]}
    backbone = by_name[BACKBONE_ONNX_FILENAME]
    assert backbone["expected"] == BACKBONE_ONNX_SHA256
    assert backbone["ok"] is False
    assert backbone.get("reason") != "missing"


def test_organize_repaths_the_saved_analysis(monkeypatch, tmp_path) -> None:
    """Sibling of the dedupe prune, and the cause named in
    _resolve_genre_conflicts' "none of the selected tracks are in the saved
    analysis" branch: organize moves the files and the GUI store follows them in
    memory, but the analysis JSON on disk kept its PRE-move paths — so after a
    reload every approval, tag write and second organize matched nothing.
    """
    from vibechek import library_state
    from vibechek.organizer import OrganizeStats

    lib = tmp_path / "lib"
    lib.mkdir()
    src = lib / "a.mp3"
    record = _seed_analysis(lib, [src])
    dst = lib / "House" / "a.mp3"

    monkeypatch.setattr(
        "vibechek.organizer.organize_from_analysis",
        lambda *_a, **_k: OrganizeStats(
            planned=1, moved=1, moved_pairs=[(str(src), str(dst))],
        ),
    )
    rpc._organize({
        "analysis": {"tracks": []}, "library_path": str(lib),
    })

    assert _saved_paths(record) == [str(dst)]
    # A re-path is not a removal.
    assert library_state.load_analysis(record)["summary"]["total_files"] == 1


def test_organize_dry_run_leaves_the_saved_analysis_alone(monkeypatch, tmp_path) -> None:
    """A dry run moves nothing, so it must rewrite nothing (organize_from_analysis
    returns empty moved_pairs for it — this pins that the RPC honours that).
    """
    from vibechek.organizer import OrganizeStats

    lib = tmp_path / "lib"
    lib.mkdir()
    src = lib / "a.mp3"
    record = _seed_analysis(lib, [src])

    monkeypatch.setattr(
        "vibechek.organizer.organize_from_analysis",
        lambda *_a, **_k: OrganizeStats(planned=1, moved=0),
    )
    rpc._organize({
        "analysis": {"tracks": []}, "library_path": str(lib), "dry_run": True,
    })

    assert _saved_paths(record) == [str(src)]


# ---------------------------------------------------------------------------
# post-dedupe summary recompute uses the analyzer's own analyzed/errors rule
# ---------------------------------------------------------------------------


def test_rewrite_analysis_tracks_recomputes_analyzed_and_errors_honestly() -> None:
    """A per-track ML failure leaves `error` unset and `ml_analysis` a truthy
    `{"ml_error": ...}`, so the old "has an ml_analysis dict" rule counted
    failures as analyzed — and `errors` was never recomputed at all. That
    drifted summary is fed to library_state.refresh_record_counts, so the
    recents index re-inflated analyzed_count after every dedupe.
    """
    report = {
        "tracks": [
            {"path": "/lib/gone.mp3", "ml_analysis": {"ml_genre": "House"}},
            {"path": "/lib/ok.mp3", "ml_analysis": {"ml_genre": "Techno"}},
            {"path": "/lib/decode_fail.mp3",
             "ml_analysis": {"ml_error": "Could not decode audio"}},
            {"path": "/lib/hard_fail.mp3", "error": "unreadable"},
        ],
        "summary": {"total_files": 4, "analyzed": 4, "errors": 0},
    }

    changed = rpc._rewrite_analysis_tracks(report, {rpc._path_key("/lib/gone.mp3")}, {})

    assert changed == 1
    assert report["summary"] == {"total_files": 3, "analyzed": 1, "errors": 2}


def test_rewrite_analysis_tracks_never_double_counts_a_failure() -> None:
    """A row carrying BOTH `error` and `ml_error` is one error, not two — same
    as analyzer._build_report."""
    report = {
        "tracks": [
            {"path": "/lib/gone.mp3"},
            {"path": "/lib/both.mp3", "error": "boom",
             "ml_analysis": {"ml_error": "boom"}},
        ],
        "summary": {"total_files": 2, "analyzed": 2, "errors": 0},
    }

    rpc._rewrite_analysis_tracks(report, {rpc._path_key("/lib/gone.mp3")}, {})

    assert report["summary"] == {"total_files": 1, "analyzed": 0, "errors": 1}


def test_rewrite_analysis_tracks_survives_a_corrupt_ml_analysis_row() -> None:
    """A hand-edited or half-written analysis JSON can hold a truthy
    `ml_analysis` that is not a dict. The recompute's `(x or {}).get(...)` raised
    AttributeError on it, and nothing between here and `_handle_duplicates`
    catches that — so the user was told the dedupe FAILED after their duplicates
    had already been trashed. A row we cannot read counts as failed."""
    report = {
        "tracks": [
            {"path": "/lib/gone.mp3", "ml_analysis": {"ml_genre": "House"}},
            {"path": "/lib/ok.mp3", "ml_analysis": {"ml_genre": "Techno"}},
            {"path": "/lib/weird.mp3", "ml_analysis": "legacy string"},
        ],
        "summary": {"total_files": 3, "analyzed": 3, "errors": 0},
    }

    changed = rpc._rewrite_analysis_tracks(report, {rpc._path_key("/lib/gone.mp3")}, {})

    assert changed == 1
    assert report["summary"] == {"total_files": 2, "analyzed": 1, "errors": 1}


def test_organize_to_a_target_outside_the_library_still_repaths_its_rows(
    monkeypatch, tmp_path,
) -> None:
    """The containment rule is scoped to DEDUPE on purpose.

    A user who organizes into a target outside their library has moved the whole
    library there; dropping every row would discard the entire ML analysis with
    nothing to replace it. A quarantined duplicate is different — its keeper's
    row stays behind, so the row that left is redundant.
    """
    from vibechek import library_state

    lib = tmp_path / "lib"
    lib.mkdir()
    track = lib / "t.mp3"
    record = _seed_analysis(lib, [track])
    dst = tmp_path / "sorted" / "House" / "t.mp3"

    from types import SimpleNamespace

    stats = SimpleNamespace(moved_pairs=[(str(track), str(dst))])
    rpc._sync_saved_analysis_after_organize(stats, {"library_path": str(lib)})

    saved = library_state.load_analysis(record)
    assert [t["path"] for t in saved["tracks"]] == [str(dst)]
