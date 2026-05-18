"""Tests for cross-vendor GPU detection (vibechek.gpu_detect).

These mock every subprocess and `shutil.which` call so the tests stay
deterministic regardless of the host hardware.
"""

from __future__ import annotations

import json
import subprocess
from unittest.mock import patch, MagicMock

import pytest

from vibechek import gpu_detect
from vibechek.gpu_detect import DetectedGpu, detect_all_gpus


# ---------------------------------------------------------------------------
# Sample fixtures — real-world output snippets from each tool.
# ---------------------------------------------------------------------------


NVIDIA_SMI_OUTPUT = "NVIDIA GeForce RTX 4070, 8192\n"

ROCM_SMI_OUTPUT = json.dumps({
    "card0": {
        "GPU ID": "0x73bf",
        "Card series": "Radeon RX 7800 XT",
        "Card model": "0x73bf",
        "VRAM Total Memory (B)": str(16 * 1024 * 1024 * 1024),  # 16 GB
    }
})

LSPCI_OUTPUT_AMD = (
    "00:00.0 Host bridge: Advanced Micro Devices, Inc. [AMD] Device 14d8\n"
    "03:00.0 VGA compatible controller: Advanced Micro Devices, Inc. "
    "[AMD/ATI] Navi 32 [Radeon RX 7800 XT] (rev cf)\n"
    "03:00.1 Audio device: Advanced Micro Devices, Inc. [AMD/ATI] Navi 31 HDMI/DP Audio\n"
)

LSPCI_OUTPUT_INTEL = (
    "00:02.0 VGA compatible controller: Intel Corporation Alder Lake-S "
    "GT1 [UHD Graphics 730] (rev 0c)\n"
)

LSPCI_OUTPUT_INTEL_ARC = (
    "03:00.0 VGA compatible controller: Intel Corporation DG2 [Arc A770] (rev 08)\n"
)

LSPCI_OUTPUT_AMD_APU = (
    "08:00.0 VGA compatible controller: Advanced Micro Devices, Inc. "
    "[AMD/ATI] Cezanne [Radeon Graphics] (rev c9)\n"
)

# wmic CSV header is "Node,AdapterRAM,Name"
WMIC_OUTPUT_AMD = (
    "Node,AdapterRAM,Name\n"
    "DESKTOP,17179869184,AMD Radeon RX 7800 XT\n"
)
WMIC_OUTPUT_INTEL = (
    "Node,AdapterRAM,Name\n"
    "DESKTOP,1073741824,Intel(R) UHD Graphics 730\n"
)

SYSTEM_PROFILER_OUTPUT_M1 = json.dumps({
    "SPDisplaysDataType": [{
        "_name": "Apple M1 Pro",
        "sppci_model": "Apple M1 Pro",
        "spdisplays_vendor": "sppci_vendor_Apple",
        "sppci_bus": "spdisplays_builtin",
    }]
})

SYSTEM_PROFILER_OUTPUT_EGPU = json.dumps({
    "SPDisplaysDataType": [{
        "_name": "Radeon RX 580",
        "sppci_model": "AMD Radeon RX 580",
        "spdisplays_vendor": "sppci_vendor_AMD",
        "spdisplays_vram": "8 GB",
        "sppci_bus": "PCIe",
    }]
})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_completed(stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


def _which_only(*tools: str):
    """Return a `shutil.which` replacement that only finds the given tools."""
    allowed = set(tools)
    return lambda name: f"/usr/bin/{name}" if name in allowed else None


# ---------------------------------------------------------------------------
# NVIDIA detection
# ---------------------------------------------------------------------------


def test_nvidia_detection_via_nvidia_smi() -> None:
    with patch("vibechek.resources.shutil.which", _which_only("nvidia-smi")), \
         patch("vibechek.resources.subprocess.run",
               return_value=_fake_completed(NVIDIA_SMI_OUTPUT)):
        # Bypass platform-specific lspci / wmic detectors.
        with patch.object(gpu_detect, "_detect_amd", return_value=[]), \
             patch.object(gpu_detect, "_detect_intel", return_value=[]), \
             patch.object(gpu_detect, "_detect_apple", return_value=[]):
            devices = detect_all_gpus()
    assert len(devices) == 1
    d = devices[0]
    assert d.vendor == "nvidia"
    assert d.name == "NVIDIA GeForce RTX 4070"
    assert d.vram_mb == 8192
    assert d.accelerated_by_vibechek is True
    assert d.unsupported_reason is None
    assert d.device_kind == "discrete"


# ---------------------------------------------------------------------------
# AMD detection
# ---------------------------------------------------------------------------


def test_amd_detection_via_rocm_smi(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gpu_detect.platform, "system", lambda: "Linux")
    monkeypatch.setattr(gpu_detect.shutil, "which", _which_only("rocm-smi"))
    monkeypatch.setattr(
        gpu_detect.subprocess, "run",
        lambda *a, **kw: _fake_completed(ROCM_SMI_OUTPUT),
    )
    devices = gpu_detect._detect_amd()
    assert len(devices) == 1
    d = devices[0]
    assert d.vendor == "amd"
    assert d.name == "Radeon RX 7800 XT"
    assert d.vram_mb == 16384
    assert d.accelerated_by_vibechek is False
    assert d.unsupported_reason is not None
    assert "essentia-tensorflow" in d.unsupported_reason
    assert "ONNX_MIGRATION" in d.unsupported_reason


def test_amd_detection_via_lspci(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gpu_detect.platform, "system", lambda: "Linux")
    monkeypatch.setattr(gpu_detect.shutil, "which", _which_only("lspci"))
    monkeypatch.setattr(
        gpu_detect.subprocess, "run",
        lambda *a, **kw: _fake_completed(LSPCI_OUTPUT_AMD),
    )
    devices = gpu_detect._detect_amd()
    # Only the VGA controller, not the audio device or host bridge
    assert len(devices) == 1
    d = devices[0]
    assert d.vendor == "amd"
    assert "Radeon RX 7800 XT" in d.name
    assert d.device_kind == "discrete"
    assert d.accelerated_by_vibechek is False


def test_amd_apu_classified_as_integrated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gpu_detect.platform, "system", lambda: "Linux")
    monkeypatch.setattr(gpu_detect.shutil, "which", _which_only("lspci"))
    monkeypatch.setattr(
        gpu_detect.subprocess, "run",
        lambda *a, **kw: _fake_completed(LSPCI_OUTPUT_AMD_APU),
    )
    devices = gpu_detect._detect_amd()
    assert len(devices) == 1
    assert devices[0].device_kind == "integrated"


def test_amd_detection_on_windows_via_wmic(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gpu_detect.platform, "system", lambda: "Windows")
    monkeypatch.setattr(gpu_detect.shutil, "which", _which_only("wmic"))
    monkeypatch.setattr(
        gpu_detect.subprocess, "run",
        lambda *a, **kw: _fake_completed(WMIC_OUTPUT_AMD),
    )
    devices = gpu_detect._detect_amd()
    assert len(devices) == 1
    d = devices[0]
    assert d.vendor == "amd"
    assert "Radeon" in d.name
    assert d.vram_mb == 16384  # 16 GB → 16384 MB
    assert d.accelerated_by_vibechek is False


def test_amd_detection_skips_when_no_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gpu_detect.platform, "system", lambda: "Linux")
    monkeypatch.setattr(gpu_detect.shutil, "which", lambda _: None)
    assert gpu_detect._detect_amd() == []


def test_amd_rocm_smi_handles_invalid_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gpu_detect.shutil, "which", _which_only("rocm-smi"))
    monkeypatch.setattr(
        gpu_detect.subprocess, "run",
        lambda *a, **kw: _fake_completed("not json"),
    )
    assert gpu_detect._detect_amd_rocm_smi() == []


def test_amd_lspci_handles_subprocess_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gpu_detect.shutil, "which", _which_only("lspci"))

    def _raise(*a, **kw):
        raise subprocess.TimeoutExpired(cmd="lspci", timeout=5)

    monkeypatch.setattr(gpu_detect.subprocess, "run", _raise)
    assert gpu_detect._detect_amd_lspci() == []


# ---------------------------------------------------------------------------
# Intel detection
# ---------------------------------------------------------------------------


def test_intel_igpu_classified_as_integrated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gpu_detect.platform, "system", lambda: "Linux")
    monkeypatch.setattr(gpu_detect.shutil, "which", _which_only("lspci"))
    monkeypatch.setattr(
        gpu_detect.subprocess, "run",
        lambda *a, **kw: _fake_completed(LSPCI_OUTPUT_INTEL),
    )
    devices = gpu_detect._detect_intel()
    assert len(devices) == 1
    d = devices[0]
    assert d.vendor == "intel"
    assert "UHD Graphics 730" in d.name
    assert d.device_kind == "integrated"
    assert d.accelerated_by_vibechek is False
    assert d.unsupported_reason is not None


def test_intel_arc_classified_as_discrete(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gpu_detect.platform, "system", lambda: "Linux")
    monkeypatch.setattr(gpu_detect.shutil, "which", _which_only("lspci"))
    monkeypatch.setattr(
        gpu_detect.subprocess, "run",
        lambda *a, **kw: _fake_completed(LSPCI_OUTPUT_INTEL_ARC),
    )
    devices = gpu_detect._detect_intel()
    assert len(devices) == 1
    assert devices[0].device_kind == "discrete"


def test_intel_detection_on_windows_via_wmic(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gpu_detect.platform, "system", lambda: "Windows")
    monkeypatch.setattr(gpu_detect.shutil, "which", _which_only("wmic"))
    monkeypatch.setattr(
        gpu_detect.subprocess, "run",
        lambda *a, **kw: _fake_completed(WMIC_OUTPUT_INTEL),
    )
    devices = gpu_detect._detect_intel()
    assert len(devices) == 1
    d = devices[0]
    assert d.vendor == "intel"
    assert d.device_kind == "integrated"
    assert d.vram_mb == 1024


# ---------------------------------------------------------------------------
# Apple detection
# ---------------------------------------------------------------------------


def test_apple_m1_detection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gpu_detect.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(gpu_detect.shutil, "which", _which_only("system_profiler"))
    monkeypatch.setattr(
        gpu_detect.subprocess, "run",
        lambda *a, **kw: _fake_completed(SYSTEM_PROFILER_OUTPUT_M1),
    )
    devices = gpu_detect._detect_apple()
    assert len(devices) == 1
    d = devices[0]
    assert d.vendor == "apple"
    assert "Apple M1 Pro" in d.name
    assert d.device_kind == "integrated"
    assert d.accelerated_by_vibechek is False
    assert d.unsupported_reason is not None
    # Unified memory: VRAM is unknown
    assert d.vram_mb is None


def test_apple_egpu_detection_amd(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gpu_detect.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(gpu_detect.shutil, "which", _which_only("system_profiler"))
    monkeypatch.setattr(
        gpu_detect.subprocess, "run",
        lambda *a, **kw: _fake_completed(SYSTEM_PROFILER_OUTPUT_EGPU),
    )
    devices = gpu_detect._detect_apple()
    assert len(devices) == 1
    d = devices[0]
    assert d.vendor == "amd"
    assert d.device_kind == "external"
    assert d.vram_mb == 8192
    assert d.accelerated_by_vibechek is False


def test_apple_detection_skipped_on_non_macos(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gpu_detect.platform, "system", lambda: "Linux")
    assert gpu_detect._detect_apple() == []


def test_macos_vram_parser_handles_units() -> None:
    assert gpu_detect._parse_macos_vram("8 GB") == 8192
    assert gpu_detect._parse_macos_vram("1536 MB") == 1536
    assert gpu_detect._parse_macos_vram("1 TB") == 1024 * 1024
    assert gpu_detect._parse_macos_vram(None) is None
    assert gpu_detect._parse_macos_vram("") is None
    assert gpu_detect._parse_macos_vram("garbage") is None


# ---------------------------------------------------------------------------
# Top-level detect_all_gpus dedup behavior
# ---------------------------------------------------------------------------


def test_detect_all_gpus_dedupes_same_card(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same NVIDIA card reported by both nvidia-smi and a hypothetical second
    source should appear once."""
    monkeypatch.setattr(gpu_detect, "_detect_nvidia", lambda: [
        DetectedGpu(vendor="nvidia", name="NVIDIA GeForce RTX 4070",
                    vram_mb=8192, accelerated_by_vibechek=True),
        DetectedGpu(vendor="nvidia", name="GeForce RTX 4070",
                    vram_mb=8192, accelerated_by_vibechek=True),
    ])
    monkeypatch.setattr(gpu_detect, "_detect_amd", lambda: [])
    monkeypatch.setattr(gpu_detect, "_detect_intel", lambda: [])
    monkeypatch.setattr(gpu_detect, "_detect_apple", lambda: [])
    devices = detect_all_gpus()
    assert len(devices) == 1  # deduped on normalized name


def test_detect_all_gpus_returns_empty_when_no_hardware(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gpu_detect, "_detect_nvidia", lambda: [])
    monkeypatch.setattr(gpu_detect, "_detect_amd", lambda: [])
    monkeypatch.setattr(gpu_detect, "_detect_intel", lambda: [])
    monkeypatch.setattr(gpu_detect, "_detect_apple", lambda: [])
    assert detect_all_gpus() == []


def test_detect_all_gpus_mixed_vendors(monkeypatch: pytest.MonkeyPatch) -> None:
    """NVIDIA + AMD + Intel coexist (e.g. laptop with iGPU + dGPU + eGPU)."""
    monkeypatch.setattr(gpu_detect, "_detect_nvidia", lambda: [
        DetectedGpu(vendor="nvidia", name="RTX 4070", vram_mb=8192,
                    accelerated_by_vibechek=True),
    ])
    monkeypatch.setattr(gpu_detect, "_detect_amd", lambda: [
        DetectedGpu(vendor="amd", name="Radeon RX 7800 XT", vram_mb=16384,
                    accelerated_by_vibechek=False,
                    unsupported_reason="amd reason"),
    ])
    monkeypatch.setattr(gpu_detect, "_detect_intel", lambda: [
        DetectedGpu(vendor="intel", name="UHD Graphics 730",
                    device_kind="integrated",
                    accelerated_by_vibechek=False,
                    unsupported_reason="intel reason"),
    ])
    monkeypatch.setattr(gpu_detect, "_detect_apple", lambda: [])

    devices = detect_all_gpus()
    assert len(devices) == 3
    vendors = [d.vendor for d in devices]
    # NVIDIA emitted first (so it wins dedup), then AMD, then Intel
    assert vendors == ["nvidia", "amd", "intel"]
    accelerated = [d for d in devices if d.accelerated_by_vibechek]
    assert len(accelerated) == 1
    assert accelerated[0].vendor == "nvidia"


# ---------------------------------------------------------------------------
# Resources integration: SystemResources counts populate
# ---------------------------------------------------------------------------


def test_system_resources_populates_accelerated_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    from vibechek import resources
    monkeypatch.setattr(gpu_detect, "detect_all_gpus", lambda: [
        DetectedGpu(vendor="nvidia", name="RTX 4070", vram_mb=8192,
                    accelerated_by_vibechek=True),
        DetectedGpu(vendor="amd", name="Radeon RX 7800 XT", vram_mb=16384,
                    accelerated_by_vibechek=False,
                    unsupported_reason="amd"),
        DetectedGpu(vendor="intel", name="UHD Graphics 730",
                    device_kind="integrated",
                    accelerated_by_vibechek=False,
                    unsupported_reason="intel"),
    ])
    res = resources.detect()
    assert res.accelerated_gpu_count == 1
    assert res.unsupported_gpu_count == 2
    assert res.gpu_available is True  # at least one accelerated
    assert len(res.gpu_devices) == 3
    # GpuDevice shape carries the cross-vendor fields
    nv = next(g for g in res.gpu_devices if g.vendor == "nvidia")
    assert nv.backend == "cuda"
    assert nv.accelerated_by_vibechek is True
    amd = next(g for g in res.gpu_devices if g.vendor == "amd")
    assert amd.backend == "rocm"
    assert amd.accelerated_by_vibechek is False
    assert amd.unsupported_reason is not None


def test_system_resources_no_gpu(monkeypatch: pytest.MonkeyPatch) -> None:
    from vibechek import resources
    monkeypatch.setattr(gpu_detect, "detect_all_gpus", lambda: [])
    res = resources.detect()
    assert res.accelerated_gpu_count == 0
    assert res.unsupported_gpu_count == 0
    assert res.gpu_available is False
    assert res.gpu_devices == []


def test_system_resources_only_unsupported_gpu_keeps_gpu_available_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Honesty check: an AMD-only system must report gpu_available=False
    because Vibechek can't actually accelerate on it."""
    from vibechek import resources
    monkeypatch.setattr(gpu_detect, "detect_all_gpus", lambda: [
        DetectedGpu(vendor="amd", name="RX 7800 XT", vram_mb=16384,
                    accelerated_by_vibechek=False,
                    unsupported_reason="amd reason"),
    ])
    res = resources.detect()
    assert res.gpu_available is False
    assert res.accelerated_gpu_count == 0
    assert res.unsupported_gpu_count == 1
