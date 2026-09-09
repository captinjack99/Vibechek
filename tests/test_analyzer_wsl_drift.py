"""Tests for the WSL version-drift guard + stderr noise filter.

Both live in `vibechek.analyzer._analyze_via_wsl`. Together they fix the
"silent exit 1 with stderr full of MusicExtractorSVM noise" failure mode the
user hit on 2026-05-18: a pre-beta.1 WSL install was missing the worker cap,
spawned 19 essentia workers, OOM-crashed in seconds, and the bounded
80-line stderr buffer filled with per-worker essentia INFO chatter so the
real error never made it back to the GUI.

These tests exercise the small helpers and the dispatch decision; the full
analyze pipeline doesn't run here.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from vibechek import analyzer
from vibechek.errors import UserFacingError

# ---------------------------------------------------------------------------
# _normalize_version — canonical form check
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("0.4.0-beta.2", "0.4.0b2"),         # human form vs PEP 440
        ("0.4.0-beta.2", "0.4.0-beta.2"),    # identical
        ("0.4.0BETA2", "0.4.0-beta.2"),      # case + separators
        ("0.4.0a1", "0.4.0-alpha.1"),        # alpha collapse
        ("0.1.0.dev0", "0.1.0dev0"),         # dev suffix
    ],
)
def test_normalize_version_treats_equivalent_strings_as_equal(a: str, b: str) -> None:
    assert analyzer._normalize_version(a) == analyzer._normalize_version(b)


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("0.4.0-beta.2", "0.4.0-beta.1"),    # different beta numbers
        ("0.4.0", "0.4.0b2"),                # release vs beta
        ("0.1.0-dev", "0.4.0-beta.2"),       # the actual user-hit drift case
        ("0.4.0-beta.2", "0.5.0-beta.2"),
    ],
)
def test_normalize_version_keeps_distinct_versions_distinct(a: str, b: str) -> None:
    assert analyzer._normalize_version(a) != analyzer._normalize_version(b)


# ---------------------------------------------------------------------------
# Stderr noise filter — survival of the real error
# ---------------------------------------------------------------------------
#
# The filter is an internal regex defined inside `_analyze_via_wsl`. We
# duplicate the pattern here to assert the contract; if either copy drifts,
# we want this test to scream.


_NOISE = re.compile(
    r"MusicExtractorSVM:|"
    r"^\s*\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+: [IWE] tensorflow|"
    r"^Skipping registering GPU devices\.\.\.$|"
    r"^Your kernel may have been built without NUMA support\.$|"
    r"^pciBusID:|^coreClock:"
)


def test_noise_filter_drops_essentia_musicextractor_spam() -> None:
    """Per-worker essentia C++ INFO must not enter the bounded stderr tail."""
    assert _NOISE.search(
        "[   INFO   ] MusicExtractorSVM: no classifier models were configured by default"
    )


def test_noise_filter_drops_tf_gpu_init_lines() -> None:
    """TF's GPU dlopen / device-table chatter is also pure noise on failure."""
    assert _NOISE.search(
        "2026-05-18 18:57:50.121618: I tensorflow/core/common_runtime/gpu/gpu_device.cc:1258 "
        "Device interconnect StreamExecutor with strength 1 edge matrix:"
    )
    assert _NOISE.search("Skipping registering GPU devices...")
    assert _NOISE.search("pciBusID: 0000:01:00.0 name: NVIDIA GeForce RTX 4070")


def test_noise_filter_keeps_real_tracebacks() -> None:
    """Python tracebacks and our own VIBECHEK_WORKER_INIT_FAIL markers must pass."""
    survivors = [
        "Traceback (most recent call last):",
        '  File "/home/x/.vibechek/venv/lib/python3.12/site-packages/vibechek/analyzer.py", line 1, in <module>',
        "RuntimeError: Analyze stalled — no track completed in 300s.",
        "VIBECHEK_WORKER_INIT_FAIL: RuntimeError: essentia-tensorflow not installed",
        "Capping workers from 19 -> 17 based on available RAM",
        "Killed",  # OOM-killer's traditional output
    ]
    for line in survivors:
        assert not _NOISE.search(line), f"filter should not drop: {line!r}"


def test_noise_filter_bounded_tail_keeps_traceback_when_noise_dominates() -> None:
    """Simulate the user's actual failure mode: 200 noise lines + 5 real ones.

    Before the filter, the 80-line bounded tail filled with essentia INFO lines
    and the real error scrolled off. After the filter, the real error survives.
    """
    stderr_tail: list[str] = []
    real_error_lines = [
        "VIBECHEK_WORKER_INIT_FAIL: RuntimeError: essentia init crash",
        "Traceback (most recent call last):",
        '  File "x.py", line 1, in <module>',
        "    raise RuntimeError('boom')",
        "RuntimeError: boom",
    ]
    feed = (
        ["[   INFO   ] MusicExtractorSVM: no classifier models were configured by default"] * 200
        + real_error_lines
    )
    for line in feed:
        if not _NOISE.search(line):
            stderr_tail.append(line)
            if len(stderr_tail) > 80:
                stderr_tail.pop(0)

    assert stderr_tail == real_error_lines


# ---------------------------------------------------------------------------
# Version-drift guard — refuse to dispatch when WSL is stale
# ---------------------------------------------------------------------------


def _stub_preflight(version: str | None) -> MagicMock:
    """Build a fake preflight() return whose WSLStatus carries `version`."""
    distro = MagicMock(
        name="Ubuntu",
        vibechek_installed=True,
        essentia_installed=True,
        vibechek_version=version,
    )
    # MagicMock(name=...) sets *its own* repr, not the .name attribute.
    distro.name = "Ubuntu"
    wsl_status = MagicMock(usable_distro="Ubuntu", distros=[distro])
    pf = MagicMock(
        ready=True,
        analyze_via="wsl",
        wsl=wsl_status,
        reasons_not_ready=[],
    )
    return pf


def test_drift_auto_updates_wsl_in_place_then_analyzes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the WSL install lags the sidecar, analyze AUTO-UPDATES it in place
    (engine-aware) and then proceeds — instead of aborting and making the user
    re-run setup. (Old behavior raised "out of date".)"""
    import vibechek as _vibechek  # noqa: PLC0415
    monkeypatch.setattr(_vibechek, "__version__", "0.5.0-beta", raising=False)

    (tmp_path / "x.flac").write_bytes(b"\x00")
    output = tmp_path / "out.json"
    output.write_text('{"tracks": [], "status": "complete", "summary": {}}', encoding="utf-8")
    fake_run = MagicMock(returncode=0, stdout="", stderr="")
    fake_upgrade = MagicMock(return_value={"ok": True})

    fake_ensure = MagicMock(return_value={"ok": True, "healed": []})

    with patch("vibechek.preflight.preflight", return_value=_stub_preflight("0.1.0-dev")), \
         patch("vibechek.utils.find_audio_files", return_value=[tmp_path / "x.flac"]), \
         patch("vibechek.wsl.upgrade_vibechek_in_wsl", fake_upgrade), \
         patch("vibechek.wsl.ensure_engine_runtime", fake_ensure), \
         patch("vibechek.wsl.run_vibechek_in_wsl", return_value=fake_run), \
         patch("vibechek.wsl.win_to_wsl_path", side_effect=lambda s: s), \
         patch("vibechek.wsl.wsl_to_win_path", side_effect=lambda s: s):
        from vibechek.config import AnalysisConfig
        # Must NOT raise — the drift is auto-healed, not surfaced as an error.
        report = analyzer.analyze_directory(
            tmp_path,
            config=AnalysisConfig(workers=1, use_gpu="off", inference_engine="essentia_tf"),
            output_path=output,
        )

    # Auto-update fired exactly once, engine-aware, then analyze proceeded.
    assert fake_upgrade.call_count == 1
    assert fake_upgrade.call_args.kwargs.get("engine") == "essentia_tf"
    # DETECT → SELF-HEAL → RUN: the drift block also runs the engine-runtime
    # self-heal (engine-aware) before dispatching the analyze.
    assert fake_ensure.call_count == 1
    assert "essentia_tf" in fake_ensure.call_args.args or \
        fake_ensure.call_args.kwargs.get("engine") == "essentia_tf"
    assert report.get("status") == "complete"


def test_drift_auto_update_failure_surfaces_clean_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the in-place update itself fails, surface a clear repair error."""
    import vibechek as _vibechek  # noqa: PLC0415
    monkeypatch.setattr(_vibechek, "__version__", "0.5.0-beta", raising=False)
    (tmp_path / "x.flac").write_bytes(b"\x00")
    with patch("vibechek.preflight.preflight", return_value=_stub_preflight("0.1.0-dev")), \
         patch("vibechek.utils.find_audio_files", return_value=[tmp_path / "x.flac"]), \
         patch("vibechek.wsl.upgrade_vibechek_in_wsl",
               return_value={"ok": False, "error": "pip failed"}):
        from vibechek.config import AnalysisConfig
        # WP-H1: now a UserFacingError with a plain headline; the version/exit
        # detail (and the "Set up WSL" phantom pointer) are gone from the headline.
        with pytest.raises(UserFacingError) as ei:
            analyzer.analyze_directory(
                tmp_path, config=AnalysisConfig(workers=1, use_gpu="off"),
            )
        assert ei.value.kind == "fatal"
        assert "Couldn't update the analysis engine" in ei.value.headline
        assert "out of date" in (ei.value.detail or "")


def test_drift_stack_broken_upgrade_self_heals_then_analyzes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A code upgrade that reports `stack_broken` (ML deps skewed) must NOT
    dead-end: the drift block falls through to ensure_engine_runtime, which
    repairs the stack, and the analyze then proceeds."""
    import vibechek as _vibechek  # noqa: PLC0415
    monkeypatch.setattr(_vibechek, "__version__", "0.5.0-beta", raising=False)

    (tmp_path / "x.flac").write_bytes(b"\x00")
    output = tmp_path / "out.json"
    output.write_text('{"tracks": [], "status": "complete", "summary": {}}', encoding="utf-8")
    fake_run = MagicMock(returncode=0, stdout="", stderr="")
    # Upgrade succeeded code-wise but flagged an import-broken ML stack.
    fake_upgrade = MagicMock(return_value={"ok": False, "stack_broken": True,
                                           "error": "onnxruntime import failed"})
    fake_ensure = MagicMock(return_value={"ok": True, "healed": ["ml-stack"]})

    with patch("vibechek.preflight.preflight", return_value=_stub_preflight("0.1.0-dev")), \
         patch("vibechek.utils.find_audio_files", return_value=[tmp_path / "x.flac"]), \
         patch("vibechek.wsl.upgrade_vibechek_in_wsl", fake_upgrade), \
         patch("vibechek.wsl.ensure_engine_runtime", fake_ensure), \
         patch("vibechek.wsl.run_vibechek_in_wsl", return_value=fake_run), \
         patch("vibechek.wsl.win_to_wsl_path", side_effect=lambda s: s), \
         patch("vibechek.wsl.wsl_to_win_path", side_effect=lambda s: s):
        from vibechek.config import AnalysisConfig
        report = analyzer.analyze_directory(
            tmp_path,
            config=AnalysisConfig(workers=1, use_gpu="off", inference_engine="onnx"),
            output_path=output,
        )

    # stack_broken did NOT raise; the self-heal ran and the analyze completed.
    assert fake_ensure.call_count == 1
    assert report.get("status") == "complete"


def test_drift_self_heal_failure_surfaces_clean_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If ensure_engine_runtime can't repair the engine, surface a clear error
    instead of letting the analyze crash raw."""
    import vibechek as _vibechek  # noqa: PLC0415
    monkeypatch.setattr(_vibechek, "__version__", "0.5.0-beta", raising=False)
    (tmp_path / "x.flac").write_bytes(b"\x00")

    with patch("vibechek.preflight.preflight", return_value=_stub_preflight("0.1.0-dev")), \
         patch("vibechek.utils.find_audio_files", return_value=[tmp_path / "x.flac"]), \
         patch("vibechek.wsl.upgrade_vibechek_in_wsl", return_value={"ok": True}), \
         patch("vibechek.wsl.ensure_engine_runtime",
               return_value={"ok": False, "error": "still broken after reinstall"}):
        from vibechek.config import AnalysisConfig
        # WP-H1: a UserFacingError with a plain headline; the raw repair error is
        # demoted to detail. (This mock returns no headline, so the analyzer's
        # generic fallback headline is used.)
        with pytest.raises(UserFacingError) as ei:
            analyzer.analyze_directory(
                tmp_path,
                config=AnalysisConfig(workers=1, use_gpu="off", inference_engine="onnx"),
            )
        assert "isn't working" in ei.value.headline
        assert "still broken after reinstall" in (ei.value.detail or "")


def test_drift_guard_silent_when_versions_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No drift -> the guard must NOT raise; it should fall through to the
    real WSL launcher (which we stub so the test stays hermetic)."""
    # Patch the source-of-truth on the `vibechek` package, since
    # _analyze_via_wsl does `from vibechek import __version__` at call time.
    import vibechek as _vibechek  # noqa: PLC0415
    monkeypatch.setattr(_vibechek, "__version__", "0.4.0-beta.2", raising=False)

    (tmp_path / "x.flac").write_bytes(b"\x00")
    output = tmp_path / "out.json"
    output.write_text('{"tracks": [], "status": "complete", "summary": {}}', encoding="utf-8")

    fake_run = MagicMock(returncode=0, stdout="", stderr="")

    with patch("vibechek.preflight.preflight", return_value=_stub_preflight("0.4.0b2")), \
         patch("vibechek.utils.find_audio_files", return_value=[tmp_path / "x.flac"]), \
         patch("vibechek.wsl.run_vibechek_in_wsl", return_value=fake_run), \
         patch("vibechek.wsl.win_to_wsl_path", side_effect=lambda s: s), \
         patch("vibechek.wsl.wsl_to_win_path", side_effect=lambda s: s):
        from vibechek.config import AnalysisConfig
        # Direct the analyze to write to a known path so we can pre-populate it.
        analyzer.analyze_directory(
            tmp_path,
            config=AnalysisConfig(workers=1, use_gpu="off"),
            output_path=output,
        )


def test_drift_guard_skipped_when_probe_returned_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Best-effort: an old WSL install with no dist-info is a degraded probe,
    not a definite drift. We must not block the analyze in that case."""
    # Patch the source-of-truth on the `vibechek` package, since
    # _analyze_via_wsl does `from vibechek import __version__` at call time.
    import vibechek as _vibechek  # noqa: PLC0415
    monkeypatch.setattr(_vibechek, "__version__", "0.4.0-beta.2", raising=False)

    (tmp_path / "x.flac").write_bytes(b"\x00")
    output = tmp_path / "out.json"
    output.write_text('{"tracks": [], "status": "complete", "summary": {}}', encoding="utf-8")

    fake_run = MagicMock(returncode=0, stdout="", stderr="")

    with patch("vibechek.preflight.preflight", return_value=_stub_preflight(None)), \
         patch("vibechek.utils.find_audio_files", return_value=[tmp_path / "x.flac"]), \
         patch("vibechek.wsl.run_vibechek_in_wsl", return_value=fake_run), \
         patch("vibechek.wsl.win_to_wsl_path", side_effect=lambda s: s), \
         patch("vibechek.wsl.wsl_to_win_path", side_effect=lambda s: s):
        from vibechek.config import AnalysisConfig
        analyzer.analyze_directory(
            tmp_path,
            config=AnalysisConfig(workers=1, use_gpu="off"),
            output_path=output,
        )


# ---------------------------------------------------------------------------
# A memory refusal raised INSIDE the VM must reach the GUI with the one action
# only the Windows side can decide on (R01)
# ---------------------------------------------------------------------------


def _refusal_event(message: str, detail: str, classifier: str = "clap") -> str:
    """The stderr line the in-WSL analyzer emits before it refuses the run."""
    import json

    return analyzer.EVENT_PREFIX + "stage\t" + json.dumps({
        "name": analyzer.WORKER_BUDGET_REFUSED,
        "message": message,
        "detail": detail,
        "genre_classifier": classifier,
    })


def _run_refusing_child(host_total_mb: int | None, tmp_path: Path):  # noqa: ANN202
    """Drive `_analyze_via_wsl` against a child that refuses for lack of memory.

    `host_total_mb` is what psutil reports on the WINDOWS side — the figure the
    in-WSL child cannot see, and the only one that decides whether raising the
    VM's `memory=` limit can hand it anything.
    """
    (tmp_path / "x.flac").write_bytes(b"\x00")
    output = tmp_path / "out.json"

    def _fake_run(distro, args, on_stderr_line=None, venv_subdir=None):  # noqa: ANN001, ANN202, ARG001
        on_stderr_line(_refusal_event(
            "Not enough memory to run the advanced genre model right now.",
            "the advanced genre model (CLAP) needs about 4.5 GB per worker, but "
            "the Linux analysis environment (WSL) has only 4.0 GB usable. "
            "Fixes: switch to the standard genre model, or give the Linux "
            "analysis environment more memory (its limit lives in .wslconfig).",
        ))
        on_stderr_line("vibechek.errors.UserFacingError: Not enough memory")
        return MagicMock(returncode=1, stdout="", stderr="")

    with patch("vibechek.preflight.preflight", return_value=_stub_preflight(None)), \
         patch("vibechek.utils.find_audio_files", return_value=[tmp_path / "x.flac"]), \
         patch("vibechek.wsl.run_vibechek_in_wsl", side_effect=_fake_run), \
         patch("vibechek.wsl.win_to_wsl_path", side_effect=lambda s: s), \
         patch("vibechek.wsl.wsl_to_win_path", side_effect=lambda s: s), \
         patch.object(analyzer, "_host_total_ram_mb", return_value=host_total_mb):
        from vibechek.config import AnalysisConfig
        with pytest.raises(UserFacingError) as ei:
            analyzer.analyze_directory(
                tmp_path,
                config=AnalysisConfig(workers=1, use_gpu="off",
                                      genre_classifier="clap"),
                output_path=output,
            )
    return ei.value


def test_wsl_memory_refusal_offers_the_bump_on_a_pc_that_can_give_more(
    tmp_path: Path,
) -> None:
    """The self-heal button was structurally unreachable.

    The refusal is raised inside the WSL VM, where psutil reports the VM's own
    limit — so the child correctly withholds "Increase memory" (it cannot tell
    whether a bump has anything to give), and the child's exception then dies
    with its process. All the Windows parent kept was exit 1 and a stderr tail,
    which it reported as a generic "Analysis stopped unexpectedly". Result: no
    headline, no detail, and `can_increase_memory` false on every code path in
    the repo. The parent now rebuilds the refusal with the HOST figure it can
    actually measure.
    """
    err = _run_refusing_child(32768, tmp_path)
    assert err.headline == "Not enough memory to run the advanced genre model right now."
    assert ".wslconfig" in (err.detail or "")
    assert err.kind == "fatal"
    assert err.options["can_increase_memory"] is True
    assert err.options["can_switch_classifier"] is True
    # And it survives serialization to the shell, which is where the buttons
    # are keyed off it.
    assert err.to_error_data()["can_increase_memory"] is True


def test_wsl_memory_refusal_still_withholds_the_bump_on_a_small_pc(
    tmp_path: Path,
) -> None:
    """16 GB host: `bump_wslconfig_memory` has nothing to raise, so offering the
    button would send the user through a self-heal that can only answer "there's
    nothing to give" — on the screen that just refused their run."""
    err = _run_refusing_child(16384, tmp_path)
    assert err.options["can_increase_memory"] is False
    assert err.options["can_switch_classifier"] is True


def test_wsl_memory_refusal_withholds_the_bump_when_the_host_cannot_be_measured(
    tmp_path: Path,
) -> None:
    """No psutil in a stripped build: withhold rather than promise a bump we
    can't verify. The detail still names the .wslconfig fix."""
    err = _run_refusing_child(None, tmp_path)
    assert err.options["can_increase_memory"] is False
    assert ".wslconfig" in (err.detail or "")


def test_a_child_failure_that_is_not_a_memory_refusal_is_unchanged(
    tmp_path: Path,
) -> None:
    """Only the refusal event re-shapes the error; every other exit 1 keeps the
    generic retryable message with its stderr tail."""
    (tmp_path / "x.flac").write_bytes(b"\x00")

    def _fake_run(distro, args, on_stderr_line=None, venv_subdir=None):  # noqa: ANN001, ANN202, ARG001
        on_stderr_line("RuntimeError: something else entirely")
        return MagicMock(returncode=1, stdout="", stderr="")

    with patch("vibechek.preflight.preflight", return_value=_stub_preflight(None)), \
         patch("vibechek.utils.find_audio_files", return_value=[tmp_path / "x.flac"]), \
         patch("vibechek.wsl.run_vibechek_in_wsl", side_effect=_fake_run), \
         patch("vibechek.wsl.win_to_wsl_path", side_effect=lambda s: s), \
         patch("vibechek.wsl.wsl_to_win_path", side_effect=lambda s: s):
        from vibechek.config import AnalysisConfig
        with pytest.raises(UserFacingError) as ei:
            analyzer.analyze_directory(
                tmp_path, config=AnalysisConfig(workers=1, use_gpu="off"),
                output_path=tmp_path / "out.json",
            )
    assert "Analysis stopped unexpectedly" in ei.value.headline
    assert ei.value.kind == "retryable"
    assert "something else entirely" in (ei.value.detail or "")
    assert ei.value.options == {}
