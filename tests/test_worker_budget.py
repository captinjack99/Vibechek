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

from pathlib import Path
from unittest.mock import patch

import pytest

from vibechek import resources
from vibechek.resources import (
    _CLAP_RESIDENT_MB,
    _CLAP_WORKER_MB,
    _ESSENTIA_TF_RESIDENT_MB,
    _ESSENTIA_TF_WORKER_MB,
    _GPU_WORKER_MB,
    compute_worker_budget,
    decoded_audio_mb,
    per_worker_mb,
    track_length_note,
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
        available_mb=None,
    )
    assert b.max_workers == 2
    assert b.effective_workers == 2
    assert b.cap_reason is not None
    assert "16" in b.cap_reason and "2" in b.cap_reason
    # The GB math / model name are DEMOTED to cap_detail (voice-guide rule 9);
    # the calm cap_reason on the progress line stays free of them.
    assert "CLAP" in (b.cap_detail or "")
    assert "CLAP" not in b.cap_reason
    assert b.refusal_reason is None
    assert b.ram_pool == "wsl_vm"


def test_discogs_same_env_stays_high() -> None:
    """Same 15.8 GB VM, Discogs' 800 MB/worker → 14 (16 − reserve headroom)."""
    b = compute_worker_budget(
        "essentia_tf", "discogs", 16,
        total_ram_mb=15821, free_vram_mb=None, gpu_registrable=None,
        cpu_count=16, under_wsl=True, use_gpu="off", ram_pool="wsl_vm",
        available_mb=None,
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
        available_mb=None,
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
    # Machine-readable action flags for the next-wave buttons. 32 GB host: the
    # bump has something to give, so the button is offered.
    opts = memory_refusal_options("clap", under_wsl=True, host_total_mb=32768)
    assert opts == {"can_switch_classifier": True, "can_increase_memory": True}


def test_memory_refusal_options_essentia_host() -> None:
    """Discogs on a native host → no classifier switch, no WSL memory bump."""
    from vibechek.resources import memory_refusal_options

    assert memory_refusal_options("discogs", under_wsl=False) == {
        "can_switch_classifier": False, "can_increase_memory": False,
    }


@pytest.mark.parametrize("host_mb", [8192, 12288, 16384])
def test_memory_refusal_hides_the_bump_where_there_is_nothing_to_raise(
    host_mb: int,
) -> None:
    """On a 16 GB-or-smaller PC `bump_wslconfig_memory`'s target IS the size the
    VM already has (WSL's own min(host/2, 8 GB) default floors it), so it
    correctly answers "there's nothing to raise". Offering the button there sent
    the user through a self-heal that could not help, on the very screen that
    just refused their run for lack of memory."""
    from vibechek.resources import memory_refusal_options
    from vibechek.wsl import _choose_wslconfig_memory_target_mb, _wsl_default_memory_mb

    # Pin the claim rather than trust it: the bump has nothing to give here.
    target = _choose_wslconfig_memory_target_mb(host_mb)
    assert target is None or target <= _wsl_default_memory_mb(host_mb)

    assert memory_refusal_options(
        "clap", under_wsl=True, host_total_mb=host_mb,
    )["can_increase_memory"] is False


def test_memory_refusal_bump_offered_just_above_the_threshold() -> None:
    """17 GB: `host - 8 GB` finally clears WSL's default, so a bump is real."""
    from vibechek.resources import memory_refusal_options
    from vibechek.wsl import _choose_wslconfig_memory_target_mb, _wsl_default_memory_mb

    host_mb = 17408
    assert _choose_wslconfig_memory_target_mb(host_mb) > _wsl_default_memory_mb(host_mb)
    assert memory_refusal_options(
        "clap", under_wsl=True, host_total_mb=host_mb,
    )["can_increase_memory"] is True


def test_memory_refusal_withholds_the_bump_when_host_ram_is_unknown() -> None:
    """The refusal is computed INSIDE the WSL VM, where psutil reports the VM's
    own limit rather than the PC's. Guessing from that number would offer the
    button on exactly the machines it can't help, so an unknown host withholds
    it — the refusal detail still names the .wslconfig fix."""
    from vibechek.resources import memory_refusal_options

    assert memory_refusal_options("clap", under_wsl=True) == {
        "can_switch_classifier": True, "can_increase_memory": False,
    }


def test_ram_cap_never_floors_to_one_doomed_worker() -> None:
    """The old `max(1, usable // per_worker)` forced 1 worker even at 0 usable.
    Now 0-fitting → refusal, not a silent OOM-bound worker."""
    b = compute_worker_budget(
        "essentia_tf", "clap", 4,
        total_ram_mb=4096, free_vram_mb=None, gpu_registrable=None,
        cpu_count=8, under_wsl=True, use_gpu="off", ram_pool="wsl_vm",
        available_mb=None,
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
        available_mb=None,
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
        available_mb=None,
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
        available_mb=None,
    )
    assert b.gpu_workers == 8000 // _GPU_WORKER_MB  # 3
    assert b.effective_workers == 4
    assert b.cpu_workers == 4 - b.gpu_workers
    assert b.gpu_reason is None


def test_gpu_off_is_all_cpu() -> None:
    b = compute_worker_budget(
        "essentia_tf", "discogs", 6,
        total_ram_mb=32000, free_vram_mb=8000, gpu_registrable=True,
        cpu_count=8, under_wsl=False, use_gpu="off", available_mb=None,
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
        available_mb=None,
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
        cpu_count=8, under_wsl=False, use_gpu="off", available_mb=None,
    )
    assert b.ram_measured is False
    assert b.cap_reason is not None
    # The psutil mechanism is NAMED (not a silent `pass`) — but DEMOTED to the
    # detail; the calm cap_reason stays plain.
    assert "psutil" in (b.cap_detail or "")
    assert "psutil" not in b.cap_reason
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
        cpu_count=4, under_wsl=False, use_gpu="off", available_mb=None,
    )
    assert b.max_workers == 4


def test_request_honored_when_it_fits_has_no_cap_reason() -> None:
    b = compute_worker_budget(
        "essentia_tf", "discogs", 4,
        total_ram_mb=32000, free_vram_mb=None, gpu_registrable=None,
        cpu_count=8, under_wsl=False, use_gpu="off", available_mb=None,
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
         patch.object(resources, "_memory_available_mb", return_value=16000), \
         patch.object(wsl, "IS_WINDOWS", True), \
         patch.object(wsl, "wsl_vm_memory_mb", return_value=15821), \
         patch.object(wsl, "wsl_vm_available_mb", return_value=None):
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
         patch.object(resources, "_memory_available_mb", return_value=16000), \
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
         patch.object(resources, "_memory_available_mb", return_value=16000), \
         patch.object(wsl, "IS_WINDOWS", False):
        out = rpc._worker_budget({"engine": engine, "workers": 4})
    for key in (
        "max_workers", "effective_workers", "per_worker_mb", "ram_seen_mb",
        "reserve_mb", "gpu_workers", "cpu_workers", "cap_reason", "gpu_reason",
        "refusal_reason", "ram_pool", "requested_workers", "ram_measured",
    ):
        assert key in out


def _slider_env():
    """The finding's box: 10 GB, 8 cores, no WSL — where a set-heavy library
    plans 6 workers but the flat slider offered 8."""
    from vibechek import resources, wsl

    fake_res = resources.SystemResources(
        platform="linux", cpu_count=8, memory_total_mb=10240,
        memory_available_mb=10240, gpu_available=False,
    )
    return (patch.object(resources, "detect", return_value=fake_res),
            patch.object(resources, "_memory_available_mb", return_value=10240),
            patch.object(wsl, "IS_WINDOWS", False))


def test_rpc_worker_budget_sizes_against_the_named_library(
    tmp_path, monkeypatch,
) -> None:
    """The slider and the run share ONE model precisely so they cannot disagree,
    but the RPC never fed it a track-length measurement: on a library of 90-minute
    sets the Settings slider reported a max of 8 while the run planned 6 — the
    exact "slider said N, the run silently used fewer" bug the shared model
    exists to prevent."""
    from vibechek import rpc

    # A real library the real scan walks; only the header read is stubbed.
    (tmp_path / "long set.mp3").write_bytes(b"\0" * 64)

    class _Audio:
        def __init__(self, path: str) -> None:
            self.info = type("I", (), {"length": 90.0 * 60})()

    monkeypatch.setattr("mutagen.File", _Audio)

    a, b, c = _slider_env()
    with a, b, c:
        flat = rpc._worker_budget({
            "engine": "essentia_tf", "genre_classifier": "discogs", "workers": 8,
        })
        sets = rpc._worker_budget({
            "engine": "essentia_tf", "genre_classifier": "discogs", "workers": 8,
            "library_path": str(tmp_path),
        })

    assert flat["max_workers"] == 8
    assert sets["max_workers"] == 6
    assert sets["per_worker_mb"] > flat["per_worker_mb"]
    # ...and the slider now agrees with what the run would plan for that library.
    run = compute_worker_budget(
        "essentia_tf", "discogs", 8,
        total_ram_mb=10240, free_vram_mb=None, gpu_registrable=None,
        cpu_count=8, under_wsl=False, use_gpu="off", available_mb=10240,
        longest_track_seconds=90 * 60,
    )
    assert sets["max_workers"] == run.max_workers
    assert sets["per_worker_mb"] == run.per_worker_mb


def test_rpc_worker_budget_probes_the_real_library(tmp_path, monkeypatch) -> None:
    """`library_path` is walked with the same file scan the analyze run uses, and
    handed to the same probe — not re-implemented here."""
    from vibechek import resources, rpc

    (tmp_path / "a.mp3").write_bytes(b"\0" * 10)
    (tmp_path / "notes.txt").write_bytes(b"x")
    seen: dict = {}

    def fake_probe(paths, **_kw):
        seen["paths"] = [Path(p).name for p in paths]
        return 90 * 60

    monkeypatch.setattr(resources, "probe_longest_track_seconds", fake_probe)

    assert rpc._library_longest_track_seconds(str(tmp_path)) == 90 * 60
    assert seen["paths"] == ["a.mp3"]      # audio only, no stray text file


@pytest.mark.parametrize("bad", ["", None, "/nope/not/a/library"])
def test_rpc_worker_budget_falls_back_flat_without_a_usable_library(bad) -> None:
    """A slider hint must never become an error the user has to dismiss before
    they can move a slider: no path, or one that isn't there, answers exactly
    what this RPC answered before the track-length term existed."""
    from vibechek import rpc

    assert rpc._library_longest_track_seconds(bad) is None

    a, b, c = _slider_env()
    with a, b, c:
        out = rpc._worker_budget({
            "engine": "essentia_tf", "genre_classifier": "discogs", "workers": 8,
            "library_path": bad,
        })
    assert out["max_workers"] == 8


# ---------------------------------------------------------------------------
# F055 — size off what's FREE, not just what the machine HAS
# ---------------------------------------------------------------------------


def test_busy_16gb_box_does_not_plan_three_clap_workers() -> None:
    """The documented OOM: 16 GB total, ~7 GB actually free (GUI + browser +
    Rekordbox), CLAP at 3.8 GB resident. Sizing off total planned 3 workers
    (~11.4 GB) against 7 GB free and they were killed mid-run."""
    b = compute_worker_budget(
        "native", "clap", 0,
        total_ram_mb=16384, free_vram_mb=None, gpu_registrable=None,
        cpu_count=16, under_wsl=False, use_gpu="off", available_mb=7000,
    )
    # (7000 - 1024) // 4500 == 1
    assert b.effective_workers == 1
    assert b.cap_reason is not None
    assert "free right now" in b.cap_reason
    assert "free right now" in (b.cap_detail or "")


def test_available_can_only_lower_the_cap_never_raise_it() -> None:
    """A transient high `available` reading must not let the plan exceed what
    the total-RAM budget allows."""
    generous = compute_worker_budget(
        "essentia_tf", "discogs", 16,
        total_ram_mb=8192, free_vram_mb=None, gpu_registrable=None,
        cpu_count=16, under_wsl=False, use_gpu="off", available_mb=64000,
    )
    baseline = compute_worker_budget(
        "essentia_tf", "discogs", 16,
        total_ram_mb=8192, free_vram_mb=None, gpu_registrable=None,
        cpu_count=16, under_wsl=False, use_gpu="off", available_mb=None,
    )
    assert generous.max_workers == baseline.max_workers


def test_no_free_memory_refuses_with_an_honest_detail() -> None:
    """Nothing fits RIGHT NOW is a refusal like any other — and its detail must
    blame free memory, not the machine's capacity, or the user goes looking for
    RAM they already have."""
    from vibechek.resources import memory_refusal_detail

    b = compute_worker_budget(
        "essentia_tf", "discogs", 4,
        total_ram_mb=32000, free_vram_mb=None, gpu_registrable=None,
        cpu_count=8, under_wsl=False, use_gpu="off", available_mb=1200,
    )
    assert b.max_workers == 0
    assert b.refusal_reason is not None
    detail = memory_refusal_detail(b, "discogs", available_mb=1200)
    assert "free right now" in detail
    assert "31.2 GB usable" not in detail


def test_availability_cap_is_skipped_for_a_wsl_vm_total() -> None:
    """Measuring here describes THIS process's pool. Under WSL-from-Windows the
    total is the VM's limit while psutil sees the Windows host — mixing the two
    would cap a healthy VM off an unrelated number."""
    from unittest.mock import patch as _patch

    from vibechek import resources

    with _patch.object(resources, "_memory_available_mb", return_value=500) as m:
        b = compute_worker_budget(
            "essentia_tf", "discogs", 8,
            total_ram_mb=15821, free_vram_mb=None, gpu_registrable=None,
            cpu_count=8, under_wsl=True, use_gpu="off", ram_pool="wsl_vm",
        )
    assert m.call_count == 0
    assert b.effective_workers == 8


def test_default_measures_available_so_the_rpc_needs_no_change() -> None:
    """The slider (worker_budget RPC) and the run must agree; the RPC doesn't
    pass the reading, so the default has to take it."""
    from unittest.mock import patch as _patch

    from vibechek import resources

    with _patch.object(resources, "_memory_available_mb", return_value=3000) as m:
        b = compute_worker_budget(
            "essentia_tf", "discogs", 8,
            total_ram_mb=32000, free_vram_mb=None, gpu_registrable=None,
            cpu_count=8, under_wsl=False, use_gpu="off",
        )
    assert m.call_count == 1
    assert b.max_workers == (3000 - 1024) // _ESSENTIA_TF_WORKER_MB  # 2


# ---------------------------------------------------------------------------
# F091 — a core-count cap must not be reported as a memory cap
# ---------------------------------------------------------------------------


def test_core_cap_is_not_blamed_on_memory() -> None:
    """8 cores, 62.5 GB RAM, workers=16 hand-set: the run drops to 8 because of
    CORES. Blaming memory sent the user closing apps for nothing."""
    b = compute_worker_budget(
        "essentia_tf", "discogs", 16,
        total_ram_mb=64000, free_vram_mb=None, gpu_registrable=None,
        cpu_count=8, under_wsl=False, use_gpu="off", available_mb=None,
    )
    assert b.max_workers == 8
    assert b.cap_reason is not None
    assert "memory" not in b.cap_reason.lower()
    assert "8 CPU cores" in b.cap_reason
    assert "16" in (b.cap_detail or "")


def test_memory_cap_wording_survives_when_memory_really_binds() -> None:
    """The core-count wording must not swallow the real RAM cap: 16 cores but
    only enough RAM for 2 CLAP workers still reads as a memory cap."""
    b = compute_worker_budget(
        "essentia_tf", "clap", 16,
        total_ram_mb=15821, free_vram_mb=None, gpu_registrable=None,
        cpu_count=16, under_wsl=True, use_gpu="off", ram_pool="wsl_vm",
        available_mb=None,
    )
    assert b.max_workers == 2
    assert "memory" in (b.cap_reason or "").lower()


# ---------------------------------------------------------------------------
# P01 — the per-worker budget has to know how long the tracks are
# ---------------------------------------------------------------------------


def test_a_library_of_singles_keeps_the_flat_budget() -> None:
    """The measured-and-shipped numbers must not move for ordinary libraries:
    the flat budget already allows 460 MB of buffers on the standard route, and
    a 6-minute track decodes to ~60 MB."""
    assert per_worker_mb("essentia_tf", "discogs", 6 * 60) == _ESSENTIA_TF_WORKER_MB
    assert per_worker_mb("essentia_tf", "clap", 6 * 60) == _CLAP_WORKER_MB
    # No probe at all is the same answer, by construction.
    assert per_worker_mb("essentia_tf", "discogs", None) == _ESSENTIA_TF_WORKER_MB
    assert per_worker_mb("essentia_tf", "discogs", 0) == _ESSENTIA_TF_WORKER_MB


def test_a_ninety_minute_set_raises_the_per_worker_budget() -> None:
    """A worker holds the whole decoded track: 90 minutes at 44.1 kHz mono
    float32 is ~908 MB inside a budget of 800. The overshoot is what gets added,
    so the answer is the measured resident floor plus the real decode."""
    pw = per_worker_mb("essentia_tf", "discogs", 90 * 60)
    assert pw == _ESSENTIA_TF_RESIDENT_MB + decoded_audio_mb("discogs", 90 * 60)
    assert pw > _ESSENTIA_TF_WORKER_MB
    # CLAP decodes at 48 kHz — the largest of its three decodes.
    assert (per_worker_mb("essentia_tf", "clap", 90 * 60)
            == _CLAP_RESIDENT_MB + decoded_audio_mb("clap", 90 * 60))


def test_the_budget_grows_monotonically_with_track_length() -> None:
    lengths = [0, 20 * 60, 43 * 60, 60 * 60, 120 * 60]
    sizes = [per_worker_mb("essentia_tf", "discogs", s) for s in lengths]
    assert sizes == sorted(sizes)
    assert sizes[0] < sizes[-1]


def test_a_set_heavy_library_shrinks_the_pool_and_says_why() -> None:
    """The finding's own scenario: six workers on 90-minute recorded sets. The
    flat model planned the pool at 800 MB each while each worker really held
    ~1.25 GB — a ~1.6x oversubscription of a pool sized to fit."""
    singles = compute_worker_budget(
        "essentia_tf", "discogs", 8,
        total_ram_mb=10240, free_vram_mb=None, gpu_registrable=None,
        cpu_count=8, under_wsl=False, use_gpu="off", available_mb=None,
    )
    sets = compute_worker_budget(
        "essentia_tf", "discogs", 8,
        total_ram_mb=10240, free_vram_mb=None, gpu_registrable=None,
        cpu_count=8, under_wsl=False, use_gpu="off", available_mb=None,
        longest_track_seconds=90 * 60,
    )
    # 8 GB usable / 800 MB = 10, clamped to the 8 cores.
    assert singles.max_workers == 8
    # ...but each worker really needs ~1.25 GB here, so only 6 fit.
    assert sets.max_workers == (10240 - 2048) // sets.per_worker_mb == 6
    assert sets.max_workers < singles.max_workers
    assert sets.per_worker_mb > singles.per_worker_mb
    # The reason is IN the explanation text, not just in the number.
    detail = sets.cap_detail or ""
    assert "90 minutes" in detail
    assert "decoded" in detail
    # ...and it stays out of the calm headline (voice-guide rule 9).
    assert "decoded" not in (sets.cap_reason or "")


def test_the_length_note_is_absent_when_the_term_did_not_move_the_budget() -> None:
    """No probe, or a short library: the detail reads exactly as it always did."""
    assert track_length_note("essentia_tf", "discogs", None) == ""
    assert track_length_note("essentia_tf", "discogs", 5 * 60) == ""
    assert "decoded" in track_length_note("essentia_tf", "discogs", 90 * 60)


def test_worker_budget_still_has_no_new_wire_field() -> None:
    """The track-length term rides the EXISTING fields (per_worker_mb +
    cap_detail). WorkerBudget crosses the wire through the TS codegen, so a new
    field here silently reds the drift gate."""
    import dataclasses

    from vibechek.resources import WorkerBudget

    assert {f.name for f in dataclasses.fields(WorkerBudget)} == {
        "max_workers", "effective_workers", "per_worker_mb", "ram_seen_mb",
        "reserve_mb", "gpu_workers", "cpu_workers", "cap_reason", "cap_detail",
        "gpu_reason", "refusal_reason", "ram_pool", "requested_workers",
        "ram_measured",
    }


# ---------------------------------------------------------------------------
# P01 — the probe that feeds it
# ---------------------------------------------------------------------------


def test_probe_reads_the_longest_of_the_biggest_files(tmp_path, monkeypatch) -> None:
    """Sizes pick the candidates, mutagen reads the durations, the LONGEST wins.

    The 2-hour set is the third-biggest file, so a probe that only opened the
    single largest would miss it."""
    lengths = {"a.mp3": 300.0, "b.mp3": 7200.0, "c.mp3": 420.0}
    sizes = {"a.mp3": 9000, "b.mp3": 7000, "c.mp3": 8000}
    for name, size in sizes.items():
        (tmp_path / name).write_bytes(b"\0" * size)

    class _Info:
        def __init__(self, length: float) -> None:
            self.length = length

    class _Audio:
        def __init__(self, path: str) -> None:
            self.info = _Info(lengths[Path(path).name])

    monkeypatch.setattr("mutagen.File", _Audio)
    got = resources.probe_longest_track_seconds(
        [tmp_path / n for n in lengths], sample=3)
    assert got == 7200.0


def test_probe_only_opens_the_biggest_few_files(tmp_path, monkeypatch) -> None:
    """Worker sizing must not turn into a full library scan: `sample` bounds the
    header reads, and they are spent on the biggest files."""
    opened: list[str] = []
    for i in range(20):
        (tmp_path / f"t{i}.mp3").write_bytes(b"\0" * (1000 + i))

    class _Audio:
        def __init__(self, path: str) -> None:
            opened.append(Path(path).name)
            self.info = type("I", (), {"length": 60.0})()

    monkeypatch.setattr("mutagen.File", _Audio)
    resources.probe_longest_track_seconds(
        [tmp_path / f"t{i}.mp3" for i in range(20)], sample=3)
    assert opened == ["t19.mp3", "t18.mp3", "t17.mp3"]


def test_probe_returns_none_rather_than_a_guess(tmp_path, monkeypatch) -> None:
    """An unreadable header, a vanished file, an empty library: no measurement,
    so no number — `per_worker_mb` then keeps the flat budget instead of sizing
    the pool against something nobody measured."""
    (tmp_path / "a.mp3").write_bytes(b"\0" * 10)

    def boom(_path: str) -> object:
        raise ValueError("not a valid mp3 header")

    monkeypatch.setattr("mutagen.File", boom)
    assert resources.probe_longest_track_seconds([tmp_path / "a.mp3"]) is None
    assert resources.probe_longest_track_seconds([]) is None
    assert resources.probe_longest_track_seconds([tmp_path / "gone.mp3"]) is None


def test_probe_treats_a_zero_length_header_as_no_measurement(
    tmp_path, monkeypatch,
) -> None:
    """mutagen reports 0.0 for a header it couldn't parse — that is "unknown",
    not "a zero-second track"."""
    (tmp_path / "a.mp3").write_bytes(b"\0" * 10)

    class _Audio:
        def __init__(self, path: str) -> None:
            self.info = type("I", (), {"length": 0.0})()

    monkeypatch.setattr("mutagen.File", _Audio)
    assert resources.probe_longest_track_seconds([tmp_path / "a.mp3"]) is None


def test_a_missing_mutagen_keeps_the_old_behaviour(monkeypatch) -> None:
    """The probe is OPTIONAL: a stripped/frozen build without mutagen sizes
    exactly as it did before, it does not fail the run."""
    import builtins

    real_import = builtins.__import__

    def no_mutagen(name, *args, **kwargs):
        if name == "mutagen":
            raise ImportError("No module named 'mutagen'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_mutagen)
    assert resources.probe_longest_track_seconds(["whatever.mp3"]) is None


# ---------------------------------------------------------------------------
# The slider and the run must apply the SAME availability cap (R02)
# ---------------------------------------------------------------------------


def _wsl_slider_budget(available_mb: int | None) -> dict:
    """The `worker_budget` RPC as the Settings slider calls it: Windows sidecar,
    essentia_tf, a 16 GB WSL VM with `available_mb` free inside it."""
    from vibechek import resources, rpc, wsl

    fake_res = resources.SystemResources(
        platform="win", cpu_count=16, memory_total_mb=32768,
        memory_available_mb=24000, gpu_available=False,
    )
    with patch.object(resources, "detect", return_value=fake_res), \
         patch.object(resources, "_memory_available_mb", return_value=24000), \
         patch.object(wsl, "IS_WINDOWS", True), \
         patch.object(wsl, "wsl_vm_memory_mb", return_value=16384), \
         patch.object(wsl, "wsl_vm_available_mb", return_value=available_mb):
        return rpc._worker_budget({
            "engine": "essentia_tf", "genre_classifier": "discogs",
            "workers": 0, "distro": "Ubuntu-24.04",
        })


def test_slider_applies_the_availability_cap_to_the_wsl_vm_pool() -> None:
    """The run happens INSIDE the VM and always hands the budget its own psutil
    availability. The slider runs in the Windows sidecar, where the budget
    refuses to measure (correctly — this process sees the HOST), so without the
    VM-side reading the availability cap silently applied to the run only: the
    slider offered 8 workers and the run planned 5.
    """
    from vibechek.resources import compute_worker_budget

    slider = _wsl_slider_budget(5120)
    # The run's own call, with the same inputs — this is the invariant.
    run = compute_worker_budget(
        "essentia_tf", "discogs", 0,
        total_ram_mb=16384, free_vram_mb=None, gpu_registrable=None,
        cpu_count=16, under_wsl=True, use_gpu="off", ram_pool="wsl_vm",
        available_mb=5120,
    )
    assert slider["max_workers"] == run.max_workers
    assert slider["cap_reason"] == run.cap_reason
    # And the cap is real, not a coincidence of two uncapped answers.
    assert slider["max_workers"] < _wsl_slider_budget(None)["max_workers"]
    assert "free right now" in slider["cap_reason"]


def test_slider_falls_back_to_the_total_cap_when_the_vm_free_ram_is_unknown() -> None:
    """An old procps or a failed probe returns None, which must SKIP the cap —
    the behaviour before the reading existed — not be mistaken for zero free."""
    from vibechek.resources import compute_worker_budget

    slider = _wsl_slider_budget(None)
    run = compute_worker_budget(
        "essentia_tf", "discogs", 0,
        total_ram_mb=16384, free_vram_mb=None, gpu_registrable=None,
        cpu_count=16, under_wsl=True, use_gpu="off", ram_pool="wsl_vm",
        available_mb=None,
    )
    assert slider["max_workers"] == run.max_workers
    assert slider["max_workers"] > 0


def test_wsl_vm_probe_reads_total_and_available_from_one_free_call() -> None:
    """Both figures come out of the SAME `free -m` read: probing them separately
    would let the slider size a total against an availability taken a second
    later, and would double a ~1s wsl.exe round-trip."""
    from unittest.mock import MagicMock

    from vibechek import wsl

    proc = MagicMock(returncode=0, stdout="16384 5120\n")
    run = MagicMock(return_value=proc)
    with patch.object(wsl, "IS_WINDOWS", True), \
         patch.object(wsl, "_wsl_exe", return_value="wsl.exe"), \
         patch.object(wsl, "_wsl_run", run):
        wsl._WSL_VM_MEM_CACHE.clear()
        assert wsl.wsl_vm_memory_mb("Ubuntu-24.04") == 16384
        assert wsl.wsl_vm_available_mb("Ubuntu-24.04") == 5120
    wsl._WSL_VM_MEM_CACHE.clear()
    assert run.call_count == 1  # cached, not re-probed


def test_wsl_vm_probe_tolerates_a_free_without_an_available_column() -> None:
    """Ancient procps prints no `available`; a total with no availability is
    still worth having, and the missing half must read as "unknown"."""
    from unittest.mock import MagicMock

    from vibechek import wsl

    proc = MagicMock(returncode=0, stdout="16384\n")
    with patch.object(wsl, "IS_WINDOWS", True), \
         patch.object(wsl, "_wsl_exe", return_value="wsl.exe"), \
         patch.object(wsl, "_wsl_run", MagicMock(return_value=proc)):
        wsl._WSL_VM_MEM_CACHE.clear()
        assert wsl._wsl_vm_memory_probe("Ubuntu-24.04") == (16384, None)
    wsl._WSL_VM_MEM_CACHE.clear()
