#!/usr/bin/env bash
# Build the Vibechek CLI / sidecar binary on Linux.
#
# Prereqs:
#   - Python 3.10+
#   - For audio fingerprinting at runtime: libchromaprint-tools (fpcalc)
#       sudo apt install libchromaprint-tools
#
# Usage:
#   ./packaging/build-linux.sh
#
# Output:
#   dist/vibechek                  — single-file executable (PyInstaller --onefile)
#   dist/vibechek-linux-x64.tar.gz

set -euo pipefail

cd "$(dirname "$0")/.."

if [[ ! -d .venv ]]; then
    echo "Creating venv..."
    python3 -m venv .venv
fi

source .venv/bin/activate

echo "Installing build deps..."
python -m pip install --upgrade pip wheel
python -m pip install -e . pyinstaller

echo "Running PyInstaller..."
pyinstaller packaging/vibechek.spec --noconfirm --clean

echo "Smoke-test the built binary..."
dist/vibechek --version
dist/vibechek --help > /dev/null

ARCHIVE="dist/vibechek-linux-x64.tar.gz"
echo "Packaging ${ARCHIVE}..."
# Onefile output is a bare executable, not a directory. We still tar it for a
# consistent release artifact so the execute bit survives the download cycle.
tar -czf "${ARCHIVE}" -C dist vibechek

echo
echo "Build complete: dist/vibechek"
echo "Archive:        ${ARCHIVE}"
