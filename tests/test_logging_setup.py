"""Tests for vibechek.logging_setup."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from vibechek import logging_setup


@pytest.fixture(autouse=True)
def _isolated_log_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect LOG_DIR / LOG_FILE to tmp_path and reset module state."""
    log_dir = tmp_path / "logs"
    log_file = log_dir / "vibechek.log"
    monkeypatch.setattr(logging_setup, "LOG_DIR", log_dir)
    monkeypatch.setattr(logging_setup, "LOG_FILE", log_file)
    monkeypatch.setattr(logging_setup, "_configured", False)

    # Snapshot and restore root handlers so we don't pollute pytest's own logging.
    root = logging.getLogger()
    original_handlers = list(root.handlers)
    original_level = root.level
    yield tmp_path
    # Tear down: close any file handlers we added so Windows can delete tmp_path
    for h in list(root.handlers):
        if h not in original_handlers:
            root.removeHandler(h)
            try:
                h.close()
            except Exception:  # noqa: BLE001
                pass
    root.setLevel(original_level)


def test_configure_creates_log_file_on_emit() -> None:
    logging_setup.configure()
    logging.getLogger("test").warning("hello world")
    assert logging_setup.LOG_FILE.exists()
    content = logging_setup.LOG_FILE.read_text(encoding="utf-8")
    assert "hello world" in content


def test_configure_is_idempotent() -> None:
    logging_setup.configure()
    root = logging.getLogger()
    handler_count_first = len(root.handlers)

    logging_setup.configure()
    handler_count_second = len(root.handlers)
    assert handler_count_first == handler_count_second


def test_configure_respects_level() -> None:
    logging_setup.configure(level="DEBUG")
    root = logging.getLogger()
    assert root.level == logging.DEBUG


def test_configure_unknown_level_defaults_to_info() -> None:
    logging_setup.configure(level="not-a-level")
    root = logging.getLogger()
    assert root.level == logging.INFO


def test_tail_returns_empty_when_no_log_file() -> None:
    # Before any configure() call
    assert logging_setup.tail() == []


def test_tail_returns_last_n_lines() -> None:
    logging_setup.configure()
    log = logging.getLogger("tail-test")
    for i in range(20):
        log.warning("line %d", i)

    tail_5 = logging_setup.tail(n=5)
    assert len(tail_5) == 5
    assert "line 19" in tail_5[-1]
    assert "line 15" in tail_5[0]


def test_tail_default_n_returns_all_when_fewer() -> None:
    logging_setup.configure()
    logging.getLogger("tail-test").warning("only-line")
    lines = logging_setup.tail()
    assert any("only-line" in ln for ln in lines)


def test_tail_strips_trailing_newlines() -> None:
    logging_setup.configure()
    logging.getLogger("t").warning("hello")
    lines = logging_setup.tail()
    assert all(not ln.endswith("\n") for ln in lines)
