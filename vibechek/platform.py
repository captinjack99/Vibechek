"""Platform detection constants.

`sys.platform` is the canonical "what OS am I on" probe, but checking it as
`sys.platform == "win32"` in five different modules invites typos ("win32"
vs "windows", "darwin" vs "macos") and the duplication makes mocking in
tests harder. We centralize it here once and let everyone import from a
single source.

Test trick: monkeypatch these module-level booleans (not `sys.platform`) when
you need to simulate "I'm on Windows" or "I'm on macOS" in a test running on
Linux CI. Each consumer should import `IS_WINDOWS` / `IS_MAC` / `IS_LINUX`
from here at call time (`from vibechek.platform import IS_WINDOWS`) rather
than caching the value at module import — that way the monkeypatch wins.
"""

from __future__ import annotations

import sys

IS_WINDOWS: bool = sys.platform == "win32"
IS_MAC: bool = sys.platform == "darwin"
IS_LINUX: bool = sys.platform.startswith("linux")

__all__ = ["IS_WINDOWS", "IS_MAC", "IS_LINUX"]
