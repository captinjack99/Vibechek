"""Tests for cross-vendor GPU detection (vibechek.gpu_detect).

These mock every subprocess and `shutil.which` call so the tests stay
deterministic regardless of the host hardware.
"""

from __future__ import annotations

import json
import subprocess
from unittest.mock import patch

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
# Windows: PowerShell Get-CimInstance path (preferred over wmic) + comma safety
# ---------------------------------------------------------------------------


# ConvertTo-Json emits a bare object for one controller, an array for several.
CIM_OUTPUT_SINGLE_AMD = json.dumps(
    {"Name": "AMD Radeon RX 7800 XT", "AdapterRAM": 17179869184}
)
CIM_OUTPUT_ARRAY = json.dumps([
    {"Name": "Intel(R) UHD Graphics 730", "AdapterRAM": 1073741824},
    {"Name": "AMD Radeon RX 7800 XT", "AdapterRAM": 17179869184},
])
# Real-world: some OEM adapter names contain a comma — the old CSV split
# (`line.split(",")`) desynced columns on these.
CIM_OUTPUT_COMMA_NAME = json.dumps(
    {"Name": "AMD Radeon RX 7800 XT, Gaming OC", "AdapterRAM": 17179869184}
)


def test_windows_prefers_powershell_cim(monkeypatch: pytest.MonkeyPatch) -> None:
    """When PowerShell is available, CIM JSON is used (not wmic)."""
    monkeypatch.setattr(gpu_detect.platform, "system", lambda: "Windows")
    monkeypatch.setattr(gpu_detect.shutil, "which", _which_only("powershell"))
    monkeypatch.setattr(
        gpu_detect.subprocess, "run",
        lambda *a, **kw: _fake_completed(CIM_OUTPUT_SINGLE_AMD),
    )
    devices = gpu_detect._detect_amd()
    assert len(devices) == 1
    d = devices[0]
    assert d.vendor == "amd"
    assert d.name == "AMD Radeon RX 7800 XT"
    assert d.vram_mb == 16384


def test_cim_handles_array_and_vendor_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    """An array of controllers is filtered by vendor."""
    monkeypatch.setattr(gpu_detect.shutil, "which", _which_only("powershell"))
    monkeypatch.setattr(
        gpu_detect.subprocess, "run",
        lambda *a, **kw: _fake_completed(CIM_OUTPUT_ARRAY),
    )
    intel = gpu_detect._detect_gpus_cim("intel")
    assert intel is not None and len(intel) == 1
    assert intel[0].vendor == "intel"
    assert intel[0].device_kind == "integrated"
    amd = gpu_detect._detect_gpus_cim("amd")
    assert amd is not None and len(amd) == 1
    assert amd[0].name == "AMD Radeon RX 7800 XT"


def test_cim_comma_in_name_is_not_split(monkeypatch: pytest.MonkeyPatch) -> None:
    """The JSON path keeps a comma-containing adapter name intact."""
    monkeypatch.setattr(gpu_detect.shutil, "which", _which_only("powershell"))
    monkeypatch.setattr(
        gpu_detect.subprocess, "run",
        lambda *a, **kw: _fake_completed(CIM_OUTPUT_COMMA_NAME),
    )
    devices = gpu_detect._detect_gpus_cim("amd")
    assert devices is not None and len(devices) == 1
    assert devices[0].name == "AMD Radeon RX 7800 XT, Gaming OC"


def test_cim_returns_none_without_powershell(monkeypatch: pytest.MonkeyPatch) -> None:
    """No powershell/pwsh -> None so the caller falls back to wmic."""
    monkeypatch.setattr(gpu_detect.shutil, "which", lambda _: None)
    assert gpu_detect._detect_gpus_cim("amd") is None


def test_wmic_csv_fallback_is_comma_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    """When PowerShell is absent, the wmic CSV fallback uses the csv module so a
    quoted comma in the Name field no longer corrupts the column split."""
    monkeypatch.setattr(gpu_detect.platform, "system", lambda: "Windows")
    monkeypatch.setattr(gpu_detect.shutil, "which", _which_only("wmic"))
    wmic_csv = (
        "Node,AdapterRAM,Name\n"
        'DESKTOP,17179869184,"AMD Radeon RX 7800 XT, Gaming OC"\n'
    )
    monkeypatch.setattr(
        gpu_detect.subprocess, "run",
        lambda *a, **kw: _fake_completed(wmic_csv),
    )
    devices = gpu_detect._detect_amd()
    assert len(devices) == 1
    assert devices[0].name == "AMD Radeon RX 7800 XT, Gaming OC"
    assert devices[0].vram_mb == 16384


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
    monkeypatch.setattr(gpu_detect, "detect_all_gpus", lambda engine=None: [
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
    monkeypatch.setattr(gpu_detect, "detect_all_gpus", lambda engine=None: [])
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
    monkeypatch.setattr(gpu_detect, "detect_all_gpus", lambda engine=None: [
        DetectedGpu(vendor="amd", name="RX 7800 XT", vram_mb=16384,
                    accelerated_by_vibechek=False,
                    unsupported_reason="amd reason"),
    ])
    res = resources.detect()
    assert res.gpu_available is False
    assert res.accelerated_gpu_count == 0
    assert res.unsupported_gpu_count == 1


# ---------------------------------------------------------------------------
# Engine-aware acceleration verdict
#
# Enumeration is engine-independent, but WHICH cards are accelerated depends on
# the selected engine: essentia_tf (NVIDIA-CUDA only), onnx (NVIDIA-CUDA today;
# AMD/Intel DirectML + Apple CoreML EPs planned), native (CPU-only for EVERY
# vendor today, NVIDIA included). A no-engine call keeps the historical
# essentia_tf default.
# ---------------------------------------------------------------------------


def _patch_mixed_vendors(monkeypatch: pytest.MonkeyPatch) -> None:
    """One NVIDIA + one AMD + one Apple card via the per-vendor detectors, so
    the real `detect_all_gpus` verdict pass runs on top of them."""
    monkeypatch.setattr(gpu_detect, "_detect_nvidia", lambda: [
        DetectedGpu(vendor="nvidia", name="RTX 4070", vram_mb=8192),
    ])
    monkeypatch.setattr(gpu_detect, "_detect_amd", lambda: [
        DetectedGpu(vendor="amd", name="Radeon RX 7800 XT", vram_mb=16384),
    ])
    monkeypatch.setattr(gpu_detect, "_detect_intel", lambda: [])
    monkeypatch.setattr(gpu_detect, "_detect_apple", lambda: [
        DetectedGpu(vendor="apple", name="Apple M3 Max"),
    ])


def test_verdict_essentia_tf_is_the_no_engine_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_mixed_vendors(monkeypatch)
    by_vendor = {d.vendor: d for d in detect_all_gpus()}
    assert by_vendor["nvidia"].accelerated_by_vibechek is True
    assert by_vendor["nvidia"].unsupported_reason is None
    assert by_vendor["amd"].accelerated_by_vibechek is False
    assert "essentia-tensorflow" in by_vendor["amd"].unsupported_reason
    assert by_vendor["apple"].accelerated_by_vibechek is False
    # Explicit essentia_tf matches the no-engine default.
    assert detect_all_gpus(engine="essentia_tf")[0].accelerated_by_vibechek is True


def test_verdict_onnx_keeps_nvidia_but_reasons_name_the_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_mixed_vendors(monkeypatch)
    by_vendor = {d.vendor: d for d in detect_all_gpus(engine="onnx")}
    assert by_vendor["nvidia"].accelerated_by_vibechek is True
    assert by_vendor["nvidia"].unsupported_reason is None
    # The non-NVIDIA reasons name the ONNX execution provider, NOT TensorFlow.
    assert by_vendor["amd"].accelerated_by_vibechek is False
    assert "DirectML" in by_vendor["amd"].unsupported_reason
    assert "essentia-tensorflow" not in by_vendor["amd"].unsupported_reason
    assert "CoreML" in by_vendor["apple"].unsupported_reason


def test_verdict_native_accelerates_nothing_including_nvidia(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_mixed_vendors(monkeypatch)
    devices = detect_all_gpus(engine="native")
    # CPU-only for every vendor today — the NVIDIA card is NOT accelerated.
    assert all(d.accelerated_by_vibechek is False for d in devices)
    nvidia = next(d for d in devices if d.vendor == "nvidia")
    assert nvidia.unsupported_reason is not None
    assert "CPU today" in nvidia.unsupported_reason


def test_verdict_unknown_engine_falls_back_to_essentia_tf(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_mixed_vendors(monkeypatch)
    by_vendor = {d.vendor: d for d in detect_all_gpus(engine="bogus")}
    assert by_vendor["nvidia"].accelerated_by_vibechek is True
    assert "essentia-tensorflow" in by_vendor["amd"].unsupported_reason


def test_acceleration_verdict_matrix() -> None:
    """Every (vendor × engine) cell, asserted directly."""
    # NVIDIA: accelerated under essentia_tf/onnx, never under native.
    assert gpu_detect._acceleration_verdict("nvidia", "essentia_tf") == (True, None)
    assert gpu_detect._acceleration_verdict("nvidia", "onnx") == (True, None)
    assert gpu_detect._acceleration_verdict("nvidia", "native")[0] is False
    # Non-NVIDIA: never accelerated on any engine, always with a reason.
    for vendor in ("amd", "intel", "apple", "unknown"):
        for engine in ("essentia_tf", "onnx", "native"):
            accel, reason = gpu_detect._acceleration_verdict(vendor, engine)
            assert accel is False
            assert reason  # a non-empty explanatory string


def test_resources_detect_engine_aware_gpu_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end through resources.detect(engine=...): an NVIDIA box reports
    gpu_available under essentia_tf/onnx but NOT under the CPU-only native
    engine — the verdict must survive the resources→gpu_detect round trip."""
    from vibechek import resources
    monkeypatch.setattr(gpu_detect, "_detect_nvidia", lambda: [
        DetectedGpu(vendor="nvidia", name="RTX 4070", vram_mb=8192),
    ])
    monkeypatch.setattr(gpu_detect, "_detect_amd", lambda: [])
    monkeypatch.setattr(gpu_detect, "_detect_intel", lambda: [])
    monkeypatch.setattr(gpu_detect, "_detect_apple", lambda: [])

    tf = resources.detect("essentia_tf")
    assert tf.gpu_available is True
    assert tf.accelerated_gpu_count == 1

    onnx = resources.detect("onnx")
    assert onnx.gpu_available is True
    assert onnx.accelerated_gpu_count == 1

    nat = resources.detect("native")
    assert nat.gpu_available is False
    assert nat.accelerated_gpu_count == 0
    assert nat.unsupported_gpu_count == 1
    dev = nat.gpu_devices[0]
    assert dev.vendor == "nvidia"
    assert dev.accelerated_by_vibechek is False
    assert "CPU" in (dev.unsupported_reason or "")


def test_system_info_rpc_threads_selected_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The `system_info` RPC resolves the engine (live GUI param → saved config)
    and hands it to `resources.detect`, so the GPU inventory it returns
    describes the correct engine's acceleration story. A bad param is coerced,
    never passed through raw."""
    from vibechek import resources
    from vibechek import rpc as rpc_mod

    captured: dict = {}

    def fake_detect(engine=None):
        captured["engine"] = engine
        return resources.SystemResources(
            platform="test", cpu_count=4,
            memory_total_mb=None, memory_available_mb=None,
            gpu_available=False,
        )

    monkeypatch.setattr(resources, "detect", fake_detect)

    # The GUI's live selection wins.
    rpc_mod._system_info({"inference_engine": "native"})
    assert captured["engine"] == "native"

    # No param → the saved config's engine (always a valid engine string).
    captured.clear()
    rpc_mod._system_info({})
    assert captured["engine"] in ("essentia_tf", "onnx", "native")

    # A garbage param is coerced to a valid engine, never forwarded raw.
    captured.clear()
    rpc_mod._system_info({"inference_engine": "TOTALLY_BOGUS"})
    assert captured["engine"] in ("essentia_tf", "onnx", "native")
