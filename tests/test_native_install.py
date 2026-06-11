"""Tests for vibechek.native_install — the managed-venv disk probe.

`probe_native_venv` is pure disk inspection, so we build venv skeletons under
tmp_path and point the module at them. The Unix-layout cases are the
regression lock for the `lib/python3.*` glob bug: `Path.glob()` only expands
wildcards in the pattern argument, so the old code — which put the wildcard in
the *parent* path — never matched the Unix site-packages layout, and
Linux/macOS always reported essentia as not installed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from vibechek import native_install


def _make_fake_venv(root: Path, layout: str, dist_info: str | None) -> Path:
    """Build a minimal venv skeleton: a python binary + one dist-info dir."""
    vd = root / "venv"
    if layout == "unix":
        (vd / "bin").mkdir(parents=True)
        (vd / "bin" / "python3").write_text("#!/bin/sh\n")
        sp = vd / "lib" / "python3.12" / "site-packages"
    else:  # windows
        (vd / "Scripts").mkdir(parents=True)
        (vd / "Scripts" / "python.exe").write_text("")
        sp = vd / "Lib" / "site-packages"
    sp.mkdir(parents=True)
    if dist_info is not None:
        (sp / dist_info).mkdir()
    return vd


@pytest.mark.parametrize(
    ("layout", "dist_info", "version"),
    [
        # essentia-tensorflow in the default venv — the layout the desktop app
        # actually creates on Linux/macOS (and the one the old glob missed).
        ("unix", "essentia_tensorflow-2.1b6.dev1110.dist-info", "2.1b6.dev1110"),
        # plain essentia, as installed into the ONNX venv
        ("unix", "essentia-2.1b6.dev1110.dist-info", "2.1b6.dev1110"),
        # Windows venv layout (kept symmetric for tests/future-proofing)
        ("windows", "essentia_tensorflow-2.1b6.dev1110.dist-info", "2.1b6.dev1110"),
    ],
)
def test_probe_detects_essentia_in_site_packages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    layout: str,
    dist_info: str,
    version: str,
) -> None:
    vd = _make_fake_venv(tmp_path, layout, dist_info)
    monkeypatch.setattr(native_install, "IS_SUPPORTED", True)
    monkeypatch.setattr(native_install, "VENV_DIR", vd)

    status = native_install.probe_native_venv()

    assert status.essentia_installed is True
    assert status.essentia_version == version


def test_probe_reports_missing_essentia(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A venv with packages but no essentia dist-info reads as not installed."""
    vd = _make_fake_venv(tmp_path, "unix", "click-8.1.7.dist-info")
    monkeypatch.setattr(native_install, "IS_SUPPORTED", True)
    monkeypatch.setattr(native_install, "VENV_DIR", vd)

    status = native_install.probe_native_venv()

    assert status.essentia_installed is False
    assert status.essentia_version is None


def test_probe_handles_absent_venv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No venv on disk → clean 'nothing installed' status, no exception."""
    monkeypatch.setattr(native_install, "IS_SUPPORTED", True)
    monkeypatch.setattr(native_install, "VENV_DIR", tmp_path / "missing-venv")

    status = native_install.probe_native_venv()

    assert status.venv_python is None
    assert status.essentia_installed is False
