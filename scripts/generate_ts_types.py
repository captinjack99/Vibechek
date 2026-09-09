"""Generate TypeScript interfaces and shared constants from Vibechek's Python source.

Run with:
    ./.venv/Scripts/python.exe scripts/generate_ts_types.py

Outputs (auto-overwritten; do not hand-edit):
    - `ui/src/types/generated.ts`     — TS interfaces for every dataclass
    - `ui/src/lib/keeperConstants.ts` — JSON-compatible constants shared with TS

To extend the mapping, edit this script.

Type mapping (Python -> TS)
---------------------------
    str                          -> string
    int, float                   -> number
    bool                         -> boolean
    Path, PurePath               -> string
    None / NoneType              -> null
    list[X] / List[X]            -> X[]
    tuple[X, ...] / Tuple        -> X[]   (TOML/JSON has no tuple)
    set[X] / frozenset[X]        -> X[]   (serialized as arrays)
    dict[str, X] / Dict[str, X]  -> Record<string, X>
    bare dict                    -> Record<string, unknown>
    bare list/set/tuple          -> unknown[]
    X | None / Optional[X]       -> X | null
    Union[A, B, ...]             -> A | B | ...
    Any                          -> unknown
    Custom dataclass             -> the matching interface name
    Anything else                -> HARD ERROR (see `_degrade`) — a silent
                                    `unknown` would make the drift gate green
                                    over a contract it stopped enforcing.

@property methods on dataclasses are emitted as readonly fields when they
appear in `PROPERTY_FIELDS` below — manually maintained because property
return types aren't preserved by `dataclasses.fields()`.

Sharing a Python constant with the UI
-------------------------------------
The `SHARED_CONSTANTS` list below names a Python attribute and the TS symbol
it should be exported as. Each entry is:

    ("vibechek.module", "_PYTHON_NAME", "TS_EXPORT_NAME", "TS type annotation")

The value must be JSON-serializable (dict/list/str/int/float/bool/None). At
generation time we `import` the module, read the attribute, and emit it via
`json.dumps()` into `ui/src/lib/keeperConstants.ts`. Add a new entry, re-run
the script, and the constant is available in TS — no hand-translation, no
drift.
"""

from __future__ import annotations

import dataclasses
import importlib
import json
import re
import sys
import types
import typing
from pathlib import Path

# Make `vibechek` importable when run from the repo root.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Emission order for the modules that were already covered — keeping them first
# keeps `generated.ts` diffs readable. This list used to BE the whole walk, and
# it reached 10 of the 19 `vibechek/*.py` modules that define dataclasses, so
# tagger's ApplyStats / BackupStats / RestoreStats — returned verbatim over
# JSON-RPC — could be renamed without `--check` noticing a thing. Every other
# dataclass-defining module is now discovered from the source tree and appended
# (sorted) by `all_dataclass_modules()`.
_MODULE_ORDER = [
    "vibechek.resources",
    "vibechek.config",
    "vibechek.wsl",
    "vibechek.native_install",
    "vibechek.preflight",
    "vibechek.analyzer",
    "vibechek.duplicates",
    "vibechek.organizer",
    "vibechek.library_state",
    "vibechek.backup_history",
]

# `@dataclass` / `@dataclasses.dataclass`, with or without arguments.
_DATACLASS_DECORATOR_RE = re.compile(r"^\s*@(?:dataclasses\.)?dataclass\b", re.MULTILINE)

# Fields/classes whose TS type degraded to `unknown` during a render. A
# populated list aborts the run — see `main()`. `typing.Any` is the ONE
# deliberate `unknown` and is not recorded.
_DEGRADED: list[str] = []


def all_dataclass_modules() -> list[str]:
    """Every `vibechek/*.py` module that defines a dataclass, in emission order.

    Derived from the source tree rather than hand-maintained, so a new module
    of wire payloads can't quietly sit outside the drift gate.
    """
    pkg_dir = ROOT / "vibechek"
    found = [
        f"vibechek.{path.stem}"
        for path in sorted(pkg_dir.glob("*.py"))
        if path.name != "__init__.py"
        and _DATACLASS_DECORATOR_RE.search(path.read_text(encoding="utf-8"))
    ]
    stale = [m for m in _MODULE_ORDER if m not in found]
    if stale:
        raise SystemExit(
            f"generate_ts_types: {stale} are listed in _MODULE_ORDER but no "
            f"longer define dataclasses — update the list in this script."
        )
    return [m for m in _MODULE_ORDER if m in found] + [
        m for m in found if m not in _MODULE_ORDER
    ]

# @property fields to surface as readonly interface members. `dataclasses.fields()`
# doesn't see properties; their return types are introspected from annotations
# or hard-coded here when we can't trust the source.
#   "<ClassName>": [(field_name, ts_type_str), ...]
PROPERTY_FIELDS: dict[str, list[tuple[str, str]]] = {
    "SystemResources": [("recommended_workers", "number")],
    "WSLStatus": [
        ("can_run_vibechek", "boolean"),
        ("usable_distro", "string | null"),
    ],
    "PreflightResult": [("reasons_not_ready", "string[]")],
}

# Dataclasses whose JSON-RPC wire shape diverges from the raw dataclass shape.
# Empty now — the previous three exceptions were fixed:
#   - DuplicateGroup: field renamed `keeper` -> `keep` in Python
#   - DuplicateReport: restructured to {summary, exact_duplicates, audio_duplicates}
#   - TrackAnalysis: uses __ts_overrides__ to type existing_tags / ml_analysis
SKIP_CLASSES: set[str] = set()

# Types referenced by `__ts_overrides__` that are declared in the hand-written
# shim (`ui/src/types/index.ts`) rather than generated here. We emit a permissive
# forward-declaration stub for each into `generated.ts` so the file type-checks
# standalone; the shim's narrower declarations shadow these for consumers, since
# `index.ts` does `export * from "./generated"` then re-declares them locally,
# and TS resolves the local declaration in favor of the re-export on conflict.
#
# Add a name here when an `__ts_overrides__` entry references a type that lives
# only in the shim. Anything not in this set and not a generated dataclass will
# fail validation at generation time (see `_validate_override` below).
EXTERNAL_TYPES: set[str] = {"ExistingTags"}

# TS identifiers that are not user-defined types and should be ignored when
# validating override strings. Anything else in an override must resolve to a
# generated dataclass or an EXTERNAL_TYPES entry.
_TS_BUILTINS: set[str] = {
    "string", "number", "boolean", "null", "undefined", "void", "any",
    "never", "unknown", "object",
    "Record", "Array", "Partial", "Required", "Readonly", "Pick", "Omit",
    "ReadonlyArray", "Map", "Set",
    "true", "false",
}

_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

OUTPUT_PATH = ROOT / "ui" / "src" / "types" / "generated.ts"

# Shared constants emitted to ui/src/lib/keeperConstants.ts.
# Each tuple: (python_module, python_attribute, ts_export_name, ts_type_annotation).
# The value at the named attribute must be JSON-serializable.
SHARED_CONSTANTS: list[tuple[str, str, str, str]] = [
    (
        "vibechek.duplicates",
        "_KEEPER_FORMAT_PRIORITY",
        "KEEPER_FORMAT_PRIORITY",
        "Record<string, number>",
    ),
]

CONSTANTS_OUTPUT_PATH = ROOT / "ui" / "src" / "lib" / "keeperConstants.ts"


# ---------------------------------------------------------------------------
# Type translation
# ---------------------------------------------------------------------------


def _is_dataclass_type(obj: object) -> bool:
    return isinstance(obj, type) and dataclasses.is_dataclass(obj)


def _unwrap_optional(tp: object) -> tuple[object, bool]:
    """Return (inner, was_optional). Handles Optional[X] and X | None."""
    args = typing.get_args(tp)
    origin = typing.get_origin(tp)
    if origin is typing.Union or origin is types.UnionType:
        non_none = [a for a in args if a is not type(None)]
        if len(non_none) == 1 and len(args) == 2:
            return non_none[0], True
        # Keep multi-arg unions intact; caller handles them.
    return tp, False


def translate(tp: object, known: set[str], context: str = "<unknown>") -> str:
    """Translate a Python type annotation to TS, given the set of dataclass names.

    `context` names the class.field being rendered so a fallback to `unknown`
    can be reported against something actionable (see `_DEGRADED`).
    """
    # Unwrap Optional first
    inner, optional = _unwrap_optional(tp)
    if optional:
        return f"{translate(inner, known, context)} | null"

    if tp is type(None):
        return "null"

    if tp is typing.Any:
        return "unknown"

    # Primitives
    if tp is str:
        return "string"
    if tp is bool:
        return "boolean"
    if tp in (int, float):
        return "number"

    # Path-like
    if isinstance(tp, type) and issubclass(tp, Path):
        return "string"

    # Generic containers
    origin = typing.get_origin(tp)
    args = typing.get_args(tp)

    if origin in (list, set, frozenset, tuple):
        if not args or (origin is tuple and len(args) == 2 and args[1] is Ellipsis):
            inner_tp = args[0] if args else typing.Any
        else:
            inner_tp = args[0]
        return f"{translate(inner_tp, known, context)}[]"

    if origin is dict:
        key_tp, val_tp = args if len(args) == 2 else (str, typing.Any)
        key_ts = translate(key_tp, known, context)
        # JSON / TS object keys must be strings.
        if key_ts != "string":
            key_ts = "string"
        return f"Record<string, {translate(val_tp, known, context)}>"

    if origin is typing.Union or origin is types.UnionType:
        parts = [translate(a, known, context) for a in args if a is not type(None)]
        if any(a is type(None) for a in args):
            parts.append("null")
        return " | ".join(parts)

    # Custom dataclass reference
    if _is_dataclass_type(tp):
        return tp.__name__

    # Unparameterised containers (`meta: dict`, `names: list`). No element type
    # to translate, but the JSON shape is still known — don't degrade these.
    if tp is dict:
        return "Record<string, unknown>"
    if tp in (list, set, frozenset, tuple):
        return "unknown[]"

    # Bare classes — fall back to the class name if we know it, else unknown.
    if isinstance(tp, type):
        if tp.__name__ in known:
            return tp.__name__
        return _degrade(context, f"unknown type {tp!r}")

    # String forward refs (resolved by get_type_hints normally, but be safe)
    if isinstance(tp, str):
        if tp in known:
            return tp
        return _degrade(context, f"unresolved forward reference {tp!r}")

    return _degrade(context, f"unhandled annotation {tp!r}")


def _degrade(context: str, reason: str) -> str:
    """Record a field that couldn't be typed and return the `unknown` stand-in.

    `unknown` is assignable from anything in TS, so emitting it silently keeps
    the frontend compiling while the wire contract this generator exists to
    enforce has evaporated for that field — and a `--check` that diffs a
    degraded render against a file degraded the same way is green forever.
    `main()` refuses to write or pass with any of these recorded.
    """
    _DEGRADED.append(f"{context}: {reason}")
    return "unknown"


# ---------------------------------------------------------------------------
# Dataclass walk
# ---------------------------------------------------------------------------


def collect_dataclasses(module_names: list[str]) -> list[type]:
    """Return dataclass types defined in the given modules, in declaration order.

    Deduplicates: if the same class is re-exported, the first occurrence wins.
    Honors `SKIP_CLASSES` (skipped names won't appear in the output).
    """
    seen: dict[str, type] = {}
    for name in module_names:
        mod = importlib.import_module(name)
        for attr_name in dir(mod):
            obj = getattr(mod, attr_name)
            if not _is_dataclass_type(obj):
                continue
            # Skip dataclasses defined in a different module (re-exports).
            if obj.__module__ != name:
                continue
            if obj.__name__ in SKIP_CLASSES:
                continue
            if obj.__name__ not in seen:
                seen[obj.__name__] = obj
    return list(seen.values())


def _validate_override(cls_name: str, field_name: str, ts: str, known: set[str]) -> None:
    """Raise if `ts` references identifiers that won't resolve in `generated.ts`.

    Override strings are emitted verbatim — if they name a type that isn't a
    generated dataclass and isn't declared as an EXTERNAL_TYPES stub, the
    resulting `generated.ts` won't compile. Catch it here instead.
    """
    unresolved = {
        ident for ident in _IDENT_RE.findall(ts)
        if ident not in _TS_BUILTINS and ident not in known
    }
    if unresolved:
        raise ValueError(
            f"__ts_overrides__ on {cls_name}.{field_name} references unknown "
            f"type(s) {sorted(unresolved)}: not a generated dataclass and not "
            f"in EXTERNAL_TYPES. Either generate the type, or add it to "
            f"EXTERNAL_TYPES in scripts/generate_ts_types.py."
        )


def emit_interface(cls: type, known: set[str]) -> str:
    """Emit a TS interface for a single dataclass.

    Per-field overrides: if the class defines `__ts_overrides__: dict[str, str]`,
    each field listed there gets its TS type replaced by the override string
    instead of the inferred one. Used when the wire shape is narrower than the
    storage shape (e.g., a `dict[str, Any]` field that's typed as a specific
    interface on the wire).
    """
    try:
        hints = typing.get_type_hints(cls)
    except Exception as e:  # noqa: BLE001
        # Every module here uses `from __future__ import annotations`, so a
        # failed resolve leaves `f.type` a bare string and EVERY field of the
        # class degrades to `unknown`. That aborts the run, it doesn't warn.
        _DEGRADED.append(
            f"{cls.__name__}: could not resolve type hints ({e}) — every field "
            f"would degrade to `unknown`"
        )
        hints = {}

    overrides: dict[str, str] = getattr(cls, "__ts_overrides__", {}) or {}

    lines = [f"export interface {cls.__name__} {{"]
    for f in dataclasses.fields(cls):
        if f.name in overrides:
            ts = overrides[f.name]
            _validate_override(cls.__name__, f.name, ts, known)
        else:
            tp = hints.get(f.name, f.type)
            ts = translate(tp, known, f"{cls.__name__}.{f.name}")
        lines.append(f"  {f.name}: {ts};")

    for prop_name, ts in PROPERTY_FIELDS.get(cls.__name__, []):
        lines.append(f"  readonly {prop_name}: {ts};")

    lines.append("}")
    return "\n".join(lines)


def emit_external_stubs() -> str:
    """Emit permissive forward declarations for shim-declared types.

    These are referenced by `__ts_overrides__` but live in `ui/src/types/index.ts`.
    The stubs let `generated.ts` type-check standalone; the shim re-declares
    them locally with their real shape and TypeScript resolves the local
    declaration in favor of the `export *` re-export on conflict.
    """
    if not EXTERNAL_TYPES:
        return ""
    lines = [
        "// External types — declared in the hand-written shim (./index.ts).",
        "// These permissive stubs let generated.ts type-check standalone; the",
        "// shim re-declares each with its real shape and shadows the re-export.",
    ]
    for name in sorted(EXTERNAL_TYPES):
        lines.append(f"export interface {name} {{ [key: string]: unknown; }}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


HEADER = """// AUTO-GENERATED — do not edit. Run scripts/generate_ts_types.py to regenerate.
//
// Source of truth: the Python dataclasses in vibechek/. Field types come from
// dataclasses.fields() + typing.get_type_hints(); @property fields are listed
// explicitly in the generator. Re-run the script after touching any source
// dataclass.
"""

CONSTANTS_HEADER = """// AUTO-GENERATED — do not edit. Run scripts/generate_ts_types.py to regenerate.
//
// Source of truth: Python attributes listed in SHARED_CONSTANTS in
// scripts/generate_ts_types.py. Re-run the script after editing the Python
// values so the UI picks them up.
"""


def emit_shared_constants() -> str:
    """Build the contents of `keeperConstants.ts` from `SHARED_CONSTANTS`.

    Each entry imports the named module, reads the attribute, validates that
    the value is JSON-serializable, and emits a TS `export const` with the
    declared type annotation. Drift between Python and TS is impossible by
    construction — there is only one copy of the value.
    """
    blocks = [CONSTANTS_HEADER]
    for module_name, attr_name, ts_name, ts_type in SHARED_CONSTANTS:
        module = importlib.import_module(module_name)
        if not hasattr(module, attr_name):
            raise AttributeError(
                f"{module_name} has no attribute {attr_name!r} "
                f"(check SHARED_CONSTANTS in scripts/generate_ts_types.py)"
            )
        value = getattr(module, attr_name)
        try:
            # `sort_keys` keeps the output stable across runs so diffs stay clean.
            literal = json.dumps(value, indent=2, sort_keys=True)
        except TypeError as e:
            raise TypeError(
                f"{module_name}.{attr_name} is not JSON-serializable: {e}. "
                f"SHARED_CONSTANTS values must be plain dict/list/str/int/float/bool/None."
            ) from e
        blocks.append(
            f"export const {ts_name}: {ts_type} = {literal};"
        )
    return "\n\n".join(blocks) + "\n"


def main() -> int:
    # `--check` renders to memory and diffs against the committed files instead
    # of writing — CI runs this so a dataclass edit can't silently drift from
    # the committed generated.ts/keeperConstants.ts wire contract.
    check_only = "--check" in sys.argv[1:]

    classes = collect_dataclasses(all_dataclass_modules())
    known = {c.__name__ for c in classes} | EXTERNAL_TYPES

    blocks = [HEADER]
    stubs = emit_external_stubs()
    if stubs:
        blocks.append(stubs)
    for cls in classes:
        blocks.append(emit_interface(cls, known))
    output = "\n\n".join(blocks) + "\n"
    constants_output = emit_shared_constants()

    if _DEGRADED:
        print(
            "generate_ts_types: refusing to emit a degraded wire contract — "
            "these fields could not be typed:",
            file=sys.stderr,
        )
        for msg in _DEGRADED:
            print(f"  - {msg}", file=sys.stderr)
        print(
            "Annotate them with a type translate() understands, or teach "
            "translate() the new form.",
            file=sys.stderr,
        )
        return 1

    if check_only:
        stale: list[str] = []
        for path, rendered in (
            (OUTPUT_PATH, output),
            (CONSTANTS_OUTPUT_PATH, constants_output),
        ):
            committed = (
                path.read_text(encoding="utf-8") if path.exists() else ""
            )
            if committed != rendered:
                stale.append(str(path))
        if stale:
            print(
                "STALE generated TS types: "
                + ", ".join(stale)
                + "\nRe-run `python scripts/generate_ts_types.py` and commit "
                "the result (the Python dataclasses changed without a regen).",
                file=sys.stderr,
            )
            return 1
        print(f"OK: generated TS types are current ({len(classes)} dataclasses, "
              f"{len(SHARED_CONSTANTS)} constants)")
        return 0

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(output, encoding="utf-8")

    print(f"Wrote {OUTPUT_PATH} ({len(classes)} dataclasses, "
          f"{output.count(chr(10)) + 1} lines)")

    CONSTANTS_OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONSTANTS_OUTPUT_PATH.write_text(constants_output, encoding="utf-8")
    print(f"Wrote {CONSTANTS_OUTPUT_PATH} ({len(SHARED_CONSTANTS)} constants)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
