"""Tests for vibechek.wsl.

Focus on the pure logic: path translation, distro list parsing, _shell_quote,
WSLStatus computed properties, _wsl_run decoding. Real subprocess calls are
mocked so these tests run anywhere — Linux, macOS, or Windows.
"""

from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

from vibechek.wsl import (
    DistroInfo,
    WSLStatus,
    _parse_distro_list,
    _shell_quote,
    _wsl_run,
    to_dict,
    win_to_wsl_path,
    wsl_to_win_path,
)


# ---------------------------------------------------------------------------
# Path translation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "win,wsl",
    [
        ("C:\\Users\\Jack\\Music", "/mnt/c/Users/Jack/Music"),
        ("C:/Users/Jack/Music", "/mnt/c/Users/Jack/Music"),
        ("D:\\Tracks", "/mnt/d/Tracks"),
        ("z:\\foo\\bar\\baz", "/mnt/z/foo/bar/baz"),
    ],
)
def test_win_to_wsl_path_translates(win: str, wsl: str) -> None:
    assert win_to_wsl_path(win) == wsl


def test_win_to_wsl_path_passthrough_for_non_windows() -> None:
    # Already WSL-shaped or not a drive path → return as-is
    assert win_to_wsl_path("/mnt/c/foo") == "/mnt/c/foo"
    assert win_to_wsl_path("/home/user") == "/home/user"
    assert win_to_wsl_path("") == ""


@pytest.mark.parametrize(
    "wsl,win",
    [
        ("/mnt/c/Users/Jack/Music", "C:\\Users\\Jack\\Music"),
        ("/mnt/d/Tracks", "D:\\Tracks"),
        ("/mnt/c", "C:\\"),
        ("/mnt/c/", "C:\\"),
    ],
)
def test_wsl_to_win_path_translates(wsl: str, win: str) -> None:
    assert wsl_to_win_path(wsl) == win


def test_wsl_to_win_path_passthrough() -> None:
    assert wsl_to_win_path("/home/user") == "/home/user"
    assert wsl_to_win_path("") == ""


def test_path_translation_round_trip() -> None:
    original = "C:\\Users\\Jack\\Music\\track.mp3"
    assert wsl_to_win_path(win_to_wsl_path(original)) == original


# ---------------------------------------------------------------------------
# _shell_quote
# ---------------------------------------------------------------------------


def test_shell_quote_empty() -> None:
    assert _shell_quote("") == "''"


@pytest.mark.parametrize(
    "raw",
    ["foo", "abc123", "path/to/file", "key=value", "a-b_c.d:e"],
)
def test_shell_quote_unquoted_safe_chars(raw: str) -> None:
    # Safe chars (alnum, _ . / = : -) don't need quoting
    assert _shell_quote(raw) == raw


def test_shell_quote_wraps_special_chars() -> None:
    quoted = _shell_quote("hello world")
    assert quoted == "'hello world'"


def test_shell_quote_escapes_single_quotes() -> None:
    # The POSIX trick: end the quote, escaped-quote literal, reopen quote.
    quoted = _shell_quote("it's")
    assert quoted == "'it'\"'\"'s'"


def test_shell_quote_handles_dollar_and_backtick() -> None:
    quoted = _shell_quote("$HOME")
    assert quoted.startswith("'") and quoted.endswith("'")
    assert "$HOME" in quoted


# ---------------------------------------------------------------------------
# _parse_distro_list
# ---------------------------------------------------------------------------


def test_parse_distro_list_typical_output() -> None:
    stdout = (
        "  NAME                STATE     VERSION\n"
        "* Ubuntu-24.04        Running   2\n"
        "  Debian              Stopped   2\n"
    )
    distros = _parse_distro_list(stdout)
    assert len(distros) == 2

    ubuntu = distros[0]
    assert ubuntu.name == "Ubuntu-24.04"
    assert ubuntu.state == "Running"
    assert ubuntu.version == "2"
    assert ubuntu.is_default is True

    debian = distros[1]
    assert debian.name == "Debian"
    assert debian.state == "Stopped"
    assert debian.is_default is False


def test_parse_distro_list_no_distros() -> None:
    # Header only
    assert _parse_distro_list("  NAME  STATE  VERSION\n") == []


def test_parse_distro_list_empty_string() -> None:
    assert _parse_distro_list("") == []


def test_parse_distro_list_partial_row() -> None:
    # If a row has fewer than 3 fields, we still capture the name.
    stdout = "  NAME  STATE  VERSION\n  Ubuntu-24.04\n"
    distros = _parse_distro_list(stdout)
    assert len(distros) == 1
    assert distros[0].name == "Ubuntu-24.04"


# ---------------------------------------------------------------------------
# WSLStatus computed properties
# ---------------------------------------------------------------------------


def test_wsl_status_can_run_vibechek_true_when_both_installed() -> None:
    s = WSLStatus(
        is_windows=True,
        wsl_available=True,
        wsl_feature_enabled=True,
        distros=[
            DistroInfo(name="Ubuntu-24.04", vibechek_installed=True, essentia_installed=True),
        ],
    )
    assert s.can_run_vibechek is True


def test_wsl_status_can_run_vibechek_false_when_only_one_installed() -> None:
    s = WSLStatus(
        is_windows=True,
        wsl_available=True,
        wsl_feature_enabled=True,
        distros=[
            DistroInfo(name="Ubuntu-24.04", vibechek_installed=True, essentia_installed=False),
        ],
    )
    assert s.can_run_vibechek is False


def test_wsl_status_can_run_vibechek_empty_distros() -> None:
    s = WSLStatus(is_windows=True, wsl_available=True, wsl_feature_enabled=True)
    assert s.can_run_vibechek is False


def test_wsl_status_usable_distro_prefers_default() -> None:
    s = WSLStatus(
        is_windows=True,
        wsl_available=True,
        wsl_feature_enabled=True,
        distros=[
            DistroInfo(name="Debian", vibechek_installed=True, essentia_installed=True),
            DistroInfo(
                name="Ubuntu-24.04",
                vibechek_installed=True,
                essentia_installed=True,
                is_default=True,
            ),
        ],
    )
    assert s.usable_distro == "Ubuntu-24.04"


def test_wsl_status_usable_distro_falls_back_to_first_ready() -> None:
    s = WSLStatus(
        is_windows=True,
        wsl_available=True,
        wsl_feature_enabled=True,
        distros=[
            DistroInfo(name="Debian", vibechek_installed=True, essentia_installed=True),
            DistroInfo(name="Ubuntu-24.04", vibechek_installed=False, essentia_installed=False),
        ],
    )
    assert s.usable_distro == "Debian"


def test_wsl_status_usable_distro_none_when_no_ready() -> None:
    s = WSLStatus(
        is_windows=True,
        wsl_available=True,
        wsl_feature_enabled=True,
        distros=[DistroInfo(name="Ubuntu-24.04")],
    )
    assert s.usable_distro is None


# ---------------------------------------------------------------------------
# to_dict
# ---------------------------------------------------------------------------


def test_to_dict_includes_computed_props() -> None:
    s = WSLStatus(
        is_windows=True,
        wsl_available=True,
        wsl_feature_enabled=True,
        distros=[
            DistroInfo(
                name="Ubuntu-24.04",
                vibechek_installed=True,
                essentia_installed=True,
                is_default=True,
            ),
        ],
    )
    d = to_dict(s)
    assert d["can_run_vibechek"] is True
    assert d["usable_distro"] == "Ubuntu-24.04"
    assert d["is_windows"] is True
    assert len(d["distros"]) == 1


# ---------------------------------------------------------------------------
# _wsl_run encoding handling
# ---------------------------------------------------------------------------


def test_wsl_run_decodes_utf16le_output() -> None:
    """wsl.exe outputs UTF-16 LE — _wsl_run should strip the BOM and null bytes."""
    fake_stdout = "Hello".encode("utf-16-le")
    fake = subprocess.CompletedProcess([], 0, fake_stdout, b"")
    with patch("vibechek.wsl.subprocess.run", return_value=fake):
        result = _wsl_run(["wsl", "--status"])
    assert result.stdout == "Hello"
    assert result.returncode == 0


def test_wsl_run_handles_utf8_output() -> None:
    fake = subprocess.CompletedProcess([], 0, b"plain utf-8", b"")
    with patch("vibechek.wsl.subprocess.run", return_value=fake):
        result = _wsl_run(["wsl", "--status"])
    # utf-16-le of "plain utf-8" happens to decode to garbage, but we tolerate;
    # the important thing is decoding doesn't crash.
    assert isinstance(result.stdout, str)


def test_wsl_run_returns_returncode() -> None:
    fake = subprocess.CompletedProcess([], 1, b"", b"")
    with patch("vibechek.wsl.subprocess.run", return_value=fake):
        result = _wsl_run(["wsl", "--status"])
    assert result.returncode == 1


# ---------------------------------------------------------------------------
# detect_wsl — only the no-op branch on non-Windows; mock on Windows path
# ---------------------------------------------------------------------------


def test_detect_wsl_non_windows_returns_empty_status(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("vibechek.wsl.IS_WINDOWS", False)
    from vibechek.wsl import detect_wsl

    status = detect_wsl()
    assert status.is_windows is False
    assert status.wsl_available is False
    assert status.distros == []


def test_detect_wsl_missing_wsl_exe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("vibechek.wsl.IS_WINDOWS", True)
    monkeypatch.setattr("vibechek.wsl.shutil.which", lambda _name: None)
    from vibechek.wsl import detect_wsl

    status = detect_wsl()
    assert status.is_windows is True
    assert status.wsl_available is False


def test_detect_wsl_status_feature_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """If wsl --status returns non-zero, feature is disabled."""
    monkeypatch.setattr("vibechek.wsl.IS_WINDOWS", True)
    monkeypatch.setattr("vibechek.wsl.shutil.which", lambda _name: "C:\\Windows\\wsl.exe")
    fake = subprocess.CompletedProcess([], 1, "", "")
    monkeypatch.setattr("vibechek.wsl._wsl_run", lambda *_a, **_k: fake)
    from vibechek.wsl import detect_wsl

    status = detect_wsl(quick=True)
    assert status.wsl_available is True
    assert status.wsl_feature_enabled is False


def test_detect_wsl_quick_mode_skips_probes(monkeypatch: pytest.MonkeyPatch) -> None:
    """quick=True should never call _probe_distro."""
    monkeypatch.setattr("vibechek.wsl.IS_WINDOWS", True)
    monkeypatch.setattr("vibechek.wsl.shutil.which", lambda _name: "C:\\Windows\\wsl.exe")

    status_output = subprocess.CompletedProcess([], 0, "ok", "")
    list_output = subprocess.CompletedProcess(
        [],
        0,
        "  NAME       STATE    VERSION\n* Ubuntu-24.04  Running  2\n",
        "",
    )
    calls: list[tuple] = []

    def fake_run(cmd, **kwargs):
        calls.append(tuple(cmd))
        if "--status" in cmd:
            return status_output
        if "--list" in cmd:
            return list_output
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr("vibechek.wsl._wsl_run", fake_run)

    probe_called = []
    monkeypatch.setattr(
        "vibechek.wsl._probe_distro",
        lambda *_a, **_k: probe_called.append(True),
    )

    from vibechek.wsl import detect_wsl
    status = detect_wsl(quick=True)
    assert status.wsl_feature_enabled is True
    assert status.default_distro == "Ubuntu-24.04"
    assert probe_called == []
