#!/usr/bin/env bash
# Stage the PyInstaller-built sidecar where Tauri's externalBin AND `cargo
# run` / `npm run tauri dev` can both find it. Linux + macOS counterpart to
# stage-sidecar.bat.
#
# We use PyInstaller --onefile, so this is a single-file copy. Tauri's
# `externalBin` is a single-file contract; the platform-triple suffix is read
# from `rustc -vV` so we don't hard-code aarch64-apple-darwin etc.
#
# We mirror the copy into target/<profile>/ for dev mode, where Cargo just
# executes whatever sits next to the Tauri exe. `cargo clean` will wipe these
# copies; re-running this script restores everything.
#
# Run this AFTER ./packaging/build-{macos,linux}.sh produces dist/vibechek.
#
# Usage:
#   ./packaging/stage-sidecar.sh

set -euo pipefail

cd "$(dirname "$0")/.."

SOURCE="dist/vibechek"
if [[ ! -f "$SOURCE" ]]; then
    echo "Error: $SOURCE not found." >&2
    echo "Run ./packaging/build-macos.sh or build-linux.sh first." >&2
    exit 1
fi

# Detect the host's target triple. Tauri uses this same value to look up the
# externalBin file at build time, so a mismatch here = silent missing-sidecar
# error from Tauri.
TRIPLE=$(rustc -vV 2>/dev/null | awk '/^host:/ {print $2}')
if [[ -z "$TRIPLE" ]]; then
    echo "Error: rustc not on PATH (needed to detect target triple)." >&2
    echo "Install Rust via https://rustup.rs/." >&2
    exit 1
fi

DEST_BIN="ui/src-tauri/binaries/vibechek-sidecar-${TRIPLE}"
mkdir -p "ui/src-tauri/binaries"

cp -f "$SOURCE" "$DEST_BIN"
chmod +x "$DEST_BIN"
echo "Staged $SOURCE -> $DEST_BIN"

# Stage into each Cargo profile dir that already exists. We pick the
# unsuffixed name `vibechek-sidecar` because Cargo + Tauri dev mode place the
# binary next to the desktop exe without the platform triple.
for profile in debug release; do
    profile_dir="ui/src-tauri/target/${profile}"
    if [[ -d "$profile_dir" ]]; then
        cp -f "$SOURCE" "${profile_dir}/vibechek-sidecar"
        chmod +x "${profile_dir}/vibechek-sidecar"
        echo "Staged $SOURCE -> ${profile_dir}/vibechek-sidecar"
    fi
done

echo
echo "Sidecar staged. \`npm run tauri dev\` and \`cargo run\` should now find a working sidecar."
