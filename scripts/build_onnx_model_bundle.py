#!/usr/bin/env python3
"""Assemble + hash the converted ONNX model bundle for the models-onnx mirror.

Copies the converted classification heads (``<stem>.onnx`` + ``<stem>.json``)
and (optionally) the EffNet backbone ``.onnx`` from a source model dir into a
flat bundle dir, computes SHA256 for each file, writes ``SHA256SUMS.txt`` + a
``MODEL_SHA256_ONNX`` Python snippet to paste into ``vibechek/analyzer.py``, and
prints the ``gh release`` command to publish the bundle.

ONE-TIME / release step. It only reads files — no essentia/onnxruntime needed.
Produce the heads first with ``scripts/convert_heads_to_onnx.py``.

Usage:
    python scripts/build_onnx_model_bundle.py --src ~/onnx_val/models --out ~/onnx_val/bundle
"""
from __future__ import annotations

import argparse
import hashlib
import shutil
from pathlib import Path

# Match vibechek/analyzer.py:_ONNX_HEAD_STEMS and onnx_backend._HEAD_SPECS.
HEAD_STEMS = (
    "genre_discogs400",
    "danceability",
    "voice_instrumental",
    "mood_aggressive",
    "mood_happy",
    "mood_relaxed",
    "mood_sad",
)
BACKBONE = "discogs-effnet-bsdynamic-1.onnx"
RELEASE_TAG = "models-onnx-v1"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", type=Path, default=Path.home() / "onnx_val" / "models")
    ap.add_argument("--out", type=Path, default=Path.home() / "onnx_val" / "bundle")
    ap.add_argument("--with-backbone", action="store_true",
                    help="Also bundle the EffNet backbone .onnx (else rely on the upstream mirror).")
    args = ap.parse_args()
    src, out = args.src.expanduser(), args.out.expanduser()
    out.mkdir(parents=True, exist_ok=True)

    files: list[Path] = []
    missing: list[str] = []
    for stem in HEAD_STEMS:
        for suffix in ("onnx", "json"):
            f = src / f"{stem}.{suffix}"
            if f.exists():
                files.append(f)
            elif suffix == "onnx" and stem != "genre_discogs400":
                missing.append(f.name)  # required head .onnx
    if args.with_backbone and (src / BACKBONE).exists():
        files.append(src / BACKBONE)

    if missing:
        print("WARNING: required head files missing from src:\n  " + "\n  ".join(missing))

    hashes: dict[str, str] = {}
    for f in files:
        dest = out / f.name
        shutil.copy2(f, dest)
        hashes[f.name] = sha256(dest)

    (out / "SHA256SUMS.txt").write_text(
        "".join(f"{h}  {name}\n" for name, h in sorted(hashes.items())),
        encoding="utf-8",
    )

    # Snippet for analyzer.MODEL_SHA256_ONNX — heads only (the backbone is
    # size-checked like the .pb backbone, not pinned, since it comes from the
    # upstream essentia mirror too).
    head_hashes = {k: v for k, v in hashes.items() if not k.startswith("discogs-effnet")}
    snippet = "MODEL_SHA256_ONNX: dict[str, str] = {\n" + "".join(
        f'    "{k}": "{v}",\n' for k, v in sorted(head_hashes.items())
    ) + "}\n"
    (out / "MODEL_SHA256_ONNX.py").write_text(snippet, encoding="utf-8")

    print(f"\nBundle: {out}  ({len(files)} files)\n")
    print(snippet)
    file_list = " ".join(f'"{out}/{name}"' for name in sorted(hashes))
    print("Publish (run as the repo owner, authenticated as papapew):")
    print(f"  gh release create {RELEASE_TAG} {file_list} \\")
    print('    --title "ONNX models v1" \\')
    print('    --notes "Converted Discogs-EffNet classification heads (.onnx + .json) for the '
          'TF-free ONNX inference engine. See docs/ONNX_MIGRATION.md." \\')
    print("    -R papapew/Vibechek")
    print(f"\n  # already exists?  gh release upload {RELEASE_TAG} {file_list} -R papapew/Vibechek --clobber")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
