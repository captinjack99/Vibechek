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

# Python RPC methods whose TypeScript wiring (methods.ts RPC_METHODS + the rpc.ts
# wrapper + the rpc.test.ts mirror) is deliberately deferred to later frontend
# work. Empty now: `increase_wsl_memory` (the ".wslconfig memory" self-heal
# RPC) got its typed wrapper, registry entry, and mirror — so the sync guard
# covers it directly again. Add a name here only to defer a *future*
# backend-only method's TS wiring.
_PENDING_TS_WIRING: set[str] = set()


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

    # Methods whose TS wiring is a known follow-up (see _PENDING_TS_WIRING) don't
    # count as drift — but if the wiring lands, drop them from the allowlist so
    # the guard covers them again (the assertion below catches a stale allowlist).
    missing_from_ts = sorted(py_methods - ts_methods - _PENDING_TS_WIRING)
    stale_in_ts = sorted(ts_methods - py_methods)
    already_wired = _PENDING_TS_WIRING & ts_methods
    assert not already_wired, (
        "These methods are now wired in methods.ts — remove them from "
        f"_PENDING_TS_WIRING in this test: {sorted(already_wired)}"
    )

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


# ---------------------------------------------------------------------------
# The other half of the same guarantee: the TS codegen must actually WALK every
# dataclass module, and must refuse to emit a contract it couldn't type. These
# live here (rather than in a script-specific module) because they guard the
# same Python↔TypeScript contract as the method-name check above.
# ---------------------------------------------------------------------------


def _load_generator():
    """Import scripts/generate_ts_types.py as a module (it isn't a package)."""
    import importlib.util

    path = _REPO_ROOT / "scripts" / "generate_ts_types.py"
    spec = importlib.util.spec_from_file_location("_gen_ts_types", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_ts_codegen_walks_every_dataclass_module() -> None:
    """The walk is derived from the source tree, not a hand-kept list.

    A hard-coded MODULES list reached 10 of the 19 `vibechek/*.py` modules that
    define dataclasses, so `tagger`'s ApplyStats / BackupStats / RestoreStats —
    returned verbatim over JSON-RPC — sat outside the drift gate entirely.
    """
    gen = _load_generator()
    walked = set(gen.all_dataclass_modules())

    on_disk = {
        f"vibechek.{p.stem}"
        for p in sorted((_REPO_ROOT / "vibechek").glob("*.py"))
        if p.name != "__init__.py"
        and re.search(r"^\s*@(?:dataclasses\.)?dataclass\b", p.read_text(encoding="utf-8"),
                      re.MULTILINE)
    }
    assert on_disk - walked == set(), f"dataclass modules outside the codegen walk: {on_disk - walked}"
    # The four wire payloads that motivated this: `asdict(stats)` straight onto
    # the JSON-RPC wire from _apply_tags / _backup_tags / _restore_tags.
    assert "vibechek.tagger" in walked


def test_generated_ts_covers_the_tagger_wire_payloads() -> None:
    """`ApplyStats` & friends must exist in the committed generated.ts."""
    generated = (_REPO_ROOT / "ui" / "src" / "types" / "generated.ts").read_text(encoding="utf-8")
    for name in ("ApplyStats", "BackupStats", "RestoreStats", "RemapRestoreStats"):
        assert f"export interface {name} " in generated, f"{name} missing from generated.ts"


def test_ts_codegen_records_untranslatable_fields_instead_of_emitting_unknown() -> None:
    """`unknown` is assignable from anything — emitting it silently would make
    the drift gate permanently green over a contract it stopped enforcing."""
    gen = _load_generator()
    gen._DEGRADED.clear()

    class Weird:
        pass

    assert gen.translate(Weird, set(), "Thing.field") == "unknown"
    assert len(gen._DEGRADED) == 1
    assert "Thing.field" in gen._DEGRADED[0]


def test_ts_codegen_records_a_class_whose_hints_do_not_resolve() -> None:
    """A failed `get_type_hints` degrades EVERY field of the class to unknown."""
    import dataclasses

    gen = _load_generator()
    gen._DEGRADED.clear()

    @dataclasses.dataclass
    class Broken:
        ml_bpm: NoSuchTypeAnywhere  # noqa: F821 — deliberately unresolvable

    out = gen.emit_interface(Broken, set())
    assert "ml_bpm: unknown;" in out
    assert any("could not resolve type hints" in m for m in gen._DEGRADED)


def test_ts_codegen_main_refuses_to_write_a_degraded_contract(monkeypatch) -> None:
    gen = _load_generator()
    gen._DEGRADED.clear()
    gen._DEGRADED.append("Fake.field: unhandled annotation")
    monkeypatch.setattr("sys.argv", ["generate_ts_types.py", "--check"])
    assert gen.main() == 1


# ---------------------------------------------------------------------------
# scripts/update_readme_stats.py — the README counter is committed, so a count
# the script could not establish must abort, never fall back to 0.
# ---------------------------------------------------------------------------


def _load_readme_stats():
    import importlib.util

    path = _REPO_ROOT / "scripts" / "update_readme_stats.py"
    spec = importlib.util.spec_from_file_location("_readme_stats", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_readme_stats_raises_when_pytest_collection_fails(monkeypatch) -> None:
    """`return 0` here wrote "**0 Python tests**" into README.md and exited 0."""
    import subprocess

    stats = _load_readme_stats()

    def fake_run(*_a, **_kw):
        # A conftest import error: exit 4, nothing on stdout, all detail on stderr.
        return subprocess.CompletedProcess(
            args=[], returncode=4, stdout="",
            stderr="ImportError while loading conftest 'tests/conftest.py'",
        )

    monkeypatch.setattr(stats.subprocess, "run", fake_run)
    with pytest.raises(stats.StatsError) as exc:
        stats.count_tests()
    assert "conftest" in str(exc.value)  # the discarded stderr is surfaced


def test_readme_stats_rejects_a_partial_collection(monkeypatch) -> None:
    """"2 tests collected, 1 error" is a partial count — just as fabricated."""
    import subprocess

    stats = _load_readme_stats()

    def fake_run(*_a, **_kw):
        return subprocess.CompletedProcess(
            args=[], returncode=2,
            stdout="ERROR tests/test_ml.py\n2 tests collected, 1 error in 0.21s\n",
            stderr="",
        )

    monkeypatch.setattr(stats.subprocess, "run", fake_run)
    with pytest.raises(stats.StatsError):
        stats.count_tests()
