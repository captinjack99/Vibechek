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


# ---------------------------------------------------------------------------
# ML-stack install ceiling: the GPU wheel set is multi-GB and must get the
# 2 h wall-clock (live-verified: the 15 min ceiling killed a real CUDA-stack
# install mid-download on an ordinary connection). CPU sets keep 30 min.
# ---------------------------------------------------------------------------


def _run_install_capturing_ml_step(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    engine: str,
    has_nvidia: bool,
) -> tuple[list[str], int]:
    """Drive install_essentia_native with every subprocess stubbed; return the
    (package list, timeout) the ML-stack pip step was invoked with."""
    vd = _make_fake_venv(tmp_path, "unix", None)
    # engine="onnx" targets the sibling venv-onnx — give it a skeleton too so
    # the venv-create step is skipped for both engines.
    onnx_vd = vd.parent / "venv-onnx"
    (onnx_vd / "bin").mkdir(parents=True)
    (onnx_vd / "bin" / "python3").write_text("#!/bin/sh\n")
    monkeypatch.setattr(native_install, "IS_SUPPORTED", True)
    monkeypatch.setattr(native_install, "VENV_DIR", vd)
    monkeypatch.setattr(native_install, "_find_host_python", lambda: "/usr/bin/python3")
    monkeypatch.setattr(
        native_install.shutil, "which",
        lambda name: "/usr/bin/nvidia-smi" if (name == "nvidia-smi" and has_nvidia) else None,
    )
    monkeypatch.setattr(
        native_install, "_run_subprocess_cancellable",
        lambda args, timeout: (0, "stub-version", "", False),
    )

    captured: dict = {}

    def _fake_run_with_progress(args: list[str], on_progress, timeout: int):
        # The ML-stack step is the only `pip install` whose args carry an
        # essentia package (the pip/wheel upgrade and the vibechek step don't).
        if any(str(a).startswith("essentia") for a in args):
            captured["packages"] = args[args.index("install") + 1:]
            captured["timeout"] = timeout
        return 0, []

    monkeypatch.setattr(native_install, "_run_with_progress", _fake_run_with_progress)

    result = native_install.install_essentia_native(engine=engine)
    assert result.get("ok") is True, result
    return captured["packages"], captured["timeout"]


def test_ml_install_gpu_stack_gets_two_hour_ceiling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    packages, timeout = _run_install_capturing_ml_step(
        tmp_path, monkeypatch, engine="onnx", has_nvidia=True,
    )
    assert "onnxruntime-gpu" in packages
    assert any(p.startswith("nvidia-") for p in packages)
    assert timeout == 60 * 120


def test_ml_install_cpu_onnx_keeps_thirty_min_ceiling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    packages, timeout = _run_install_capturing_ml_step(
        tmp_path, monkeypatch, engine="onnx", has_nvidia=False,
    )
    assert "onnxruntime" in packages
    assert not any(p.startswith("nvidia-") for p in packages)
    assert timeout == 60 * 30


def test_ml_install_essentia_tf_keeps_thirty_min_ceiling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    packages, timeout = _run_install_capturing_ml_step(
        tmp_path, monkeypatch, engine="essentia_tf", has_nvidia=False,
    )
    assert "essentia-tensorflow" in packages
    assert timeout == 60 * 30
