"""Cross-vendor GPU/APU enumeration.

This module discovers every GPU the host exposes — NVIDIA, AMD, Intel, Apple —
regardless of whether Vibechek's analyze engine can actually accelerate
inference on it. The point is to give the UI an honest picture so we never
silently fall back to CPU after telling the user "GPU is available".

Reality check (as of the essentia-tensorflow vendored TF 2.5 build):

  - NVIDIA: CUDA-accelerated, works via the engine probe.
  - AMD: ROCm path doesn't ship in the bundled TF; user-installed AMD
    drivers can't be hooked. The card shows up here for transparency, but
    `accelerated_by_vibechek=False`.
  - Intel iGPU / Arc: same story — no oneDNN-GPU / DirectML path in our TF.
  - Apple Metal: TF-Metal exists but is a separate wheel; the essentia
    bundle doesn't link against it.

Every helper here returns `[]` on failure and never raises. Subprocess
timeouts are short (5s) because this runs synchronously on the Settings
page load. We never import any vendor SDK — pure CLI scraping + a couple of
filesystem checks.
"""

from __future__ import annotations

import csv
import json
import logging
import platform
import re
import shutil
import subprocess
from dataclasses import dataclass

log = logging.getLogger(__name__)


# The vendor literal is constrained so the TS side can switch on it without a
# wider string type. "unknown" is the fallback when a row from lspci/wmic
# names a device we can't classify.
GpuVendor = str  # Literal["nvidia", "amd", "intel", "apple", "unknown"]
GpuKind = str    # Literal["discrete", "integrated", "external"]


# User-facing copy for `unsupported_reason`. Centralized so the UI strings stay
# consistent and the migration doc reference points to a single place.
_REASON_AMD = (
    "AMD GPUs need ROCm/DirectML; essentia-tensorflow only supports CUDA today. "
    "See docs/ONNX_MIGRATION.md."
)
_REASON_INTEL = (
    "Intel GPUs need oneDNN/DirectML; essentia-tensorflow only supports CUDA today. "
    "See docs/ONNX_MIGRATION.md."
)
_REASON_APPLE = (
    "Apple GPUs need tensorflow-metal; essentia-tensorflow ships its own CUDA-only TF. "
    "See docs/ONNX_MIGRATION.md."
)
_REASON_UNKNOWN = (
    "Vibechek can only accelerate inference on NVIDIA CUDA GPUs today. "
    "See docs/ONNX_MIGRATION.md."
)


@dataclass
class DetectedGpu:
    """A single GPU/APU enumerated on the host.

    `accelerated_by_vibechek` is the honest answer to "will analyze be
    faster on this card?". Currently True only for NVIDIA cards.
    """
    vendor: GpuVendor                       # nvidia | amd | intel | apple | unknown
    name: str
    device_kind: GpuKind = "discrete"       # discrete | integrated | external
    vram_mb: int | None = None
    accelerated_by_vibechek: bool = False
    unsupported_reason: str | None = None


# ---------------------------------------------------------------------------
# Top-level enumeration
# ---------------------------------------------------------------------------


def detect_all_gpus() -> list[DetectedGpu]:
    """Return every GPU/APU we can discover, across vendors.

    Order: NVIDIA first (the only currently-accelerated path), then AMD,
    Intel, Apple. Within a vendor, dedupe by name (e.g. lspci + wmic may
    both report the same device on weird WSL setups).
    """
    devices: list[DetectedGpu] = []
    devices.extend(_detect_nvidia())
    devices.extend(_detect_amd())
    devices.extend(_detect_intel())
    devices.extend(_detect_apple())

    # Dedupe by (vendor, normalized name) — keep first occurrence (NVIDIA
    # probes come first so the authoritative nvidia-smi entry wins over a
    # generic wmic row).
    seen: set[tuple[str, str]] = set()
    deduped: list[DetectedGpu] = []
    for d in devices:
        key = (d.vendor, _normalize_name(d.name))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(d)
    return deduped


def _normalize_name(name: str) -> str:
    """Cheap normalization so 'NVIDIA GeForce RTX 4070' and 'GeForce RTX 4070'
    dedupe to the same key."""
    s = name.strip().lower()
    for prefix in ("nvidia ", "amd ", "ati ", "intel(r) ", "intel "):
        if s.startswith(prefix):
            s = s[len(prefix):]
    return s


# ---------------------------------------------------------------------------
# NVIDIA
# ---------------------------------------------------------------------------


def _detect_nvidia() -> list[DetectedGpu]:
    """Reuse the canonical nvidia-smi parser from `vibechek.resources`.

    We keep the parser there as the single source of truth for the CUDA path
    (apply_gpu_preference etc. all live alongside it). Here we just adapt
    the result to the cross-vendor shape.
    """
    try:
        from vibechek import resources  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return []

    out: list[DetectedGpu] = []
    for g in resources._gpu_devices_from_nvidia_smi():
        out.append(DetectedGpu(
            vendor="nvidia",
            name=g.name,
            device_kind="discrete",
            vram_mb=g.memory_mb,
            accelerated_by_vibechek=True,
            unsupported_reason=None,
        ))
    return out


# ---------------------------------------------------------------------------
# AMD
# ---------------------------------------------------------------------------


def _detect_amd() -> list[DetectedGpu]:
    """AMD on Linux (rocm-smi + lspci) and Windows (wmic)."""
    sysname = platform.system()
    out: list[DetectedGpu] = []

    if sysname == "Linux":
        out.extend(_detect_amd_rocm_smi())
        # lspci is the broad fallback — picks up cards without ROCm too.
        out.extend(_detect_amd_lspci())
    elif sysname == "Windows":
        out.extend(_detect_gpus_wmic(vendor_filter="amd"))
    # macOS AMD eGPUs surface through system_profiler in _detect_apple; treat
    # them there because the data shape is identical to Apple GPUs.

    return out


def _detect_amd_rocm_smi() -> list[DetectedGpu]:
    """Parse `rocm-smi --showid --showmeminfo vram --json`.

    The JSON shape is `{"card0": {"GPU ID": "...", "VRAM Total Memory (B)": "..."}, ...}`.
    `rocm-smi` is only present when the ROCm stack is installed; gracefully
    no-op otherwise.
    """
    rsmi = shutil.which("rocm-smi")
    if not rsmi:
        return []
    try:
        out = subprocess.run(
            [rsmi, "--showid", "--showmeminfo", "vram", "--json"],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        log.debug("rocm-smi probe failed: %s", e)
        return []
    if out.returncode != 0 or not out.stdout.strip():
        return []
    try:
        payload = json.loads(out.stdout)
    except json.JSONDecodeError:
        return []

    devices: list[DetectedGpu] = []
    for card_key, info in payload.items():
        if not isinstance(info, dict):
            continue
        name = (
            info.get("Card series")
            or info.get("Card model")
            or info.get("GPU ID")
            or card_key
        )
        # VRAM key spelling varies across rocm-smi versions; check all.
        vram_bytes_raw = (
            info.get("VRAM Total Memory (B)")
            or info.get("VRAM Total (B)")
            or info.get("vram_total_b")
        )
        vram_mb: int | None = None
        if vram_bytes_raw:
            try:
                vram_mb = int(vram_bytes_raw) // (1024 * 1024)
            except (TypeError, ValueError):
                vram_mb = None
        devices.append(DetectedGpu(
            vendor="amd",
            name=str(name),
            device_kind="discrete",
            vram_mb=vram_mb,
            accelerated_by_vibechek=False,
            unsupported_reason=_REASON_AMD,
        ))
    return devices


def _detect_amd_lspci() -> list[DetectedGpu]:
    """Scrape `lspci` for AMD/ATI VGA controllers.

    Matches lines like:
      03:00.0 VGA compatible controller: Advanced Micro Devices, Inc. [AMD/ATI] Navi 32 [Radeon RX 7800 XT] (rev cf)
    """
    lspci = shutil.which("lspci")
    if not lspci:
        return []
    try:
        out = subprocess.run(
            [lspci], capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if out.returncode != 0:
        return []

    devices: list[DetectedGpu] = []
    for line in out.stdout.splitlines():
        # Only VGA / 3D / Display controllers are GPUs (lspci also lists
        # audio devices on the GPU's PCI function).
        if not re.search(r"\b(VGA compatible controller|3D controller|Display controller)\b", line):
            continue
        if not re.search(r"(Advanced Micro Devices|AMD|ATI)", line, flags=re.IGNORECASE):
            continue
        # Extract the bracketed marketing name when present:
        #   "... [AMD/ATI] Navi 32 [Radeon RX 7800 XT] (rev cf)"
        m = re.search(r"\[([^\[\]]+)\]\s*(?:\(rev\s+[^\)]+\))?\s*$", line)
        if m:
            name = f"AMD {m.group(1).strip()}"
        else:
            # Fall back to the substring after the controller-type colon.
            after_colon = line.split(":", 2)[-1].strip()
            name = after_colon or "AMD GPU"

        # APUs (integrated graphics on Ryzen) typically have "Radeon Graphics"
        # or "Vega" in the iGPU name; not perfect but good enough for the UI.
        kind = "integrated" if re.search(
            r"(Radeon Graphics|Vega \d+|RX Vega \d+ Mobile)", name, flags=re.IGNORECASE,
        ) else "discrete"

        devices.append(DetectedGpu(
            vendor="amd",
            name=name,
            device_kind=kind,
            vram_mb=None,
            accelerated_by_vibechek=False,
            unsupported_reason=_REASON_AMD,
        ))
    return devices


# ---------------------------------------------------------------------------
# Intel
# ---------------------------------------------------------------------------


def _detect_intel() -> list[DetectedGpu]:
    sysname = platform.system()
    if sysname == "Linux":
        return _detect_intel_lspci()
    if sysname == "Windows":
        return _detect_gpus_wmic(vendor_filter="intel")
    return []


def _detect_intel_lspci() -> list[DetectedGpu]:
    """`lspci` row for Intel graphics. Typically the iGPU on most laptops/desktops."""
    lspci = shutil.which("lspci")
    if not lspci:
        return []
    try:
        out = subprocess.run(
            [lspci], capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if out.returncode != 0:
        return []

    devices: list[DetectedGpu] = []
    for line in out.stdout.splitlines():
        if not re.search(r"\b(VGA compatible controller|3D controller|Display controller)\b", line):
            continue
        if not re.search(r"Intel Corporation", line):
            continue
        # Pull the marketing name. Most lines look like:
        #   "Intel Corporation Alder Lake-S GT1 [UHD Graphics 730] (rev 0c)"
        m = re.search(r"\[([^\[\]]+)\]\s*(?:\(rev\s+[^\)]+\))?\s*$", line)
        if m:
            name = f"Intel {m.group(1).strip()}"
        else:
            after_colon = line.split(":", 2)[-1].strip()
            name = after_colon or "Intel GPU"

        # Arc A-series are discrete; UHD/Iris/Xe are integrated.
        kind = "discrete" if re.search(r"\bArc\s+A\d+", name, flags=re.IGNORECASE) else "integrated"

        devices.append(DetectedGpu(
            vendor="intel",
            name=name,
            device_kind=kind,
            vram_mb=None,
            accelerated_by_vibechek=False,
            unsupported_reason=_REASON_INTEL,
        ))
    return devices


# ---------------------------------------------------------------------------
# Apple (macOS only)
# ---------------------------------------------------------------------------


def _detect_apple() -> list[DetectedGpu]:
    """`system_profiler SPDisplaysDataType -json` on macOS.

    Apple Silicon (M-series) and external/AMD eGPUs both surface here. We
    tag Apple GPUs as `vendor="apple"` and AMD eGPUs as `vendor="amd"` with
    `device_kind="external"`.
    """
    if platform.system() != "Darwin":
        return []
    sp = shutil.which("system_profiler")
    if not sp:
        return []
    try:
        out = subprocess.run(
            [sp, "SPDisplaysDataType", "-json"],
            capture_output=True, text=True, timeout=8, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if out.returncode != 0 or not out.stdout.strip():
        return []
    try:
        payload = json.loads(out.stdout)
    except json.JSONDecodeError:
        return []

    items = payload.get("SPDisplaysDataType") or []
    if not isinstance(items, list):
        return []

    devices: list[DetectedGpu] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = (
            item.get("sppci_model")
            or item.get("_name")
            or "GPU"
        )
        vendor_id = (item.get("spdisplays_vendor") or "").lower()
        vram_str = item.get("spdisplays_vram") or item.get("spdisplays_vram_shared")
        vram_mb = _parse_macos_vram(vram_str)

        # Bus tells us internal vs external; "Built-In" is the Apple SoC GPU,
        # "PCIe" is a discrete card (including AMD eGPUs).
        bus = (item.get("sppci_bus") or "").lower()
        is_external = "pcie" in bus

        if "apple" in vendor_id or "apple" in str(name).lower():
            vendor: GpuVendor = "apple"
            reason: str | None = _REASON_APPLE
            kind: GpuKind = "integrated"
        elif "amd" in vendor_id or "ati" in vendor_id or "amd" in str(name).lower():
            vendor = "amd"
            reason = _REASON_AMD
            kind = "external" if is_external else "discrete"
        elif "nvidia" in vendor_id or "nvidia" in str(name).lower():
            # No modern macOS supports NVIDIA, but be defensive.
            vendor = "nvidia"
            reason = _REASON_UNKNOWN  # macOS CUDA path is dead
            kind = "external" if is_external else "discrete"
        elif "intel" in vendor_id or "intel" in str(name).lower():
            vendor = "intel"
            reason = _REASON_INTEL
            kind = "integrated"
        else:
            vendor = "unknown"
            reason = _REASON_UNKNOWN
            kind = "discrete"

        # Vibechek currently never accelerates on macOS regardless of vendor —
        # essentia's bundled TF doesn't ship for Darwin. Surface every Mac GPU
        # as unsupported so we don't lie.
        devices.append(DetectedGpu(
            vendor=vendor,
            name=str(name),
            device_kind=kind,
            vram_mb=vram_mb,
            accelerated_by_vibechek=False,
            unsupported_reason=reason,
        ))
    return devices


def _parse_macos_vram(value: object) -> int | None:
    """`system_profiler` returns VRAM as e.g. '8 GB' or '1536 MB'.

    Apple Silicon often omits it entirely (unified memory) — return None.
    """
    if not value:
        return None
    s = str(value).strip()
    m = re.match(r"([\d.]+)\s*(GB|MB|TB)", s, flags=re.IGNORECASE)
    if not m:
        return None
    try:
        num = float(m.group(1))
    except ValueError:
        return None
    unit = m.group(2).upper()
    if unit == "GB":
        return int(num * 1024)
    if unit == "MB":
        return int(num)
    if unit == "TB":
        return int(num * 1024 * 1024)
    return None


# ---------------------------------------------------------------------------
# Windows: video-controller enumeration shared helper for AMD + Intel
# ---------------------------------------------------------------------------


def _classify_windows_gpu(
    name: str, vram_mb: int | None, vendor_filter: str
) -> DetectedGpu | None:
    """Turn one (name, vram) Win32_VideoController row into a `DetectedGpu`.

    Returns None when `name` doesn't match the requested `vendor_filter`
    ("amd" or "intel"). Shared by the PowerShell-CIM and wmic-CSV paths so the
    vendor-match + integrated/discrete heuristics live in exactly one place.
    """
    if not name:
        return None
    filter_lc = vendor_filter.lower()
    name_lc = name.lower()
    if filter_lc == "amd" and not (
        "amd" in name_lc or "ati" in name_lc or "radeon" in name_lc
    ):
        return None
    if filter_lc == "intel" and "intel" not in name_lc:
        return None

    if filter_lc == "amd":
        vendor: GpuVendor = "amd"
        reason: str | None = _REASON_AMD
    else:
        vendor = "intel"
        reason = _REASON_INTEL

    # Heuristic for integrated vs discrete: Intel UHD/Iris/Xe (non-Arc)
    # are iGPUs; AMD "Radeon Graphics" without an RX is the APU.
    kind: GpuKind = "discrete"
    if vendor == "intel" and not re.search(r"\bArc\s+A\d+", name, flags=re.IGNORECASE):
        kind = "integrated"
    if vendor == "amd" and re.search(r"Radeon\s+Graphics$", name, flags=re.IGNORECASE):
        kind = "integrated"

    return DetectedGpu(
        vendor=vendor,
        name=name,
        device_kind=kind,
        vram_mb=vram_mb,
        accelerated_by_vibechek=False,
        unsupported_reason=reason,
    )


def _vram_mb_from_adapter_ram(ram: object) -> int | None:
    """AdapterRAM (bytes) → MB. 0/None/unparseable → None (shared/unknown iGPU)."""
    try:
        ram_i = int(ram)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return ram_i // (1024 * 1024) if ram_i > 0 else None


def _detect_gpus_cim(vendor_filter: str) -> list[DetectedGpu] | None:
    """Enumerate via PowerShell `Get-CimInstance Win32_VideoController` (JSON).

    Preferred over `wmic` because (a) `wmic` is deprecated and absent by
    default on Windows 11 24H2, and (b) JSON parsing is immune to the
    comma-in-name bug that the CSV path suffers (e.g. "Intel(R) Iris(R) Xe
    Graphics" is fine, but some OEM adapter names DO contain commas).

    Returns a (possibly empty) device list on success, or None when PowerShell
    is unavailable / failed — letting the caller fall back to wmic.
    """
    pwsh = shutil.which("powershell") or shutil.which("pwsh")
    if not pwsh:
        return None
    # -NoProfile keeps startup fast; ConvertTo-Json emits a single object for one
    # controller and an array for several, so we normalise below.
    script = (
        "Get-CimInstance Win32_VideoController | "
        "Select-Object Name,AdapterRAM | ConvertTo-Json -Compress"
    )
    try:
        out = subprocess.run(
            [pwsh, "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0 or not out.stdout.strip():
        return None
    try:
        data = json.loads(out.stdout)
    except (ValueError, json.JSONDecodeError):
        return None
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        return None

    devices: list[DetectedGpu] = []
    for row in data:
        if not isinstance(row, dict):
            continue
        name = str(row.get("Name") or "").strip()
        vram_mb = _vram_mb_from_adapter_ram(row.get("AdapterRAM"))
        dev = _classify_windows_gpu(name, vram_mb, vendor_filter)
        if dev is not None:
            devices.append(dev)
    return devices


def _detect_gpus_wmic(vendor_filter: str) -> list[DetectedGpu]:
    """Enumerate Windows GPUs for `vendor_filter` ("amd"/"intel").

    Prefers PowerShell `Get-CimInstance` (modern, JSON, comma-safe) and falls
    back to the legacy `wmic ... /format:csv` parser only when PowerShell is
    missing or errors. The CSV fallback uses the stdlib `csv` module so quoted
    adapter names containing commas no longer corrupt the column split (the old
    `line.split(",")` shredded them). If neither tool is available we return []
    and the user simply sees fewer detected GPUs — acceptable degradation.
    """
    cim = _detect_gpus_cim(vendor_filter)
    if cim is not None:
        return cim

    wmic = shutil.which("wmic")
    if not wmic:
        return []
    try:
        out = subprocess.run(
            [wmic, "path", "win32_videocontroller", "get",
             "name,adapterram", "/format:csv"],
            capture_output=True, text=True, timeout=8, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if out.returncode != 0:
        return []

    devices: list[DetectedGpu] = []
    # CSV header is typically: "Node,AdapterRAM,Name". Parse with the csv module
    # so a comma INSIDE a quoted Name field doesn't desync the columns.
    lines = [ln for ln in out.stdout.splitlines() if ln.strip()]
    if len(lines) < 2:
        return []
    reader = csv.reader(lines)
    rows = list(reader)
    header = [h.strip().lower() for h in rows[0]]
    try:
        i_name = header.index("name")
        i_ram = header.index("adapterram")
    except ValueError:
        return []

    for parts in rows[1:]:
        if len(parts) <= max(i_name, i_ram):
            continue
        name = parts[i_name].strip()
        vram_mb = _vram_mb_from_adapter_ram(parts[i_ram].strip())
        dev = _classify_windows_gpu(name, vram_mb, vendor_filter)
        if dev is not None:
            devices.append(dev)
    return devices


__all__ = [
    "DetectedGpu",
    "detect_all_gpus",
]
