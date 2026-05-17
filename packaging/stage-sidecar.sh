#!/usr/bin/env bash
# Stage the PyInstaller-built sidecar where Tauri's externalBin expects it.
#
# Tauri 2 validates externalBin paths even in dev. The naming convention is
# `<name>-<target-triple>` (no extension on macOS/Linux). The triple is read
# from `rustc -vV`.
#
# Usage:
#   ./packaging/stage-sidecar.sh

set -euo pipefail

cd "$(dirname "$0")/.."

SOURCE="dist/vibechek/vibechek"
[[ -f "$SOURCE" ]] || {
    echo "Error: $SOURCE not found." >&2
    echo "Run ./packaging/build-macos.sh or build-linux.sh first." >&2
    exit 1
}

# Detect the host's target triple
TRIPLE=$(rustc -vV 2>/dev/null | awk '/^host:/ {print $2}')
if [[ -z "$TRIPLE" ]]; then
    echo "Error: rustc not on PATH (needed to detect target triple)." >&2
    echo "Install Rust via https://rustup.rs/." >&2
    exit 1
fi

DEST="ui/src-tauri/binaries/vibechek-sidecar-${TRIPLE}"
mkdir -p "ui/src-tauri/binaries"

cp -f "$SOURCE" "$DEST"
chmod +x "$DEST"
echo "Staged $SOURCE -> $DEST"
