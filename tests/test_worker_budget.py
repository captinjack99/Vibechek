"""Tests for the shared worker-budget model (Wave 2 WP4/WP5 + the dynamic-
resources directive).

`vibechek.resources.compute_worker_budget` is the ONE pure function both the
analyzer's sizing block and the `worker_budget` RPC call, so the Settings slider
max and the actual run can never disagree — the exact bug the user hit (slider
said 16, the run silently clamped to 2 with only a discarded log line).

These are table-driven over the live-confirmed scenarios plus the arithmetic
edge cases the old inline math got wrong (RAM floor-to-1, GPU floor-to-1 that
dispatched a doomed worker, the silent psutil-missing pass, GPU workers sized
off nvidia-smi VRAM without a registration check).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from vibechek.resources import (
    _CLAP_WORKER_MB,
    _ESSENTIA_TF_WORKER_MB,
    _GPU_WORKER_MB,
    compute_worker_budget,
    per_worker_mb,
)

# ---------------------------------------------------------------------------
# per_worker_mb — engine-aware budget
# ---------------------------------------------------------------------------


def test_per_worker_mb_clap_dominates_every_engine() -> None:
    for engine in ("essentia_tf", "onnx", "native"):
        assert per_worker_mb(engine, "clap") == _CLAP_WORKER_MB
        # onnx/native deliberately REUSE the essentia_tf baseline (conservative,
        # not an invented number) until their real RSS is measured.
        assert per_worker_mb(engine, "discogs") == _ESSENTIA_TF_WORKER_MB


# ---------------------------------------------------------------------------
# RAM cap — the headline "16 → 2 under WSL" case + refusal
# ---------------------------------------------------------------------------


def test_clap_wsl_16req_caps_to_2_with_reason() -> None:
    """The user's live bug: CLAP on a 15.8 GB WSL VM, 16 requested → 2 workers
    with a GUI-visible reason (was a discarded log.warning)."""
    b = compute_worker_budget(
        "essentia_tf", "clap", 16,
        total_ram_mb=15821, free_vram_mb=None, gpu_registrable=None,
        cpu_count=16, under_wsl=True, use_gpu="off", ram_pool="wsl_vm",
    )
    assert b.max_workers == 2
    assert b.effective_workers == 2
    assert b.cap_reason is not None
    assert "16" in b.cap_reason and "2" in b.cap_reason
    assert "CLAP" in b.cap_reason
    assert b.refusal_reason is None
    assert b.ram_pool == "wsl_vm"


def test_discogs_same_env_stays_high() -> None:
    """Same 15.8 GB VM, Discogs' 800 MB/worker → 14 (16 − reserve headroom)."""
    b = compute_worker_budget(
        "essentia_tf", "discogs", 16,
        total_ram_mb=15821, free_vram_mb=None, gpu_registrable=None,
        cpu_count=16, under_wsl=True, use_gpu="off", ram_pool="wsl_vm",
    )
    assert b.max_workers == 14
    assert b.effective_workers == 14


def test_clap_8gb_refuses_with_plain_headline_and_demoted_detail() -> None:
    """8 GB WSL VM, CLAP → nothing fits: refuse. WP-D3: `refusal_reason` is now a
    PLAIN headline (no GB math / no "CLAP"/"Discogs" jargon — those move to the
    detail helper), and the machine-readable options drive the refusal buttons."""
    from vibechek.resources import memory_refusal_detail, memory_refusal_options

    b = compute_worker_budget(
        "essentia_tf", "clap", 16,
        total_ram_mb=8192, free_vram_mb=None, gpu_registrable=None,
        cpu_count=16, under_wsl=True, use_gpu="off", ram_pool="wsl_vm",
    )
    assert b.max_workers == 0
    assert b.effective_workers == 0
    # Plain headline — no engineer-speak, no numbers.
    assert b.refusal_reason == "Not enough memory to run the advanced genre model right now."
    assert "CLAP" not in b.refusal_reason
    assert "GB" not in b.refusal_reason
    # The technical explanation is DEMOTED to the detail, not deleted.
    detail = memory_refusal_detail(b, "clap")
    assert "GB" in detail
    assert ".wslconfig" in detail  # the mechanism, in the detail only
    # Machine-readable action flags for the next-wave buttons.
    opts = memory_refusal_options("clap", under_wsl=True)
    assert opts == {"can_switch_classifier": True, "can_increase_memory": True}


def test_memory_refusal_options_essentia_host() -> None:
    """Discogs on a native host → no classifier switch, no WSL memory bump."""
    from vibechek.resources import memory_refusal_options

    assert memory_refusal_options("discogs", under_wsl=False) == {
        "can_switch_classifier": False, "can_increase_memory": False,
    }


def test_ram_cap_never_floors_to_one_doomed_worker() -> None:
    """The old `max(1, usable // per_worker)` forced 1 worker even at 0 usable.
    Now 0-fitting → refusal, not a silent OOM-bound worker."""
    b = compute_worker_budget(
        "essentia_tf", "clap", 4,
        total_ram_mb=4096, free_vram_mb=None, gpu_registrable=None,
        cpu_count=8, under_wsl=True, use_gpu="off", ram_pool="wsl_vm",
    )
    assert b.max_workers == 0
    assert b.refusal_reason is not None


# ---------------------------------------------------------------------------
# GPU cap — floor to 0, registration gating
# ---------------------------------------------------------------------------


def test_gpu_not_registrable_yields_zero_gpu_workers_with_reason() -> None:
    """The headline WP5 bug: nvidia-smi VRAM sized "3 GPU workers" while TF
    couldn't register the GPU → all ran on CPU. Now registration gates it:
    0 GPU workers, full CPU budget, honest reason."""
    b = compute_worker_budget(
        "essentia_tf", "discogs", 8,
        total_ram_mb=32000, free_vram_mb=8000, gpu_registrable=False,
        cpu_count=8, under_wsl=False, use_gpu="on", hybrid=True,
    )
    assert b.gpu_workers == 0
    assert b.cpu_workers == 8  # falls through to the full CPU budget
    assert b.gpu_reason is not None
    assert "register" in b.gpu_reason


def test_near_empty_vram_floors_to_zero_not_a_doomed_pool() -> None:
    """WP4 HIGH: 200 MB free VRAM used to floor to 1 GPU worker that
    __init_fail__'d and aborted the WHOLE (viable) run. Now → 0 GPU workers +
    reason, falls through to CPU."""
    b = compute_worker_budget(
        "essentia_tf", "discogs", 8,
        total_ram_mb=32000, free_vram_mb=200, gpu_registrable=True,
        cpu_count=8, under_wsl=False, use_gpu="on", hybrid=True,
    )
    assert b.gpu_workers == 0
    assert b.cpu_workers == 8
    assert b.gpu_reason is not None
    assert "VRAM" in b.gpu_reason


def test_healthy_hybrid_splits_gpu_and_cpu() -> None:
    """8 GB free VRAM, registrable, 4 requested → 3 GPU (8000//2500) + hybrid
    CPU fills the rest of the RAM budget."""
    b = compute_worker_budget(
        "essentia_tf", "discogs", 4,
        total_ram_mb=32000, free_vram_mb=8000, gpu_registrable=True,
        cpu_count=8, under_wsl=False, use_gpu="on", hybrid=True,
    )
    assert b.gpu_workers == 8000 // _GPU_WORKER_MB  # 3
    assert b.effective_workers == 4
    assert b.cpu_workers == 4 - b.gpu_workers
    assert b.gpu_reason is None


def test_gpu_off_is_all_cpu() -> None:
    b = compute_worker_budget(
        "essentia_tf", "discogs", 6,
        total_ram_mb=32000, free_vram_mb=8000, gpu_registrable=True,
        cpu_count=8, under_wsl=False, use_gpu="off",
    )
    assert b.gpu_workers == 0
    assert b.cpu_workers == 6
    assert b.gpu_reason is None  # GPU off → no "unavailable" story


def test_vram_probe_none_uses_fallback_cap() -> None:
    """nvidia-smi unreadable but the engine CAN register → conservative cap of 4
    (preserves prior behaviour on GPU boxes where the probe fails)."""
    b = compute_worker_budget(
        "essentia_tf", "discogs", 8,
        total_ram_mb=32000, free_vram_mb=None, gpu_registrable=True,
        cpu_count=8, under_wsl=False, use_gpu="on", hybrid=True,
    )
    assert b.gpu_workers == 4  # _GPU_FALLBACK_CAP
    assert b.effective_workers == 8


# ---------------------------------------------------------------------------
# psutil-missing path — named, not a silent pass
# ---------------------------------------------------------------------------


def test_psutil_absent_is_named_not_silent() -> None:
    b = compute_worker_budget(
        "essentia_tf", "clap", 8,
        total_ram_mb=None, free_vram_mb=None, gpu_registrable=None,
        cpu_count=8, under_wsl=False, use_gpu="off",
    )
    assert b.ram_measured is False
    assert b.cap_reason is not None
    assert "psutil" in b.cap_reason
    # No RAM info → cores are the only ceiling; still runs.
    assert b.max_workers == 8
    assert b.effective_workers == 8


# ---------------------------------------------------------------------------
# Slider-max semantics — a big request is bounded, a small one isn't inflated
# ---------------------------------------------------------------------------


def test_max_workers_is_ram_and_core_ceiling() -> None:
    # Plenty of RAM, few cores → cores bound the max.
    b = compute_worker_budget(
        "essentia_tf", "discogs", 0,
        total_ram_mb=64000, free_vram_mb=None, gpu_registrable=None,
        cpu_count=4, under_wsl=False, use_gpu="off",
    )
    assert b.max_workers == 4


def test_request_honored_when_it_fits_has_no_cap_reason() -> None:
    b = compute_worker_budget(
        "essentia_tf", "discogs", 4,
        total_ram_mb=32000, free_vram_mb=None, gpu_registrable=None,
        cpu_count=8, under_wsl=False, use_gpu="off",
    )
    assert b.effective_workers == 4
    assert b.cap_reason is None


# ---------------------------------------------------------------------------
# The worker_budget RPC — measures the right pool, mocks the probes
# ---------------------------------------------------------------------------


def test_rpc_worker_budget_measures_wsl_vm_on_windows() -> None:
    """essentia_tf on Windows routes through WSL → the budget must measure the
    VM's RAM (15.8 GB), not the host total (31.7 GB)."""
    from vibechek import resources, rpc, wsl

    fake_res = resources.SystemResources(
        platform="win", cpu_count=16, memory_total_mb=32000,
        memory_available_mb=16000, gpu_available=False,
    )
    with patch.object(resources, "detect", return_value=fake_res), \
         patch.object(wsl, "IS_WINDOWS", True), \
         patch.object(wsl, "wsl_vm_memory_mb", return_value=15821):
        out = rpc._worker_budget({
            "engine": "essentia_tf", "genre_classifier": "clap",
            "workers": 16, "distro": "Ubuntu-24.04",
        })
    assert out["ram_pool"] == "wsl_vm"
    assert out["ram_seen_mb"] == 15821
    assert out["max_workers"] == 2  # CLAP on 15.8 GB VM


def test_rpc_worker_budget_falls_back_to_host_without_distro() -> None:
    from vibechek import resources, rpc, wsl

    fake_res = resources.SystemResources(
        platform="linux", cpu_count=8, memory_total_mb=32000,
        memory_available_mb=16000, gpu_available=False,
    )
    with patch.object(resources, "detect", return_value=fake_res), \
         patch.object(wsl, "IS_WINDOWS", False):
        out = rpc._worker_budget({
            "engine": "essentia_tf", "genre_classifier": "discogs", "workers": 6,
        })
    assert out["ram_pool"] == "host"
    assert out["ram_seen_mb"] == 32000
    assert out["effective_workers"] == 6


@pytest.mark.parametrize("engine", ["essentia_tf", "onnx", "native"])
def test_rpc_worker_budget_returns_full_shape(engine: str) -> None:
    """Every WorkerBudget field crosses the wire (dataclasses.asdict)."""
    from vibechek import resources, rpc, wsl

    fake_res = resources.SystemResources(
        platform="linux", cpu_count=8, memory_total_mb=32000,
        memory_available_mb=16000, gpu_available=False,
    )
    with patch.object(resources, "detect", return_value=fake_res), \
         patch.object(wsl, "IS_WINDOWS", False):
        out = rpc._worker_budget({"engine": engine, "workers": 4})
    for key in (
        "max_workers", "effective_workers", "per_worker_mb", "ram_seen_mb",
        "reserve_mb", "gpu_workers", "cpu_workers", "cap_reason", "gpu_reason",
        "refusal_reason", "ram_pool", "requested_workers", "ram_measured",
    ):
        assert key in out
