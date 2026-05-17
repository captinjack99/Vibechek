#!/usr/bin/env bash
# Build the Vibechek CLI on macOS.
#
# Prereqs:
#   - Python 3.10+ (system python3 or installed via brew)
#   - Xcode command-line tools (xcode-select --install)
#
# Usage:
#   ./packaging/build-macos.sh
#
# Output:
#   dist/vibechek/                  — one-folder bundle
#   dist/vibechek-macos-<arch>.tar.gz

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
dist/vibechek/vibechek --version
dist/vibechek/vibechek --help > /dev/null

ARCH=$(uname -m)
ARCHIVE="dist/vibechek-macos-${ARCH}.tar.gz"
echo "Packaging ${ARCHIVE}..."
tar -czf "${ARCHIVE}" -C dist vibechek

echo
echo "Build complete: dist/vibechek/vibechek"
echo "Archive:        ${ARCHIVE}"
