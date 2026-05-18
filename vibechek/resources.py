"""System resource detection.

Exposes what the host has (CPU cores, RAM, GPU devices) so the UI can show
recommendations and the CLI can pick smart defaults.

All detection is best-effort: missing optional dependencies (psutil for memory,
tensorflow for GPU enumeration) degrade to None / [] rather than raising.
"""

from __future__ import annotations

import logging
import os
import platform
import shutil
import subprocess
from dataclasses import asdict, dataclass, field

log = logging.getLogger(__name__)


@dataclass
class GpuDevice:
    """A single GPU as the host sees it.

    `vendor` and the "acceleration" fields were added when we extended GPU
    detection to AMD/Intel/Apple (see `vibechek.gpu_detect`). The legacy
    `backend` field is kept for backward compat with serialized configs and
    older UI code paths — new code should consume `vendor` directly.
    """
    name: str
    backend: str  # "cuda" | "rocm" | "metal" | "unknown"
    memory_mb: int | None = None
    # Newer cross-vendor fields:
    vendor: str = "nvidia"  # nvidia | amd | intel | apple | unknown
    device_kind: str = "discrete"  # discrete | integrated | external
    accelerated_by_vibechek: bool = True  # default True preserves old NVIDIA semantics
    unsupported_reason: str | None = None


@dataclass
class SystemResources:
    platform: str
    cpu_count: int
    memory_total_mb: int | None
    memory_available_mb: int | None
    gpu_available: bool
    gpu_devices: list[GpuDevice] = field(default_factory=list)
    cuda_runtime: str | None = None  # e.g. "12.3" if `nvidia-smi` reports one
    # Cross-vendor counts: useful for the UI to decide whether to show the
    # "your GPU isn't supported" callout without scanning the device list itself.
    accelerated_gpu_count: int = 0
    unsupported_gpu_count: int = 0

    @property
    def recommended_workers(self) -> int:
        """Leave one core free for the OS / GUI."""
        return max(1, self.cpu_count - 1)


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


def detect() -> SystemResources:
    """Return a snapshot of available compute resources.

    `gpu_devices` now spans every vendor (NVIDIA + AMD + Intel + Apple), via
    `vibechek.gpu_detect.detect_all_gpus`. The legacy `gpu_available` flag
    means "an *accelerated* GPU is available" — i.e. there's at least one
    NVIDIA card the engine can actually use. This preserves the meaning the
    rest of the code (UI Stat, analyzer worker scaling) already relies on.
    """
    cross_devices = _all_gpu_devices()
    accelerated = sum(1 for d in cross_devices if d.accelerated_by_vibechek)
    unsupported = len(cross_devices) - accelerated
    return SystemResources(
        platform=platform.platform(),
        cpu_count=_cpu_count(),
        memory_total_mb=_memory_total_mb(),
        memory_available_mb=_memory_available_mb(),
        gpu_devices=cross_devices,
        gpu_available=accelerated > 0,
        cuda_runtime=_cuda_runtime(),
        accelerated_gpu_count=accelerated,
        unsupported_gpu_count=unsupported,
    )


def to_dict(r: SystemResources) -> dict:
    out = asdict(r)
    out["recommended_workers"] = r.recommended_workers
    return out


# ---------------------------------------------------------------------------
# CPU
# ---------------------------------------------------------------------------


def _cpu_count() -> int:
    # os.cpu_count includes hyperthreads. On modern CPUs that's a fair number
    # for ML-inference workers (each is GIL-bound but releases the GIL inside
    # the C++ Essentia operators).
    return os.cpu_count() or 1


# ---------------------------------------------------------------------------
# Memory
# ---------------------------------------------------------------------------


def _memory_total_mb() -> int | None:
    try:
        import psutil  # noqa: PLC0415
        return psutil.virtual_memory().total // (1024 * 1024)
    except ImportError:
        return _memory_total_fallback_mb()


def _memory_available_mb() -> int | None:
    try:
        import psutil  # noqa: PLC0415
        return psutil.virtual_memory().available // (1024 * 1024)
    except ImportError:
        return None


def _memory_total_fallback_mb() -> int | None:
    """Stdlib-only memory total. Linux only; returns None elsewhere."""
    try:
        with open("/proc/meminfo", encoding="utf-8") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    kb = int(line.split()[1])
                    return kb // 1024
    except OSError:
        pass
    return None


# ---------------------------------------------------------------------------
# GPU
# ---------------------------------------------------------------------------


def _gpu_devices() -> list[GpuDevice]:
    """NVIDIA-only enumeration, kept for any callers that still want the
    legacy CUDA-only view. Newer code should use `_all_gpu_devices()`.

    *Deliberately does NOT import TensorFlow.* Importing TF has the side-effect
    of initializing CUDA with the *current* value of `CUDA_VISIBLE_DEVICES`,
    which is set by `apply_gpu_preference()` right before model load. If
    `system_info` were to import TF here, every subsequent analyze would
    inherit whatever GPU visibility TF first saw — making `--gpu off` a no-op.
    The engine-side GPU probe (vibechek.wsl.probe_engine_gpu) is the right
    place to do an actual TF query, and runs in a *separate subprocess*.
    """
    return _gpu_devices_from_nvidia_smi()


def _all_gpu_devices() -> list[GpuDevice]:
    """Cross-vendor enumeration via `vibechek.gpu_detect`.

    Imported lazily so a stale/broken `gpu_detect` (e.g. mid-merge) can't
    take down resource detection — falls back to the NVIDIA-only path.
    """
    try:
        from vibechek.gpu_detect import detect_all_gpus  # noqa: PLC0415
    except Exception as e:  # noqa: BLE001
        log.warning("gpu_detect import failed, falling back to nvidia-smi only: %s", e)
        return _gpu_devices_from_nvidia_smi()
    out: list[GpuDevice] = []
    for d in detect_all_gpus():
        # Map vendor → backend for the legacy field. "cuda" for NVIDIA;
        # "rocm"/"metal" follow the existing convention; "unknown" else.
        backend = {
            "nvidia": "cuda",
            "amd": "rocm",
            "apple": "metal",
            "intel": "unknown",
        }.get(d.vendor, "unknown")
        out.append(GpuDevice(
            name=d.name,
            backend=backend,
            memory_mb=d.vram_mb,
            vendor=d.vendor,
            device_kind=d.device_kind,
            accelerated_by_vibechek=d.accelerated_by_vibechek,
            unsupported_reason=d.unsupported_reason,
        ))
    return out


def _gpu_devices_from_nvidia_smi() -> list[GpuDevice]:
    """Parse `nvidia-smi --query-gpu=name,memory.total --format=csv,noheader`."""
    smi = shutil.which("nvidia-smi")
    if not smi:
        return []
    try:
        out = subprocess.run(
            [smi, "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        log.debug("nvidia-smi probe failed: %s", e)
        return []

    devices: list[GpuDevice] = []
    for line in out.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 2:
            try:
                mem = int(parts[1])
            except ValueError:
                mem = None
            devices.append(GpuDevice(
                name=parts[0],
                backend="cuda",
                memory_mb=mem,
                vendor="nvidia",
                device_kind="discrete",
                accelerated_by_vibechek=True,
                unsupported_reason=None,
            ))
    return devices


def _cuda_runtime() -> str | None:
    """CUDA driver version as reported by `nvidia-smi`, e.g. '12.3'. None if no NVIDIA GPU."""
    smi = shutil.which("nvidia-smi")
    if not smi:
        return None
    try:
        out = subprocess.run(
            [smi, "--query-gpu=driver_version", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    first = out.stdout.strip().splitlines()
    return first[0].strip() if first else None


# ---------------------------------------------------------------------------
# GPU runtime control
# ---------------------------------------------------------------------------


def apply_gpu_preference(use_gpu: str) -> None:
    """Set environment variables that TensorFlow reads at import time.

    Must be called BEFORE essentia/tensorflow is imported in the same process.

    - "auto" → leave defaults; TF uses GPU if available
    - "on"   → force GPU 0 visible
    - "off"  → hide all GPUs (CPU-only)
    """
    if use_gpu == "off":
        os.environ["CUDA_VISIBLE_DEVICES"] = "-1"
    elif use_gpu == "on":
        # Explicit "0" forces device 0; if no GPU exists, TF falls back to CPU
        # but logs a warning — which is the user's expected feedback.
        os.environ["CUDA_VISIBLE_DEVICES"] = "0"
    # else "auto": don't set the var; TF picks GPU if it can


__all__ = [
    "GpuDevice",
    "SystemResources",
    "detect",
    "to_dict",
    "apply_gpu_preference",
]
