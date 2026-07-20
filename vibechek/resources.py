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

    # A non-zero exit means the driver/NVML is in a bad state ("Failed to
    # initialize NVML: Driver/library version mismatch" is a common one).
    # stdout is usually empty in that case, but a future driver could print a
    # partial banner to stdout — bail rather than risk fabricating a device.
    if out.returncode != 0:
        log.debug("nvidia-smi returned %d: %s", out.returncode, out.stderr.strip())
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


# ---------------------------------------------------------------------------
# Worker-budget model — the SINGLE source of truth for "how many workers"
# ---------------------------------------------------------------------------
#
# Both the analyzer's sizing block AND the `worker_budget` RPC (which feeds the
# Settings slider) call `compute_worker_budget`, so the slider max and the
# actual run can NEVER disagree — the exact class of bug the user hit ("slider
# said 16, the run silently clamped to 2 with only a discarded log line").

# Per-worker RAM budget (MB). The essentia/TF path was measured ~340 MB resident
# after load; 800 leaves headroom for per-track buffers + TF inference. CLAP
# loads a ~2.2 GB fp32 checkpoint + the torch runtime INTO EVERY worker (measured
# ~3.8 GB resident, peaking higher during inference) — so it budgets 4.5 GB. The
# old 3.5 GB estimate still let three 3.8 GB workers OOM a 16 GB machine (silent
# exit 1, the worker killed before it could print); sizing CLAP off the 800 MB
# baseline was even worse (a 32 GB box at 15 workers ≈ 50 GB resident).
_ESSENTIA_TF_WORKER_MB = 800
_CLAP_WORKER_MB = 4500

# VRAM budget per GPU worker. Tuned against an RTX 4070 Laptop (8 GB shared):
# 2500 → cap 3 ran cleanly, 1800 → cap 4 stalled at init OOM. Includes ~800 MB
# for the persistent CUDA context before any model/activation memory. Kept in
# sync with the analyzer, which re-exports it.
_GPU_WORKER_MB = 2500

# Fallback GPU cap when VRAM probing fails (nvidia-smi missing/unreadable) — the
# engine CAN register the GPU but we can't size precisely, so preserve the prior
# "at most 4 workers in GPU mode" behaviour rather than regress usable machines.
_GPU_FALLBACK_CAP = 4


def per_worker_mb(engine: str, genre_classifier: str) -> int:
    """RAM budget (MB) per analysis worker, used to cap the worker count.

    `genre_classifier="clap"` dominates the footprint (a 2.2 GB checkpoint per
    worker), so it wins regardless of engine. `engine` is accepted so the
    onnx/native engines can diverge from essentia_tf ONCE their per-worker RSS is
    actually measured — today they DELIBERATELY reuse the essentia_tf 800 MB
    baseline rather than invent an unmeasured number. That is a conservative
    reuse, not a guess: the ONNX weight set is ~half TF's and onnxruntime's
    baseline runtime is lighter than full TensorFlow, so the real onnx/native
    RSS is very likely BELOW 800 MB — reusing 800 sizes slightly fewer workers
    than RAM allows (safe) rather than risking the multi-worker OOM that a too-
    small budget would. Replace with a measured onnx/native constant here when
    one exists (see docs/native-windows-essentia notes).
    """
    if genre_classifier == "clap":
        return _CLAP_WORKER_MB
    return _ESSENTIA_TF_WORKER_MB


@dataclass
class WorkerBudget:
    """The computed worker plan for one analyze run.

    Shared by the analyzer's sizing block and the `worker_budget` RPC so the
    Settings slider's max and the actual run can never disagree. Every "why did
    I get fewer workers than I asked for?" answer lives here as a human-readable
    reason string the GUI can surface, instead of a log line the user never sees.
    """

    # RAM-and-core ceiling for the slider max. 0 means "nothing fits" — the run
    # must be refused (see refusal_reason).
    max_workers: int
    # gpu_workers + cpu_workers actually launched for THIS run.
    effective_workers: int
    per_worker_mb: int
    # Total RAM the cap math saw. Under WSL this is the VM's RAM, not the host's
    # — the pool the workers actually draw from.
    ram_seen_mb: int
    reserve_mb: int
    gpu_workers: int
    cpu_workers: int
    # Why effective_workers < the requested count (RAM cap or GPU single-device
    # cap). None when the request was honored in full.
    cap_reason: str | None = None
    # Why GPU workers = 0 despite GPU mode being on (engine can't register the
    # GPU, or insufficient VRAM). None when GPU mode is off or GPU workers ran.
    gpu_reason: str | None = None
    # Set IFF nothing fits (max_workers == 0). The caller MUST refuse the run and
    # surface this actionable message instead of launching a doomed worker.
    refusal_reason: str | None = None
    # Which RAM pool ram_seen_mb measured: "host" | "wsl_vm".
    ram_pool: str = "host"
    # The count the user asked for (raw config.workers, or the auto default),
    # for the "capped N→M" display.
    requested_workers: int = 0
    # False when psutil was unavailable and RAM-based capping had to be skipped.
    ram_measured: bool = True


def compute_worker_budget(
    engine: str,
    genre_classifier: str,
    requested_workers: int,
    *,
    total_ram_mb: int | None,
    free_vram_mb: int | None,
    gpu_registrable: bool | None,
    cpu_count: int,
    under_wsl: bool,
    use_gpu: str = "auto",
    hybrid: bool = True,
    ram_pool: str = "host",
) -> WorkerBudget:
    """Pure worker-sizing decision — no I/O, fully table-testable.

    Fixes over the old inline math (all silent before):
      * RAM cap can express "nothing fits" (0) → the caller refuses with an
        actionable message instead of launching one doomed OOM-killed worker.
      * GPU cap floors to 0 (not 1) → a near-empty card falls through to CPU
        instead of dispatching a single GPU worker that __init_fail__s and
        aborts the whole (otherwise viable) run.
      * GPU workers are gated on REGISTRATION (`gpu_registrable`), not just free
        VRAM — so we never report "N GPU workers" that silently run on CPU.
      * `total_ram_mb is None` (psutil missing) is a NAMED cap_reason, not a
        bare pass, so a stripped/frozen build is visible in diagnostics.

    Args:
      total_ram_mb: measured total RAM (WSL VM RAM under WSL), or None if psutil
        couldn't measure it — capping is then skipped and noted.
      free_vram_mb: the VRAM figure to size GPU workers against, or None when it
        couldn't be probed (→ conservative fallback cap). Callers pass a
        conservative reading (see the analyzer's min-of-samples probe) so a
        transient high free reading can't oversize the GPU pool.
      gpu_registrable: True/False when the engine's real GPU probe is
        conclusive; None when it couldn't run (we then size off VRAM as before
        rather than needlessly disabling a healthy GPU).
    """
    cpu_count = max(1, cpu_count)
    # Raw request for DISPLAY ("capped 16→2" shows the user's own 16). The run is
    # additionally clamped to the core count below.
    raw_request = (
        requested_workers if requested_workers and requested_workers > 0
        else max(1, cpu_count - 1)
    )
    requested_run = max(1, min(raw_request, cpu_count))
    pw = per_worker_mb(engine, genre_classifier)
    # Hold back more under WSL: the VM shares one pool of physical RAM with the
    # Windows host + GUI + browser, so a 2 GB Linux-only reserve starves Windows
    # and the whole machine thrashes/OOMs.
    reserve_mb = 4096 if under_wsl else 2048

    classifier_label = "CLAP" if genre_classifier == "clap" else "Discogs"
    pool_label = "the WSL VM" if (under_wsl or ram_pool == "wsl_vm") else "this machine"

    cap_reason: str | None = None

    if total_ram_mb is None:
        # psutil unavailable → we CANNOT bound workers by RAM. Name it (the old
        # code did a bare `pass`) so a psutil-less venv/frozen build shows up in
        # logs/diagnostics instead of silently oversubscribing (cpu_count-1 CLAP
        # workers × 4.5 GB would thrash the box).
        max_workers = cpu_count
        ram_seen = 0
        cap_reason = (
            "RAM-based worker capping unavailable (psutil not importable) — "
            f"sizing by CPU cores ({cpu_count}) only, unbounded by memory"
        )
        return _finish_budget(
            max_workers=max_workers, requested_run=requested_run,
            raw_request=raw_request, pw=pw, ram_seen=ram_seen,
            reserve_mb=reserve_mb, cpu_count=cpu_count, use_gpu=use_gpu,
            hybrid=hybrid, free_vram_mb=free_vram_mb,
            gpu_registrable=gpu_registrable, ram_pool=ram_pool,
            cap_reason=cap_reason, ram_measured=False,
        )

    ram_seen = total_ram_mb
    usable = max(0, total_ram_mb - reserve_mb)
    memory_cap = usable // pw  # FLOOR TO 0 — "nothing fits" is now expressible.
    if memory_cap <= 0:
        # Refuse rather than force a single worker the budget itself says won't
        # fit (the old max(1, ...) launched one and got it OOM-killed silently).
        # `refusal_reason` is now a PLAIN headline (shown verbatim on the Settings
        # slider + the refused-run event). The GB math / classifier name /
        # .wslconfig mechanism are DEMOTED to `memory_refusal_detail()`, and the
        # machine-readable action flags come from `memory_refusal_options()` — the
        # analyzer packs both into the UserFacingError it raises (WP-D3).
        refusal = (
            "Not enough memory to run the advanced genre model right now."
            if genre_classifier == "clap"
            else "Not enough memory to run analysis right now."
        )
        return WorkerBudget(
            max_workers=0, effective_workers=0, per_worker_mb=pw,
            ram_seen_mb=ram_seen, reserve_mb=reserve_mb, gpu_workers=0,
            cpu_workers=0, refusal_reason=refusal, ram_pool=ram_pool,
            requested_workers=raw_request, ram_measured=True,
        )

    max_workers = min(cpu_count, memory_cap)
    if max_workers < requested_run or max_workers < raw_request:
        # The RUN is capped below what the user asked for. Surface it (was a
        # discarded log.warning) so "16→2" reaches the GUI, on WSL runs too.
        cap_reason = (
            f"Workers capped {raw_request}→{max_workers}: {classifier_label} "
            f"needs ~{pw / 1024:.1f} GB each; {pool_label} has "
            f"{ram_seen / 1024:.1f} GB"
        )
    return _finish_budget(
        max_workers=max_workers, requested_run=requested_run,
        raw_request=raw_request, pw=pw, ram_seen=ram_seen,
        reserve_mb=reserve_mb, cpu_count=cpu_count, use_gpu=use_gpu,
        hybrid=hybrid, free_vram_mb=free_vram_mb,
        gpu_registrable=gpu_registrable, ram_pool=ram_pool,
        cap_reason=cap_reason, ram_measured=True,
    )


def memory_refusal_detail(budget: WorkerBudget, genre_classifier: str) -> str:
    """The technical explanation behind a memory refusal — DEMOTED to the toast
    detail toggle (the plain headline is `budget.refusal_reason`, WP-D3).

    Rebuilt from the budget's own measured numbers so the GB math can never
    diverge from the cap that produced it. Names CLAP + `.wslconfig` here (in the
    detail), not in the headline.
    """
    pw_gb = budget.per_worker_mb / 1024
    ram_gb = budget.ram_seen_mb / 1024
    reserve_gb = budget.reserve_mb / 1024
    usable_gb = max(0, budget.ram_seen_mb - budget.reserve_mb) / 1024
    under_wsl = budget.ram_pool == "wsl_vm"
    pool_label = (
        "the Linux analysis environment (WSL)" if under_wsl else "this computer"
    )
    reserve_for = "Windows and the rest of your PC" if under_wsl else "the operating system"
    model_label = (
        "the advanced genre model (CLAP)" if genre_classifier == "clap"
        else "each analysis worker"
    )
    detail = (
        f"{model_label} needs about {pw_gb:.1f} GB per worker, but {pool_label} "
        f"has only {usable_gb:.1f} GB usable ({ram_gb:.1f} GB total minus "
        f"{reserve_gb:.1f} GB held back for {reserve_for})."
    )
    if genre_classifier == "clap":
        detail += (
            " Fixes: switch to the standard genre model, or give the Linux "
            "analysis environment more memory (its limit lives in .wslconfig)."
            if under_wsl
            else " Fix: switch to the standard genre model, or free some memory."
        )
    else:
        detail += " Free some memory or close other apps, then try again."
    return detail


def memory_refusal_options(genre_classifier: str, under_wsl: bool) -> dict[str, bool]:
    """Machine-readable flags the shell keys the refusal's action buttons off of.

    `can_switch_classifier` — the run used CLAP; the standard (Discogs) model
    needs far less RAM, so a one-click switch can unblock the run.
    `can_increase_memory` — under WSL the VM's `memory=` limit can be raised
    (the `increase_wsl_memory` RPC / `bump_wslconfig_memory`).
    """
    return {
        "can_switch_classifier": genre_classifier == "clap",
        "can_increase_memory": bool(under_wsl),
    }


def _finish_budget(
    *, max_workers: int, requested_run: int, raw_request: int, pw: int,
    ram_seen: int, reserve_mb: int, cpu_count: int, use_gpu: str, hybrid: bool,
    free_vram_mb: int | None, gpu_registrable: bool | None, ram_pool: str,
    cap_reason: str | None, ram_measured: bool,
) -> WorkerBudget:
    """Split the RAM-bounded total into GPU + CPU workers and finalize."""
    ram_cap = min(requested_run, max_workers)  # RAM-bounded total for THIS run
    gpu_workers = 0
    cpu_workers = ram_cap  # default (GPU off, or GPU unavailable): all CPU
    gpu_reason: str | None = None

    if use_gpu in ("auto", "on"):
        if gpu_registrable is False:
            # The headline bug: sizing GPU workers from nvidia-smi VRAM alone
            # reported "3 GPU workers" while TF couldn't register the GPU, so all
            # 3 silently ran on CPU. If the engine can't register the GPU we
            # launch ZERO GPU workers and say why — no phantom GPU pool.
            gpu_reason = "the analysis engine could not register the GPU"
        else:
            if free_vram_mb is None:
                gpu_cap = _GPU_FALLBACK_CAP
            else:
                gpu_cap = free_vram_mb // _GPU_WORKER_MB  # FLOOR TO 0
                if gpu_cap <= 0:
                    gpu_reason = (
                        f"insufficient free VRAM ({free_vram_mb} MB; a GPU worker "
                        f"needs ~{_GPU_WORKER_MB} MB)"
                    )
            gpu_workers = min(max(0, gpu_cap), ram_cap)
            if gpu_workers > 0:
                if hybrid and ram_cap > gpu_workers:
                    # Hybrid: GPU workers + CPU workers filling the rest of the
                    # RAM budget, all pulling from one shared queue.
                    cpu_workers = max(0, min(cpu_count, ram_cap) - gpu_workers)
                else:
                    cpu_workers = 0
            else:
                # GPU unavailable/insufficient — use the full RAM budget on CPU.
                # THIS is the fix for the doomed 1-GPU-worker abort: fall through
                # to CPU instead of dispatching a worker that can't init.
                cpu_workers = ram_cap

    effective_workers = gpu_workers + cpu_workers
    if cap_reason is None and effective_workers < raw_request:
        # Not a RAM cap (that already set cap_reason). The single-device GPU cap
        # left fewer workers than requested — explain it so it isn't a mystery.
        cap_reason = (
            f"Capped to {gpu_workers} GPU worker(s) from {raw_request}: enable "
            "hybrid CPU+GPU, or set GPU mode off, to use more CPU workers"
        )

    return WorkerBudget(
        max_workers=max_workers, effective_workers=effective_workers,
        per_worker_mb=pw, ram_seen_mb=ram_seen, reserve_mb=reserve_mb,
        gpu_workers=gpu_workers, cpu_workers=cpu_workers, cap_reason=cap_reason,
        gpu_reason=gpu_reason, ram_pool=ram_pool, requested_workers=raw_request,
        ram_measured=ram_measured,
    )


__all__ = [
    "GpuDevice",
    "SystemResources",
    "WorkerBudget",
    "detect",
    "to_dict",
    "apply_gpu_preference",
    "compute_worker_budget",
    "memory_refusal_detail",
    "memory_refusal_options",
    "per_worker_mb",
]
