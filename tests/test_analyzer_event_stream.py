"""Tests for the structured `VIBECHEK_EVENT` progress + per-track stream.

Two channels live on top of the subprocess `stderr`:

  1. `VIBECHEK_EVENT\\t<type>\\t<json>` lines emitted by the child
     `vibechek analyze` process (WSL or managed-venv routed). Each carries
     either a "stage" message (scanning → preflight → spawning workers →
     analyzing) or a "track" record (one per finished file). The parent
     sidecar parses these in `_make_event_aware_line_handler` and re-emits
     them as JSON-RPC notifications the GUI subscribes to.

  2. The legacy Rich-progress "N / M" regex, kept as a fallback for older
     WSL installs and for the interactive CLI.

These tests exercise the parser and the emitter side directly — no
subprocess required.
"""

from __future__ import annotations

import io
import json
import os
import sys
from unittest.mock import MagicMock

import pytest

from vibechek import analyzer


# ---------------------------------------------------------------------------
# _emit_event — the producer side
# ---------------------------------------------------------------------------


def _capture_emit(event_type: str, **payload):
    """Run _emit_event with VIBECHEK_STREAM_PROGRESS=1 against a buffer."""
    # _emit_event reads `_EVENT_STREAM_ON` once at module import. We patch
    # the module flag directly to flip behavior per test instead of
    # re-importing.
    original_flag = analyzer._EVENT_STREAM_ON
    original_stderr = sys.stderr
    try:
        analyzer._EVENT_STREAM_ON = True
        buf = io.StringIO()
        sys.stderr = buf
        analyzer._emit_event(event_type, **payload)
        return buf.getvalue()
    finally:
        analyzer._EVENT_STREAM_ON = original_flag
        sys.stderr = original_stderr


def test_emit_event_is_a_noop_when_env_flag_is_off() -> None:
    """Interactive CLI users must NOT see the structured-event lines."""
    original_flag = analyzer._EVENT_STREAM_ON
    original_stderr = sys.stderr
    try:
        analyzer._EVENT_STREAM_ON = False
        buf = io.StringIO()
        sys.stderr = buf
        analyzer._emit_event("stage", name="scanning")
        assert buf.getvalue() == ""
    finally:
        analyzer._EVENT_STREAM_ON = original_flag
        sys.stderr = original_stderr


def test_emit_event_writes_single_line_with_sentinel_prefix() -> None:
    out = _capture_emit("stage", name="preflight", message="Checking environment...")
    assert out.startswith(analyzer.EVENT_PREFIX)
    assert out.count("\n") == 1, "each event must be exactly one line"
    # Verify the trailing JSON parses cleanly with the expected payload.
    _, event_type, payload = out.rstrip("\n").split("\t", 2)
    assert event_type == "stage"
    assert json.loads(payload) == {"name": "preflight", "message": "Checking environment..."}


def test_emit_event_swallows_unserializable_payload() -> None:
    """A bad payload (e.g. circular reference) must not crash analyze."""
    circular: dict = {}
    circular["self"] = circular
    # No exception even though json.dumps would normally raise.
    out = _capture_emit("track", record=circular)
    assert out == ""


# ---------------------------------------------------------------------------
# _make_event_aware_line_handler — the consumer side
# ---------------------------------------------------------------------------


def test_handler_routes_stage_event_to_on_progress() -> None:
    """Stage events fire on_progress(0, 0, message) — current=total=0 keeps
    the UI's percentage bar in indeterminate mode while the message updates."""
    on_progress = MagicMock()
    handler = analyzer._make_event_aware_line_handler(on_progress=on_progress, on_track=None)
    handler('VIBECHEK_EVENT\tstage\t{"name":"preflight","message":"Checking..."}')
    on_progress.assert_called_once_with(0, 0, "Checking...")


def test_handler_routes_track_event_to_both_callbacks() -> None:
    """A track event fires both on_track (record streaming) AND on_progress
    (so the percentage bar advances). The track event is the canonical
    "1/N done" signal during the per-track streaming phase."""
    on_progress = MagicMock()
    on_track = MagicMock()
    handler = analyzer._make_event_aware_line_handler(on_progress=on_progress, on_track=on_track)
    record = {"path": "/x/foo.flac", "filename": "foo.flac", "ml_analysis": {"ml_genre": "Rock"}}
    line = (
        "VIBECHEK_EVENT\ttrack\t"
        + json.dumps({"index": 5, "total": 12, "record": record})
    )
    handler(line)
    on_progress.assert_called_once_with(5, 12, "foo.flac")
    on_track.assert_called_once_with(record, 5, 12)


def test_handler_falls_back_to_legacy_regex_when_no_event_prefix() -> None:
    """Older WSL installs (or the interactive CLI) don't emit
    VIBECHEK_EVENT lines — only Rich's "Analyzing N / M" text. The parser
    must still scrape progress out of that as a fallback."""
    on_progress = MagicMock()
    handler = analyzer._make_event_aware_line_handler(on_progress=on_progress, on_track=None)
    handler("Analyzing ━━━━━━━━ 5/12 • 0:00:33 • some.flac")
    on_progress.assert_called_once()
    args = on_progress.call_args.args
    assert args[0] == 5
    assert args[1] == 12


def test_handler_filters_essentia_noise_from_stderr_tail() -> None:
    """The bounded error-context buffer must not fill with essentia spam.
    Carried over from the beta.3 stderr-noise filter fix."""
    tail: list[str] = []
    handler = analyzer._make_event_aware_line_handler(
        on_progress=None,
        on_track=None,
        stderr_tail=tail,
        noise_re=re.compile(r"MusicExtractorSVM:"),
    )
    handler("[   INFO   ] MusicExtractorSVM: no classifier models were configured by default")
    handler("Traceback (most recent call last):")
    assert tail == ["Traceback (most recent call last):"]


def test_handler_does_not_crash_on_malformed_event_line() -> None:
    """A garbled event line (truncated JSON, missing tabs) must be skipped
    silently — never raise. Production parses thousands of these per analyze
    and one bad line shouldn't kill the whole run."""
    on_progress = MagicMock()
    handler = analyzer._make_event_aware_line_handler(on_progress=on_progress, on_track=None)
    handler("VIBECHEK_EVENT\tnope")  # missing payload tab
    handler('VIBECHEK_EVENT\tstage\t{"name": "preflight"')  # truncated JSON
    on_progress.assert_not_called()


def test_handler_silences_callback_exceptions() -> None:
    """A handler that raises must NOT prevent later lines from being parsed.
    The analyze pipeline already lost too much progress to swallowed
    exceptions in callbacks before audit #11."""
    def boom(*_args, **_kw):
        raise RuntimeError("simulated UI callback crash")

    handler = analyzer._make_event_aware_line_handler(
        on_progress=boom,
        on_track=boom,
    )
    # If any of these raised, pytest would mark the test failed.
    handler('VIBECHEK_EVENT\tstage\t{"message": "x"}')
    handler('VIBECHEK_EVENT\ttrack\t{"index": 1, "total": 5, "record": {}}')
    handler("Analyzing ━━ 2/5 • foo")


# Imported below the tests so the test names show up before unrelated
# dependencies in pytest collection output. (re is used by _STDERR_NOISE.)
import re  # noqa: E402
