"""Generate Vibechek's app icons in every format Tauri wants.

Run once (or when the brand design changes) to regenerate everything under
`ui/src-tauri/icons/`. The output files are checked into the repo — regular
contributors never need to run this.

Outputs:
    ui/src-tauri/icons/
        32x32.png
        128x128.png
        128x128@2x.png        (= 256x256)
        icon.png              (= 1024x1024 master)
        icon.ico              (multi-res: 16/24/32/48/64/128/256)
        icon.icns             (PNG-in-ICNS, 128 + 256 + 512 chunks)

Design:
    Rounded purple square (#a855f7) with a stylized vinyl record:
    outer ring, inner label ring, central spindle dot. Reads as a DJ icon
    at every size; abstract enough to not look dated.

Requires:
    pip install Pillow
"""

from __future__ import annotations

import io
import struct
import sys
from pathlib import Path

from PIL import Image, ImageDraw

# Brand colors (match tailwind.config.js)
PURPLE = (168, 85, 247, 255)   # #a855f7
WHITE  = (255, 255, 255, 255)
BG_TRANSPARENT = (0, 0, 0, 0)

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = REPO_ROOT / "ui" / "src-tauri" / "icons"


def draw_icon(size: int) -> Image.Image:
    """Render a Vibechek icon at the given pixel size."""
    img = Image.new("RGBA", (size, size), BG_TRANSPARENT)
    draw = ImageDraw.Draw(img)

    # Background — rounded purple square with margin (so it doesn't fill edge-to-edge)
    pad = max(1, size // 32)
    radius = size // 6
    draw.rounded_rectangle(
        [(pad, pad), (size - 1 - pad, size - 1 - pad)],
        radius=radius,
        fill=PURPLE,
    )

    # Outer ring of the vinyl record
    disc_margin = size // 4
    ring_width = max(2, size // 24)
    draw.ellipse(
        [(disc_margin, disc_margin), (size - disc_margin, size - disc_margin)],
        outline=WHITE,
        width=ring_width,
    )

    # Mid ring (gives the record more definition at small sizes)
    mid_margin = disc_margin + size // 14
    mid_width = max(1, size // 64)
    draw.ellipse(
        [(mid_margin, mid_margin), (size - mid_margin, size - mid_margin)],
        outline=WHITE,
        width=mid_width,
    )

    # Center label
    label_margin = size // 2 - size // 9
    draw.ellipse(
        [(label_margin, label_margin), (size - label_margin, size - label_margin)],
        fill=WHITE,
    )

    # Spindle hole
    spindle = max(1, size // 56)
    cx = cy = size // 2
    draw.ellipse(
        [(cx - spindle, cy - spindle), (cx + spindle, cy + spindle)],
        fill=PURPLE,
    )

    return img


def write_pngs(master: Image.Image) -> None:
    """Write the per-size PNGs Tauri lists explicitly."""
    sizes = {
        "32x32.png":      32,
        "128x128.png":    128,
        "128x128@2x.png": 256,
        "icon.png":       1024,
    }
    for name, size in sizes.items():
        out = OUT_DIR / name
        master.resize((size, size), Image.LANCZOS).save(out, optimize=True)
        print(f"  wrote {out.relative_to(REPO_ROOT)} ({size}x{size})")


def write_ico(master: Image.Image) -> None:
    """Write a multi-resolution Windows .ico file."""
    out = OUT_DIR / "icon.ico"
    # Each size becomes its own image inside the .ico
    sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    master.save(out, sizes=sizes, format="ICO")
    print(f"  wrote {out.relative_to(REPO_ROOT)} ({len(sizes)} sizes)")


def write_icns(master: Image.Image) -> None:
    """Hand-write a minimal Apple .icns containing PNG chunks.

    Pillow's ICNS writer is missing in many builds, so we write the binary
    format directly:

        Magic "icns" + total length, then chunks of:
            4-byte type code + 4-byte chunk length (incl. header) + chunk data

    We embed PNGs in the chunks Apple introduced specifically for that
    purpose (ic07/ic08/ic09 = 128/256/512 px).
    """
    chunk_codes = {
        128:  b"ic07",
        256:  b"ic08",
        512:  b"ic09",
        1024: b"ic10",
    }

    chunks: list[bytes] = []
    for size, code in chunk_codes.items():
        buf = io.BytesIO()
        master.resize((size, size), Image.LANCZOS).save(buf, format="PNG", optimize=True)
        png_bytes = buf.getvalue()
        chunk_len = 8 + len(png_bytes)  # 4 type + 4 length + data
        chunks.append(code + struct.pack(">I", chunk_len) + png_bytes)

    body = b"".join(chunks)
    total_len = 8 + len(body)
    icns = b"icns" + struct.pack(">I", total_len) + body

    out = OUT_DIR / "icon.icns"
    out.write_bytes(icns)
    print(f"  wrote {out.relative_to(REPO_ROOT)} "
          f"({len(chunk_codes)} sizes, {total_len} bytes)")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    print(f"Generating icons -> {OUT_DIR.relative_to(REPO_ROOT)}")

    # Build at 1024 first; downscale from there for max quality
    master = draw_icon(1024)

    write_pngs(master)
    write_ico(master)
    write_icns(master)

    print("Done.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
