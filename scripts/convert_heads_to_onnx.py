#!/usr/bin/env python3
"""Convert the Discogs-EffNet classification heads (.pb → .onnx) via tf2onnx.

ONE-TIME DEV/SETUP STEP. The EffNet *backbone* ships an official ONNX export
(`discogs-effnet-bsdynamic-1.onnx`), but the classification HEADS
(`genre_discogs400`, `danceability`, `voice_instrumental`, `mood_*`) do NOT —
essentia.upf.edu only hosts their TensorFlow `.pb` graphs. They are tiny dense
graphs (1280-d embedding → dense → sigmoid/softmax), so a one-off `tf2onnx`
conversion is trivial and faithful. The converted `.onnx` heads get hosted on
the Vibechek release mirror later; for now this script produces them locally so
a developer can run the ONNX backend end-to-end.

This script is NOT shipped in the wheel and is NOT part of the unit suite — it
needs `tensorflow` + `tf2onnx` installed (run it in the same WSL env that does
analysis). See docs/ONNX_MIGRATION.md §4.

Each head's input/output tensor node names are read from its `.json` `schema`
(the same names essentia uses), so the conversion needs no hardcoded names.

Idempotent: an existing `<head>.onnx` is skipped unless `--force` is given.

Usage:
    python scripts/convert_heads_to_onnx.py [--models DIR] [--force]

`--models` defaults to `~/onnx_val/models` (the validation layout used by
scripts/onnx_parity.py). Download the head `.pb` + `.json` files first from
https://essentia.upf.edu/models/classification-heads/<name>/ :

    genre_discogs400, danceability, voice_instrumental,
    mood_aggressive, mood_happy, mood_relaxed, mood_sad
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

# (pb stem, json stem) — the upstream essentia filenames. The metadata stem
# matches the weights stem for every head, so one constant covers both.
HEADS: tuple[str, ...] = (
    "genre_discogs400-discogs-effnet-1",
    "danceability-discogs-effnet-1",
    "voice_instrumental-discogs-effnet-1",
    "mood_aggressive-discogs-effnet-1",
    "mood_happy-discogs-effnet-1",
    "mood_relaxed-discogs-effnet-1",
    "mood_sad-discogs-effnet-1",
)

# Map an upstream stem to the short .onnx name the runtime backend expects
# (vibechek/onnx_backend.py:_HEAD_SPECS uses these short stems).
SHORT_NAME = {
    "genre_discogs400-discogs-effnet-1": "genre_discogs400",
    "danceability-discogs-effnet-1": "danceability",
    "voice_instrumental-discogs-effnet-1": "voice_instrumental",
    "mood_aggressive-discogs-effnet-1": "mood_aggressive",
    "mood_happy-discogs-effnet-1": "mood_happy",
    "mood_relaxed-discogs-effnet-1": "mood_relaxed",
    "mood_sad-discogs-effnet-1": "mood_sad",
}


def _node_names(metadata_path: Path) -> tuple[str, str]:
    """Read (input_name, output_name) from a head's .json schema."""
    meta = json.loads(metadata_path.read_text(encoding="utf-8"))
    schema = meta["schema"]
    in_name = schema["inputs"][0]["name"]
    out_name = schema["outputs"][0]["name"]
    return in_name, out_name


def _classes(metadata_path: Path) -> list[str]:
    try:
        return json.loads(metadata_path.read_text(encoding="utf-8")).get("classes", [])
    except Exception:  # noqa: BLE001
        return []


def convert_one(pb_path: Path, json_path: Path, out_path: Path, *, force: bool) -> bool:
    """Convert a single head .pb → .onnx. Returns True if a conversion ran."""
    if out_path.exists() and not force:
        print(f"  skip   {out_path.name} (exists; use --force to overwrite)")
        return False

    in_name, out_name = _node_names(json_path)
    # tf2onnx wants the tensor name with a port suffix (":0") for a frozen graph.
    in_tensor = in_name if ":" in in_name else f"{in_name}:0"
    out_tensor = out_name if ":" in out_name else f"{out_name}:0"

    cmd = [
        sys.executable, "-m", "tf2onnx.convert",
        "--graphdef", str(pb_path),
        "--output", str(out_path),
        "--inputs", in_tensor,
        "--outputs", out_tensor,
        "--opset", "13",
    ]
    print(f"  convert {pb_path.name} -> {out_path.name}")
    print(f"          inputs={in_tensor} outputs={out_tensor}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(result.stdout)
        print(result.stderr, file=sys.stderr)
        raise RuntimeError(f"tf2onnx failed for {pb_path.name} (exit {result.returncode})")

    # Sidecar the metadata next to the .onnx under the SHORT name the runtime
    # backend looks for (it reads <short>.json for class labels).
    short = out_path.stem
    short_json = out_path.with_name(f"{short}.json")
    if not short_json.exists():
        short_json.write_text(json.dumps({"classes": _classes(json_path)}), encoding="utf-8")
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--models", default=str(Path.home() / "onnx_val" / "models"))
    ap.add_argument("--force", action="store_true", help="overwrite existing .onnx")
    args = ap.parse_args()

    model_dir = Path(args.models)
    if not model_dir.is_dir():
        print(f"error: model dir not found: {model_dir}", file=sys.stderr)
        return 2

    # Verify tf2onnx is importable before doing anything (clearer error).
    try:
        import tf2onnx  # noqa: F401
    except ImportError:
        print(
            "error: tf2onnx is not installed. Install it in the analysis env:\n"
            "    pip install tf2onnx tensorflow\n"
            "(dev/setup only — not a Vibechek runtime dependency).",
            file=sys.stderr,
        )
        return 2

    converted = 0
    missing: list[str] = []
    for stem in HEADS:
        pb_path = model_dir / f"{stem}.pb"
        json_path = model_dir / f"{stem}.json"
        short = SHORT_NAME[stem]
        out_path = model_dir / f"{short}.onnx"

        if not pb_path.exists() or not json_path.exists():
            missing.append(stem)
            print(f"  MISSING {stem}.pb/.json — download from "
                  f"essentia.upf.edu/models/classification-heads/")
            continue

        if convert_one(pb_path, json_path, out_path, force=args.force):
            converted += 1

    print(f"\nDone: {converted} converted, {len(HEADS) - len(missing)} present, "
          f"{len(missing)} missing.")
    if missing:
        print("Missing head .pb files (download them, then re-run):")
        for m in missing:
            print(f"  - {m}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
