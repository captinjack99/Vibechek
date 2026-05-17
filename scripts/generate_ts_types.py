"""Generate TypeScript interfaces from Vibechek's Python dataclasses.

Run with:
    ./.venv/Scripts/python.exe scripts/generate_ts_types.py

Writes to `ui/src/types/generated.ts`. The file is auto-overwritten; do not
hand-edit it. To extend the mapping, edit this script.

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
    X | None / Optional[X]       -> X | null
    Union[A, B, ...]             -> A | B | ...
    Any                          -> unknown
    Custom dataclass             -> the matching interface name
    Unknown                      -> unknown   (with a warning)

@property methods on dataclasses are emitted as readonly fields when they
appear in `PROPERTY_FIELDS` below — manually maintained because property
return types aren't preserved by `dataclasses.fields()`.
"""

from __future__ import annotations

import dataclasses
import importlib
import sys
import types
import typing
from pathlib import Path

# Make `vibechek` importable when run from the repo root.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Modules to walk for dataclasses, in emission order.
MODULES = [
    "vibechek.resources",
    "vibechek.config",
    "vibechek.wsl",
    "vibechek.preflight",
    "vibechek.analyzer",
    "vibechek.duplicates",
    "vibechek.organizer",
    "vibechek.library_state",
    "vibechek.backup_history",
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

OUTPUT_PATH = ROOT / "ui" / "src" / "types" / "generated.ts"


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


def translate(tp: object, known: set[str]) -> str:
    """Translate a Python type annotation to TS, given the set of dataclass names."""
    # Unwrap Optional first
    inner, optional = _unwrap_optional(tp)
    if optional:
        return f"{translate(inner, known)} | null"

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
        return f"{translate(inner_tp, known)}[]"

    if origin is dict:
        key_tp, val_tp = args if len(args) == 2 else (str, typing.Any)
        key_ts = translate(key_tp, known)
        # JSON / TS object keys must be strings.
        if key_ts != "string":
            key_ts = "string"
        return f"Record<string, {translate(val_tp, known)}>"

    if origin is typing.Union or origin is types.UnionType:
        parts = [translate(a, known) for a in args if a is not type(None)]
        if any(a is type(None) for a in args):
            parts.append("null")
        return " | ".join(parts)

    # Custom dataclass reference
    if _is_dataclass_type(tp):
        return tp.__name__

    # Bare classes — fall back to the class name if we know it, else unknown.
    if isinstance(tp, type):
        if tp.__name__ in known:
            return tp.__name__
        print(f"  warning: unknown type {tp!r} -> unknown", file=sys.stderr)
        return "unknown"

    # String forward refs (resolved by get_type_hints normally, but be safe)
    if isinstance(tp, str):
        return tp if tp in known else "unknown"

    print(f"  warning: unhandled annotation {tp!r} -> unknown", file=sys.stderr)
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
        print(f"  warning: could not resolve hints for {cls.__name__}: {e}", file=sys.stderr)
        hints = {}

    overrides: dict[str, str] = getattr(cls, "__ts_overrides__", {}) or {}

    lines = [f"export interface {cls.__name__} {{"]
    for f in dataclasses.fields(cls):
        if f.name in overrides:
            ts = overrides[f.name]
        else:
            tp = hints.get(f.name, f.type)
            ts = translate(tp, known)
        lines.append(f"  {f.name}: {ts};")

    for prop_name, ts in PROPERTY_FIELDS.get(cls.__name__, []):
        lines.append(f"  readonly {prop_name}: {ts};")

    lines.append("}")
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


def main() -> int:
    classes = collect_dataclasses(MODULES)
    known = {c.__name__ for c in classes}

    blocks = [HEADER]
    for cls in classes:
        blocks.append(emit_interface(cls, known))
    output = "\n\n".join(blocks) + "\n"

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(output, encoding="utf-8")

    print(f"Wrote {OUTPUT_PATH} ({len(classes)} dataclasses, "
          f"{output.count(chr(10)) + 1} lines)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
