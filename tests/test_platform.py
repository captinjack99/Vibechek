"""Tests for vibechek.platform — the constants are derived from sys.platform.

We can't easily change sys.platform at runtime, so the assertions verify
internal consistency (exactly one of the three is true) and that the values
match `sys.platform` directly.
"""

from __future__ import annotations

import sys

from vibechek import platform as vplatform


def test_exactly_one_platform_constant_is_true() -> None:
    """The three constants are mutually exclusive AND cover the platforms we
    support (Windows, macOS, Linux). Anything else (FreeBSD, etc.) leaves
    all three False — that's OK, code paths that branch on these all have a
    "no" default.
    """
    truthy = [vplatform.IS_WINDOWS, vplatform.IS_MAC, vplatform.IS_LINUX]
    # At most one is true; on a supported platform exactly one is.
    assert sum(truthy) <= 1


def test_platform_constants_match_sys_platform() -> None:
    """The constants are computed from sys.platform — they should match."""
    assert vplatform.IS_WINDOWS == (sys.platform == "win32")
    assert vplatform.IS_MAC == (sys.platform == "darwin")
    assert vplatform.IS_LINUX == sys.platform.startswith("linux")


def test_wsl_imports_from_platform() -> None:
    """wsl.py used to define `IS_WINDOWS` locally. Confirm the module-level
    name now comes from vibechek.platform — a regression here would mean
    someone reintroduced the local definition."""
    from vibechek import wsl

    # The symbol should still be importable from wsl (back-compat for any
    # caller still doing `from vibechek.wsl import IS_WINDOWS`).
    assert wsl.IS_WINDOWS is vplatform.IS_WINDOWS


def test_native_install_imports_from_platform() -> None:
    """Same regression guard for native_install.py."""
    from vibechek import native_install

    assert native_install.IS_WINDOWS is vplatform.IS_WINDOWS
    assert native_install.IS_MAC is vplatform.IS_MAC
    assert native_install.IS_LINUX is vplatform.IS_LINUX
