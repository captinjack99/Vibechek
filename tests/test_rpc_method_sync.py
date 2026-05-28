"""Cross-language guardrail: the Python RPC METHODS registry must stay in
sync with the TypeScript `RPC_METHODS` array the frontend uses.

This lives in pytest (not vitest) because Python can read BOTH source files
directly and authoritatively — the previous vitest-side check mirrored the
method list by hand and compared it to itself, so 7 real methods silently
drifted out of sync (missing from the TS wrappers entirely) while the test
stayed green.

If this test fails, the two sides have diverged:
  - A method in Python but not TS → the frontend can't call it type-safely
    (and `ui/src/api/rpc.ts` is probably missing a wrapper).
  - A method in TS but not Python → a dead/typo'd method name the GUI would
    call and get METHOD_NOT_FOUND at runtime.

Fix by adding/removing the name in `vibechek/rpc.py:METHODS`,
`ui/src/api/methods.ts:RPC_METHODS`, the matching wrapper in
`ui/src/api/rpc.ts`, and the mirror in `ui/src/api/rpc.test.ts`.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from vibechek.rpc import METHODS

_REPO_ROOT = Path(__file__).resolve().parent.parent
_METHODS_TS = _REPO_ROOT / "ui" / "src" / "api" / "methods.ts"


def _parse_ts_rpc_methods(source: str) -> list[str]:
    """Extract the string literals from the `RPC_METHODS = [ ... ] as const`
    array in methods.ts. Ignores `//` line comments so section headers like
    `// diagnostics` don't get mistaken for entries."""
    m = re.search(r"RPC_METHODS\s*=\s*\[(.*?)\]\s*as\s+const", source, re.DOTALL)
    if not m:
        raise AssertionError("Could not find `RPC_METHODS = [...] as const` in methods.ts")
    body = m.group(1)
    # Strip // line comments before pulling string literals.
    body = re.sub(r"//[^\n]*", "", body)
    return re.findall(r'"([a-z_]+)"', body)


@pytest.mark.skipif(not _METHODS_TS.exists(), reason="frontend methods.ts not present")
def test_python_methods_match_ts_rpc_methods() -> None:
    py_methods = set(METHODS.keys())
    ts_methods = set(_parse_ts_rpc_methods(_METHODS_TS.read_text(encoding="utf-8")))

    missing_from_ts = sorted(py_methods - ts_methods)
    stale_in_ts = sorted(ts_methods - py_methods)

    assert not missing_from_ts, (
        "Python RPC methods missing from ui/src/api/methods.ts:RPC_METHODS "
        f"(add a typed wrapper too): {missing_from_ts}"
    )
    assert not stale_in_ts, (
        "ui/src/api/methods.ts:RPC_METHODS has names not registered in "
        f"vibechek/rpc.py:METHODS: {stale_in_ts}"
    )


@pytest.mark.skipif(not _METHODS_TS.exists(), reason="frontend methods.ts not present")
def test_ts_rpc_methods_has_no_duplicates() -> None:
    names = _parse_ts_rpc_methods(_METHODS_TS.read_text(encoding="utf-8"))
    dupes = sorted({n for n in names if names.count(n) > 1})
    assert not dupes, f"Duplicate entries in RPC_METHODS: {dupes}"
