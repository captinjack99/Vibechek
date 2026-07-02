"""Gold-corpus accuracy gate.

CI proves the engines *run*; this proves they still produce the RIGHT answers.
`tests/fixtures/gold/` carries three openly-licensed 45s excerpts (see
ATTRIBUTION.md there) plus `manifest.json` pinning the genre/BPM/key/energy the
production pipeline read for them — identical on the essentia_tf and native
engines when pinned. A result regression (model staging bug, DSP drift, a
broken mel frontend, resampler change…) fails the gate even though nothing
crashed.

Two layers so every harness shares one implementation:

- :func:`check_gold_report` — pure: analysis report dict + manifest dict → list
  of human-readable failures. Unit-testable without audio or models.
- :func:`run_gold_gate` — runs the real `analyze_directory` over the fixtures
  dir (in-process, ``workers=1`` — no multiprocessing, so it is safe inside the
  frozen onefile exe) and checks the result.

Used by `vibechek selftest-native --gold-dir` (release build gate),
`scripts/rpc_smoke.py` (native-smoke full tier), and `tests/test_gold_gate.py`.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

log = logging.getLogger("vibechek.gold_gate")

MANIFEST_NAME = "manifest.json"


def load_manifest(fixtures_dir: Path) -> dict[str, Any]:
    """Read + minimally validate the gold manifest. Raises on a broken one —
    a malformed manifest must fail the gate, never skip it."""
    path = Path(fixtures_dir) / MANIFEST_NAME
    manifest = json.loads(path.read_text(encoding="utf-8"))
    tracks = manifest.get("tracks")
    if not isinstance(tracks, dict) or not tracks:
        raise ValueError(f"gold manifest has no tracks: {path}")
    for fname, spec in tracks.items():
        if not isinstance(spec, dict):
            raise ValueError(f"gold manifest entry for {fname!r} is not an object")
    return manifest


def check_gold_report(
    report: dict[str, Any], manifest: dict[str, Any]
) -> list[str]:
    """Compare an analysis report against the manifest. Returns failures
    (empty list == gate passes). Pure — no I/O."""
    failures: list[str] = []
    records = {
        t.get("filename"): t for t in (report.get("tracks") or []) if t.get("filename")
    }

    for fname, spec in manifest["tracks"].items():
        rec = records.get(fname)
        if rec is None:
            failures.append(f"{fname}: not analyzed (missing from the report)")
            continue
        if rec.get("error"):
            failures.append(f"{fname}: track error: {rec['error']}")
            continue
        ml = rec.get("ml_analysis") or {}
        if ml.get("ml_error"):
            failures.append(f"{fname}: ml_error: {ml['ml_error']}")
            continue

        for field, expected in (spec.get("expect") or {}).items():
            got = ml.get(field)
            if got != expected:
                failures.append(f"{fname}: {field} = {got!r}, expected {expected!r}")

        for field, rule in (spec.get("close") or {}).items():
            got = ml.get(field)
            want, tol = rule["value"], rule["tol"]
            if not isinstance(got, (int, float)):
                failures.append(
                    f"{fname}: {field} = {got!r}, expected ~{want} (±{tol})"
                )
            elif abs(float(got) - float(want)) > float(tol):
                failures.append(
                    f"{fname}: {field} = {got}, expected {want} ±{tol}"
                )

        for field, floor in (spec.get("min") or {}).items():
            got = ml.get(field)
            if not isinstance(got, (int, float)) or float(got) < float(floor):
                failures.append(f"{fname}: {field} = {got!r}, expected >= {floor}")

        for field in spec.get("present") or []:
            got = ml.get(field)
            if got is None or (isinstance(got, str) and not got.strip()):
                failures.append(f"{fname}: {field} missing/empty")

    return failures


def run_gold_gate(
    fixtures_dir: Path,
    engine: str = "",
    models_dir: Path | None = None,
) -> list[str]:
    """Analyze the gold fixtures through the production pipeline and check them.

    Runs in-process with a single worker: three 45s clips take well under a
    minute, and avoiding the multiprocessing pool keeps this callable inside
    the frozen onefile exe (spawn re-execution) and other constrained hosts.
    Returns the failure list; raises only on setup problems (missing manifest,
    unloadable models) — those must fail the calling gate loudly too.
    """
    from vibechek.analyzer import analyze_directory  # noqa: PLC0415 — heavy import
    from vibechek.config import AnalysisConfig  # noqa: PLC0415

    fixtures_dir = Path(fixtures_dir)
    manifest = load_manifest(fixtures_dir)

    kwargs: dict[str, Any] = {"workers": 1}
    if engine:
        kwargs["inference_engine"] = engine
    if models_dir is not None:
        kwargs["models_dir"] = models_dir
    report = analyze_directory(fixtures_dir, config=AnalysisConfig(**kwargs))
    return check_gold_report(report, manifest)
