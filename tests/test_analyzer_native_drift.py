"""Tests for the managed-venv pre-run self-heal (WP-G2 — WSL parity).

Mirrors tests/test_analyzer_wsl_drift.py's self-heal cases for the managed
Linux/macOS venv path: `native_install.ensure_native_engine_runtime` (the
analog of `wsl.ensure_engine_runtime`) and its integration into
`analyzer._analyze_via_native_venv` — DETECT → SELF-HEAL → RUN before every
dispatch, honest structured failure when repair can't fix it, and the
VIBECHEK_NO_AUTOHEAL opt-out.

These tests exercise the decision logic with the probe/installer stubbed; the
real pip install never runs here.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from vibechek import analyzer, cancellation, native_install
from vibechek.errors import UserFacingError

# ---------------------------------------------------------------------------
# ensure_native_engine_runtime — DETECT → SELF-HEAL → RUN decision logic
# ---------------------------------------------------------------------------


def _enable_native(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Force IS_SUPPORTED on (tests run on Windows too) + a tmp venv root."""
    monkeypatch.setattr(native_install, "IS_SUPPORTED", True)
    monkeypatch.setattr(native_install, "VENV_DIR", tmp_path / "venv")
    monkeypatch.delenv("VIBECHEK_NO_AUTOHEAL", raising=False)


def test_ensure_healthy_stack_skips_reinstall(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A venv whose ML stack imports must NOT be reinstalled (probe-only)."""
    _enable_native(monkeypatch, tmp_path)
    fake_install = MagicMock()
    monkeypatch.setattr(
        native_install, "_probe_native_stack_import", lambda engine: (True, ""),
    )
    monkeypatch.setattr(native_install, "install_essentia_native", fake_install)

    res = native_install.ensure_native_engine_runtime("essentia_tf")

    assert res["ok"] is True
    assert res["healed"] == []
    fake_install.assert_not_called()


def test_ensure_broken_stack_reinstalls_once_then_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Import-broken stack → ONE engine-aware reinstall → re-probe OK → healed."""
    _enable_native(monkeypatch, tmp_path)
    probe = MagicMock(side_effect=[
        (False, "libcudart.so.13: cannot open shared object file"),
        (True, ""),
    ])
    fake_install = MagicMock(return_value={"ok": True})
    monkeypatch.setattr(native_install, "_probe_native_stack_import", probe)
    monkeypatch.setattr(native_install, "install_essentia_native", fake_install)

    res = native_install.ensure_native_engine_runtime("onnx")

    assert res["ok"] is True
    assert res["healed"] == ["ml-stack"]
    fake_install.assert_called_once()
    assert fake_install.call_args.kwargs.get("engine") == "onnx"


def test_ensure_autoheal_disabled_reports_honestly_without_reinstall(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """VIBECHEK_NO_AUTOHEAL suppresses the repair but NOT the detection: the
    broken stack comes back as a structured failure, and pip never runs."""
    _enable_native(monkeypatch, tmp_path)
    monkeypatch.setenv("VIBECHEK_NO_AUTOHEAL", "1")
    fake_install = MagicMock()
    monkeypatch.setattr(
        native_install, "_probe_native_stack_import",
        lambda engine: (False, "ImportError: no module named essentia"),
    )
    monkeypatch.setattr(native_install, "install_essentia_native", fake_install)

    res = native_install.ensure_native_engine_runtime("essentia_tf")

    assert res["ok"] is False
    assert res["autoheal_disabled"] is True
    assert res["kind"] == "fatal"
    assert "automatic repair is" in res["headline"].lower()
    # The real import error is DEMOTED to detail, never lost.
    assert "no module named essentia" in res["detail"]
    fake_install.assert_not_called()


def test_ensure_reinstall_failure_is_honest_and_loop_guarded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed reinstall surfaces the headline/detail/kind trio and does NOT
    retry (no second install, no re-probe of a venv that was never rebuilt)."""
    _enable_native(monkeypatch, tmp_path)
    probe = MagicMock(return_value=(False, "dlopen failed"))
    fake_install = MagicMock(return_value={"ok": False, "error": "pip exited 1"})
    monkeypatch.setattr(native_install, "_probe_native_stack_import", probe)
    monkeypatch.setattr(native_install, "install_essentia_native", fake_install)

    res = native_install.ensure_native_engine_runtime("essentia_tf")

    assert res["ok"] is False
    assert res["kind"] == "fatal"
    assert "couldn't be repaired" in res["headline"]
    assert "pip exited 1" in res["detail"]
    fake_install.assert_called_once()
    probe.assert_called_once()  # loop guard: no re-probe after a failed install


def test_ensure_reinstall_failure_passes_through_installer_trio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """install_essentia_native's verify failure already ships a plain headline
    (the WP-J1 clean-reinstall exhausted) — reuse it rather than re-wording."""
    _enable_native(monkeypatch, tmp_path)
    monkeypatch.setattr(
        native_install, "_probe_native_stack_import",
        lambda engine: (False, "dlopen failed"),
    )
    monkeypatch.setattr(
        native_install, "install_essentia_native",
        MagicMock(return_value={
            "ok": False,
            "error": "Install completed but verification failed.",
            "kind": "fatal",
            "headline": "Setup finished, but the analysis engine still isn't working.",
            "detail": "import essentia → rc=1",
        }),
    )

    res = native_install.ensure_native_engine_runtime("essentia_tf")

    assert res["ok"] is False
    assert res["headline"] == (
        "Setup finished, but the analysis engine still isn't working."
    )
    assert res["detail"] == "import essentia → rc=1"


def test_ensure_still_broken_after_reinstall_fails_after_single_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reinstall 'succeeds' but the stack still won't import → honest fatal
    error after exactly ONE install attempt (the loop guard)."""
    _enable_native(monkeypatch, tmp_path)
    probe = MagicMock(side_effect=[(False, "before"), (False, "still broken after")])
    fake_install = MagicMock(return_value={"ok": True})
    monkeypatch.setattr(native_install, "_probe_native_stack_import", probe)
    monkeypatch.setattr(native_install, "install_essentia_native", fake_install)

    res = native_install.ensure_native_engine_runtime("essentia_tf")

    assert res["ok"] is False
    assert res["kind"] == "fatal"
    assert "still isn't working" in res["headline"]
    assert "still broken after" in res["detail"]
    fake_install.assert_called_once()
    assert probe.call_count == 2


def test_ensure_cancelled_install_propagates_cancelled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_native(monkeypatch, tmp_path)
    monkeypatch.setattr(
        native_install, "_probe_native_stack_import",
        lambda engine: (False, "broken"),
    )
    monkeypatch.setattr(
        native_install, "install_essentia_native",
        MagicMock(return_value={"ok": False, "cancelled": True,
                                "error": "Cancelled by user"}),
    )

    res = native_install.ensure_native_engine_runtime("essentia_tf")

    assert res["ok"] is False
    assert res["cancelled"] is True


def test_ensure_is_a_noop_on_unsupported_platforms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On Windows (WSL owns self-heal there) ensure must not probe anything."""
    monkeypatch.setattr(native_install, "IS_SUPPORTED", False)
    probe = MagicMock()
    monkeypatch.setattr(native_install, "_probe_native_stack_import", probe)

    res = native_install.ensure_native_engine_runtime("essentia_tf")

    assert res["ok"] is True
    assert res.get("skipped") == "unsupported-platform"
    probe.assert_not_called()


# ---------------------------------------------------------------------------
# _probe_native_stack_import — definite negatives vs inconclusive
# ---------------------------------------------------------------------------


def test_probe_missing_interpreter_is_a_definite_negative(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No python in the venv (the dangling-symlink post-upgrade state) must
    report broken — this is exactly the state the self-heal exists for."""
    monkeypatch.setattr(native_install, "VENV_DIR", tmp_path / "venv")
    ok, detail = native_install._probe_native_stack_import("essentia_tf")
    assert ok is False
    assert "no working Python interpreter" in detail


def test_probe_engine_selects_the_right_imports() -> None:
    """onnx/native must prove onnxruntime imports (the CUDA-skew crash site);
    essentia_tf only needs essentia."""
    assert "onnxruntime" in native_install._native_stack_imports("onnx")
    assert "onnxruntime" in native_install._native_stack_imports("native")
    assert native_install._native_stack_imports("essentia_tf") == "import essentia"


# ---------------------------------------------------------------------------
# _analyze_via_native_venv — the pre-dispatch DETECT → SELF-HEAL → RUN wiring
# ---------------------------------------------------------------------------


def _stub_native_preflight() -> MagicMock:
    """A preflight() result that routes analyze through the managed venv."""
    return MagicMock(ready=True, analyze_via="native_venv", reasons_not_ready=[])


def _write_report(path: Path) -> None:
    path.write_text(
        '{"tracks": [], "status": "complete", "summary": {}}', encoding="utf-8",
    )


def test_native_dispatch_heals_first_then_analyzes_and_reports_it(
    tmp_path: Path,
) -> None:
    """The self-heal runs BEFORE the venv dispatch (engine-aware), and a repair
    is surfaced on the finished report as runtime_healed — not swallowed."""
    (tmp_path / "x.flac").write_bytes(b"\x00")
    output = tmp_path / "out.json"
    _write_report(output)

    calls: list[str] = []

    def fake_ensure(engine, on_progress=None):
        calls.append("heal")
        return {"ok": True, "healed": ["ml-stack"]}

    def fake_run(args, on_stderr_line=None, engine="essentia_tf"):
        calls.append("run")
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch("vibechek.preflight.preflight", return_value=_stub_native_preflight()), \
         patch("vibechek.utils.find_audio_files", return_value=[tmp_path / "x.flac"]), \
         patch("vibechek.native_install.ensure_native_engine_runtime",
               side_effect=fake_ensure) as ensure_mock, \
         patch("vibechek.native_install.run_vibechek_in_native_venv",
               side_effect=fake_run):
        from vibechek.config import AnalysisConfig
        report = analyzer.analyze_directory(
            tmp_path,
            config=AnalysisConfig(workers=1, use_gpu="off",
                                  inference_engine="essentia_tf"),
            output_path=output,
        )

    assert calls == ["heal", "run"]  # DETECT → SELF-HEAL strictly before RUN
    assert ensure_mock.call_args.args[0] == "essentia_tf"  # engine-aware
    assert report.get("status") == "complete"
    assert "repaired automatically" in report.get("runtime_healed", "")


def test_native_dispatch_healthy_engine_adds_no_notice(tmp_path: Path) -> None:
    """healed=[] (nothing was wrong) must not fabricate a repair notice."""
    (tmp_path / "x.flac").write_bytes(b"\x00")
    output = tmp_path / "out.json"
    _write_report(output)

    fake_run = MagicMock(return_value=MagicMock(returncode=0, stdout="", stderr=""))

    with patch("vibechek.preflight.preflight", return_value=_stub_native_preflight()), \
         patch("vibechek.utils.find_audio_files", return_value=[tmp_path / "x.flac"]), \
         patch("vibechek.native_install.ensure_native_engine_runtime",
               return_value={"ok": True, "healed": []}), \
         patch("vibechek.native_install.run_vibechek_in_native_venv", fake_run):
        from vibechek.config import AnalysisConfig
        report = analyzer.analyze_directory(
            tmp_path,
            config=AnalysisConfig(workers=1, use_gpu="off",
                                  inference_engine="essentia_tf"),
            output_path=output,
        )

    assert "runtime_healed" not in report
    fake_run.assert_called_once()


def test_native_heal_failure_surfaces_clean_error_and_skips_dispatch(
    tmp_path: Path,
) -> None:
    """If the repair can't fix the venv, raise the structured error (generic
    fallback headline when the heal result carries none) and never dispatch."""
    (tmp_path / "x.flac").write_bytes(b"\x00")
    fake_run = MagicMock()

    with patch("vibechek.preflight.preflight", return_value=_stub_native_preflight()), \
         patch("vibechek.utils.find_audio_files", return_value=[tmp_path / "x.flac"]), \
         patch("vibechek.native_install.ensure_native_engine_runtime",
               return_value={"ok": False, "error": "still broken after reinstall"}), \
         patch("vibechek.native_install.run_vibechek_in_native_venv", fake_run):
        from vibechek.config import AnalysisConfig
        with pytest.raises(UserFacingError) as ei:
            analyzer.analyze_directory(
                tmp_path,
                config=AnalysisConfig(workers=1, use_gpu="off",
                                      inference_engine="essentia_tf"),
            )

    assert ei.value.kind == "fatal"
    assert "isn't working" in ei.value.headline
    assert "still broken after reinstall" in (ei.value.detail or "")
    fake_run.assert_not_called()


def test_native_heal_failure_uses_the_heal_results_own_trio(
    tmp_path: Path,
) -> None:
    """When ensure returns headline/detail/kind (WP-H style), the analyzer must
    pass them through verbatim instead of substituting its fallback."""
    (tmp_path / "x.flac").write_bytes(b"\x00")

    heal = {
        "ok": False,
        "kind": "fatal",
        "headline": "The analysis engine can't start, and automatic repair is turned off.",
        "detail": "VIBECHEK_NO_AUTOHEAL is set.",
    }
    with patch("vibechek.preflight.preflight", return_value=_stub_native_preflight()), \
         patch("vibechek.utils.find_audio_files", return_value=[tmp_path / "x.flac"]), \
         patch("vibechek.native_install.ensure_native_engine_runtime",
               return_value=heal):
        from vibechek.config import AnalysisConfig
        with pytest.raises(UserFacingError) as ei:
            analyzer.analyze_directory(
                tmp_path,
                config=AnalysisConfig(workers=1, use_gpu="off",
                                      inference_engine="essentia_tf"),
            )

    assert ei.value.headline == heal["headline"]
    assert ei.value.detail == heal["detail"]
    assert ei.value.kind == "fatal"


def test_native_heal_cancellation_raises_cancelled(tmp_path: Path) -> None:
    """A user cancel during the repair must surface as CancelledError (the
    normal cancel path), not as an analysis failure."""
    (tmp_path / "x.flac").write_bytes(b"\x00")
    fake_run = MagicMock()

    with patch("vibechek.preflight.preflight", return_value=_stub_native_preflight()), \
         patch("vibechek.utils.find_audio_files", return_value=[tmp_path / "x.flac"]), \
         patch("vibechek.native_install.ensure_native_engine_runtime",
               return_value={"ok": False, "cancelled": True,
                             "error": "Cancelled by user"}), \
         patch("vibechek.native_install.run_vibechek_in_native_venv", fake_run):
        from vibechek.config import AnalysisConfig
        with pytest.raises(cancellation.CancelledError):
            analyzer.analyze_directory(
                tmp_path,
                config=AnalysisConfig(workers=1, use_gpu="off",
                                      inference_engine="essentia_tf"),
            )

    fake_run.assert_not_called()
