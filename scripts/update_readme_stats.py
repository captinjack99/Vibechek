"""Regenerate the stats line in README.md.

Run from the repo root:
    ./.venv/Scripts/python.exe scripts/update_readme_stats.py

CI calls this with `--check` to fail the build if the README is stale, which
keeps PRs honest. Without `--check` it rewrites the block in place.

The README must contain a marker block:
    <!-- STATS_LINE_START -->
    ... anything ...
    <!-- STATS_LINE_END -->

We replace the content between the markers with the current counts of:
  - pytest tests (collected via `pytest --collect-only -q`)
  - RPC methods (parsed from vibechek/rpc.py:METHODS dict at import time)
  - Python modules (count of *.py files under vibechek/)
"""

from __future__ import annotations

import argparse
import importlib.util
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
MARKER_START = "<!-- STATS_LINE_START -->"
MARKER_END = "<!-- STATS_LINE_END -->"


def count_tests() -> int:
    """Run pytest --collect-only to get the actual collected test count."""
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "tests/"],
        capture_output=True, text=True, cwd=ROOT,
    )
    # Last non-empty line is like "317 tests collected in 0.37s"
    for line in reversed(result.stdout.strip().splitlines()):
        m = re.search(r"(\d+)\s+tests?\s+collected", line)
        if m:
            return int(m.group(1))
    return 0


def count_rpc_methods() -> int:
    """Import vibechek.rpc and count METHODS dict entries."""
    sys.path.insert(0, str(ROOT))
    spec = importlib.util.spec_from_file_location("vibechek.rpc", ROOT / "vibechek" / "rpc.py")
    if spec is None or spec.loader is None:
        return 0
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return len(getattr(mod, "METHODS", {}))


def count_modules() -> int:
    """Count *.py files under vibechek/ (excluding __pycache__)."""
    return len([p for p in (ROOT / "vibechek").glob("*.py") if p.name != "__init__.py"]) + 1


def render_block(tests: int, rpcs: int, modules: int) -> str:
    return (
        f"{MARKER_START}\n"
        f"**{tests} Python tests** · **{rpcs} JSON-RPC methods** · "
        f"**{modules} Python modules** · auto-updated by "
        f"`scripts/update_readme_stats.py`\n"
        f"{MARKER_END}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit 1 if the README is stale (instead of rewriting).",
    )
    args = parser.parse_args()

    tests = count_tests()
    rpcs = count_rpc_methods()
    modules = count_modules()

    new_block = render_block(tests, rpcs, modules)
    text = README.read_text(encoding="utf-8")

    if MARKER_START not in text or MARKER_END not in text:
        print(
            f"ERROR: README.md missing marker block. Insert this between any "
            f"two paragraphs and re-run:\n\n{MARKER_START}\n... will be regenerated ...\n{MARKER_END}",
            file=sys.stderr,
        )
        return 2

    pattern = re.compile(
        re.escape(MARKER_START) + r".*?" + re.escape(MARKER_END),
        re.DOTALL,
    )
    updated = pattern.sub(new_block, text)

    if updated == text:
        print(f"README stats already current: {tests} tests · {rpcs} RPCs · {modules} modules")
        return 0

    if args.check:
        print(
            f"ERROR: README stats are stale.\n"
            f"  Current README block:  {pattern.search(text).group()!r}\n"
            f"  Expected:              {new_block!r}\n"
            f"Run: ./.venv/Scripts/python.exe scripts/update_readme_stats.py",
            file=sys.stderr,
        )
        return 1

    README.write_text(updated, encoding="utf-8")
    print(f"Updated README: {tests} tests · {rpcs} RPCs · {modules} modules")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
