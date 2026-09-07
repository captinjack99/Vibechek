"""Tests for vibechek.preflight."""

from __future__ import annotations

from pathlib import Path

import pytest

from vibechek import preflight as preflight_mod
from vibechek.native_install import NativeVenvStatus
from vibechek.preflight import (
    EssentiaCheck,
    ModelsCheck,
    PreflightResult,
    check_models,
    preflight,
    summary_lines,
    to_dict,
)
from vibechek.wsl import DistroInfo, WSLStatus

# ---------------------------------------------------------------------------
# PreflightResult.reasons_not_ready
# ---------------------------------------------------------------------------


def _wsl_ready() -> WSLStatus:
    return WSLStatus(
        is_windows=True,
        wsl_available=True,
        wsl_feature_enabled=True,
        distros=[
            DistroInfo(
                name="Ubuntu-24.04",
                vibechek_installed=True,
                essentia_installed=True,
                is_default=True,
            )
        ],
    )


def _wsl_not_ready() -> WSLStatus:
    return WSLStatus(is_windows=True, wsl_available=False, wsl_feature_enabled=False)


def _wsl_drifted(version: str) -> WSLStatus:
    """WSL with vibechek+essentia present but pinned to a specific version."""
    return WSLStatus(
        is_windows=True,
        wsl_available=True,
        wsl_feature_enabled=True,
        distros=[
            DistroInfo(
                name="Ubuntu-24.04",
                vibechek_installed=True,
                essentia_installed=True,
                is_default=True,
                vibechek_version=version,
            )
        ],
    )


def test_reasons_not_ready_essentia_missing_no_wsl() -> None:
    r = PreflightResult(
        ready=False,
        essentia=EssentiaCheck(installed=False, error="No module named essentia"),
        models=ModelsCheck(models_dir="/x"),
        platform="Linux-5.0",
        wsl=None,
    )
    reasons = r.reasons_not_ready
    assert len(reasons) == 1
    assert "analysis engine" in reasons[0].lower()


def test_reasons_not_ready_essentia_missing_but_wsl_ready() -> None:
    """If WSL has vibechek+essentia, missing native essentia is NOT a blocker."""
    r = PreflightResult(
        ready=False,
        essentia=EssentiaCheck(installed=False, error="ImportError"),
        models=ModelsCheck(models_dir="/x"),
        platform="Windows-10",
        wsl=_wsl_ready(),
    )
    # Only models_missing should appear as a reason; essentia is covered by WSL.
    assert not any("essentia" in r.lower() for r in r.reasons_not_ready)


def test_reasons_not_ready_models_missing() -> None:
    r = PreflightResult(
        ready=False,
        essentia=EssentiaCheck(installed=True, version="2.1"),
        models=ModelsCheck(models_dir="/x", missing=["effnet", "genre_discogs400"]),
        platform="Linux-5.0",
        wsl=None,
        essentia_usable=True,  # essentia serves the engine in-process; only models missing
    )
    reasons = r.reasons_not_ready
    assert any("model" in r.lower() and "missing" in r.lower() for r in reasons)
    assert "2" in reasons[0]  # the count


def test_reasons_not_ready_both_problems() -> None:
    r = PreflightResult(
        ready=False,
        essentia=EssentiaCheck(installed=False, error="ImportError"),
        models=ModelsCheck(models_dir="/x", missing=["effnet"]),
        platform="Linux-5.0",
        wsl=_wsl_not_ready(),
    )
    assert len(r.reasons_not_ready) == 2


def test_reasons_not_ready_empty_when_all_good() -> None:
    r = PreflightResult(
        ready=True,
        essentia=EssentiaCheck(installed=True, version="2.1"),
        models=ModelsCheck(models_dir="/x", found=["effnet"]),
        platform="Linux-5.0",
        wsl=None,
        essentia_usable=True,  # essentia serves the engine in-process
    )
    assert r.reasons_not_ready == []


def test_wsl_version_drift_is_not_a_not_ready_reason() -> None:
    """An OLD WSL vibechek must NOT be surfaced as a 'not ready' reason: the
    analyzer auto-updates it in place on the next analyze (engine-aware,
    one-time). Regression: preflight used to flag 'out of date — Update WSL
    install' and block, which dead-ended every app upgrade / reinstall at a
    dialog AND shadowed the self-heal so it never ran (analyze_directory
    re-checks preflight and raised before reaching the auto-update guard)."""
    r = PreflightResult(
        ready=True,
        essentia=EssentiaCheck(installed=False, error="ImportError (host)"),
        models=ModelsCheck(models_dir="/x", found=["effnet"]),  # models present
        platform="Windows-10",
        wsl=_wsl_drifted("0.1.0"),  # far older than any current build
        analyze_via="wsl",
    )
    reasons = r.reasons_not_ready
    assert not any("out of date" in x.lower() for x in reasons), reasons
    assert not any("update wsl install" in x.lower() for x in reasons), reasons


def test_reasons_not_ready_no_drift_when_wsl_matches_sidecar() -> None:
    import vibechek  # noqa: PLC0415

    r = PreflightResult(
        ready=True,
        essentia=EssentiaCheck(installed=False, error="ImportError (host)"),
        models=ModelsCheck(models_dir="/x", found=["effnet"]),
        platform="Windows-10",
        wsl=_wsl_drifted(vibechek.__version__),  # same version -> not outdated
        analyze_via="wsl",
    )
    assert not any("out of date" in x.lower() for x in r.reasons_not_ready)


def test_reasons_not_ready_managed_venv_essentia_without_vibechek() -> None:
    """Managed venv has essentia but NOT vibechek -> NOT a usable engine.

    Regression: `reasons_not_ready` counted the managed venv as a present engine
    on `essentia_installed` alone, while `preflight()` requires essentia AND
    vibechek. That mismatch produced ready=False with an EMPTY reasons list (and
    the analyzer's 'Cannot analyze: .' message) when the vibechek install step
    failed after essentia was already in. The two have_engine definitions must
    agree: the managed venv only counts when both are installed.
    """
    r = PreflightResult(
        ready=False,
        essentia=EssentiaCheck(installed=False, error="ImportError (host)"),
        models=ModelsCheck(models_dir="/x", missing=[]),  # models present
        platform="Linux-5.0",
        wsl=None,
        native_venv=NativeVenvStatus(
            supported=True,
            venv_dir="/home/u/.vibechek/venv",
            essentia_installed=True,
            vibechek_installed=False,
        ),
    )
    reasons = r.reasons_not_ready
    assert reasons, "ready=False must always surface at least one reason"
    assert any("analysis engine" in x.lower() for x in reasons), reasons


def test_reasons_not_ready_managed_venv_fully_installed_is_clean() -> None:
    """Managed venv with BOTH essentia and vibechek counts as a present engine."""
    r = PreflightResult(
        ready=True,
        essentia=EssentiaCheck(installed=False, error="ImportError (host)"),
        models=ModelsCheck(models_dir="/x", found=["effnet"]),
        platform="Linux-5.0",
        wsl=None,
        native_venv=NativeVenvStatus(
            supported=True,
            venv_dir="/home/u/.vibechek/venv",
            essentia_installed=True,
            vibechek_installed=True,
        ),
        analyze_via="native_venv",
    )
    assert not any("not installed" in x.lower() for x in r.reasons_not_ready)


# ---------------------------------------------------------------------------
# probe_native_venv — essentia version parse (essentia_tf + plain/onnx venvs)
# ---------------------------------------------------------------------------


def _make_essentia_venv(venv_dir: Path, dist_info_name: str) -> None:
    """Lay out a minimal venv with an essentia*.dist-info dir.

    Uses the `Lib/site-packages` layout (the one probe_native_venv's
    site-packages glob actually traverses) so the test runs on the Windows dev
    box and CI alike.
    """
    (venv_dir / "bin").mkdir(parents=True, exist_ok=True)
    (venv_dir / "bin" / "python3").write_text("#!/bin/sh\n")
    sp = venv_dir / "Lib" / "site-packages"
    sp.mkdir(parents=True, exist_ok=True)
    (sp / dist_info_name).mkdir()


@pytest.mark.parametrize(
    ("dist_info", "expected"),
    [
        ("essentia_tensorflow-2.1b6.dev1110.dist-info", "2.1b6.dev1110"),
        # Regression: plain essentia (ONNX venv) has only ONE hyphen-token, so
        # the old `essentia[_-][^-]+-(...)` regex never matched and the version
        # was lost (Settings showed "Installed at ... ()").
        ("essentia-2.1b6.dev1110.dist-info", "2.1b6.dev1110"),
    ],
)
def test_probe_native_venv_parses_essentia_version(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    dist_info: str,
    expected: str,
) -> None:
    from vibechek import native_install as ni

    venv_dir = tmp_path / "venv"
    _make_essentia_venv(venv_dir, dist_info)

    # Force the module to treat this platform as supported and point at our venv.
    monkeypatch.setattr(ni, "IS_SUPPORTED", True)
    monkeypatch.setattr(ni, "_venv_dir", lambda engine="essentia_tf": venv_dir)

    status = ni.probe_native_venv()
    assert status.essentia_installed is True
    assert status.essentia_version == expected


# ---------------------------------------------------------------------------
# check_models — uses real filesystem on tmp_path
# ---------------------------------------------------------------------------


def test_check_models_dir_missing_returns_all_missing(tmp_path: Path) -> None:
    result = check_models(tmp_path / "does-not-exist")
    assert result.found == []
    assert len(result.missing) > 0  # at least the known models
    assert all(not c.present for c in result.per_model)


def test_check_models_empty_dir_marks_all_missing(tmp_path: Path) -> None:
    result = check_models(tmp_path)
    assert result.found == []
    assert len(result.missing) > 0


def test_check_models_zero_byte_pb_treated_as_missing(tmp_path: Path) -> None:
    from vibechek.analyzer import MODELS

    name = next(iter(MODELS.keys()))
    (tmp_path / f"{name}.pb").write_bytes(b"")  # 0 bytes
    (tmp_path / f"{name}.json").write_text("{}")

    result = check_models(tmp_path)
    assert name in result.missing


def test_check_models_present_when_pb_above_threshold(tmp_path: Path) -> None:
    from vibechek.analyzer import MODELS

    name = next(iter(MODELS.keys()))
    (tmp_path / f"{name}.pb").write_bytes(b"x" * 2048)  # > 1024
    (tmp_path / f"{name}.json").write_text("{}")

    result = check_models(tmp_path)
    assert name in result.found
    assert name not in result.missing


# ---------------------------------------------------------------------------
# preflight() — exercise the boolean matrix using monkey-patched dependencies
# ---------------------------------------------------------------------------


def test_preflight_ready_when_native_and_models_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        preflight_mod,
        "check_essentia",
        lambda: EssentiaCheck(installed=True, version="2.1"),
    )
    # essentia_tf needs a TensorFlow-capable build to run in-process — simulate one.
    monkeypatch.setattr(preflight_mod, "_essentia_has_tf_algos", lambda: True)
    monkeypatch.setattr(
        preflight_mod,
        "check_models",
        lambda _d=None, engine="essentia_tf": ModelsCheck(models_dir="/x", found=["effnet"]),
    )
    monkeypatch.setattr(
        preflight_mod,
        "detect_wsl",
        lambda quick=True, venv_subdir="venv": WSLStatus(False, False, False),
    )

    r = preflight()
    assert r.ready is True
    assert r.analyze_via == "native"
    assert r.essentia_usable is True


def test_preflight_native_engine_served_by_dsp_only_wheel(monkeypatch: pytest.MonkeyPatch) -> None:
    """engine='native' runs in-process on a DSP-only essentia (no TF algos) — the
    bundled Windows wheel case. The NumPy mel frontend replaces the missing
    TensorflowInputMusiCNN, so 'native' is served even though TF algos are absent."""
    monkeypatch.setattr(
        preflight_mod, "check_essentia",
        lambda: EssentiaCheck(installed=True, version="2.1b6.dev0"),
    )
    monkeypatch.setattr(preflight_mod, "_essentia_has_tf_algos", lambda: False)  # DSP-only wheel
    # native runs inference through onnxruntime — the bundled Windows onefile
    # always ships both, so simulate that here (see the F054 tests below).
    monkeypatch.setattr(preflight_mod, "_onnxruntime_importable", lambda: True)
    monkeypatch.setattr(
        preflight_mod, "check_models",
        lambda _d=None, engine="native": ModelsCheck(models_dir="/x", found=["effnet (onnx backbone)"]),
    )
    monkeypatch.setattr(
        preflight_mod, "detect_wsl",
        lambda quick=True, venv_subdir="venv-onnx": WSLStatus(False, False, False),
    )

    r = preflight(engine="native")
    assert r.ready is True
    assert r.analyze_via == "native"
    assert r.essentia_usable is True


def test_preflight_dsp_only_wheel_does_not_hijack_essentia_tf(monkeypatch: pytest.MonkeyPatch) -> None:
    """The bundled DSP-only wheel must NOT capture the default essentia_tf path:
    it lacks TensorFlow, so essentia_tf still routes through WSL, not in-process."""
    monkeypatch.setattr(
        preflight_mod, "check_essentia",
        lambda: EssentiaCheck(installed=True, version="2.1b6.dev0"),  # DSP-only wheel present
    )
    monkeypatch.setattr(preflight_mod, "_essentia_has_tf_algos", lambda: False)  # no TF build
    monkeypatch.setattr(
        preflight_mod, "check_models",
        lambda _d=None, engine="essentia_tf": ModelsCheck(models_dir="/x", found=["effnet"]),
    )
    monkeypatch.setattr(preflight_mod, "detect_wsl", lambda quick=True, venv_subdir="venv": _wsl_ready())

    r = preflight(engine="essentia_tf")
    assert r.ready is True
    assert r.analyze_via == "wsl"          # routed to WSL, NOT in-process native
    assert r.essentia_usable is False      # DSP-only wheel can't serve essentia_tf


def test_preflight_not_ready_when_no_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        preflight_mod,
        "check_essentia",
        lambda: EssentiaCheck(installed=False, error="ImportError"),
    )
    monkeypatch.setattr(
        preflight_mod,
        "check_models",
        lambda _d=None, engine="essentia_tf": ModelsCheck(models_dir="/x", found=["effnet"]),
    )
    monkeypatch.setattr(
        preflight_mod,
        "detect_wsl",
        lambda quick=True, venv_subdir="venv": WSLStatus(True, True, True),  # no distros
    )

    r = preflight()
    assert r.ready is False
    assert r.analyze_via is None


def test_preflight_via_wsl_when_only_wsl_has_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        preflight_mod,
        "check_essentia",
        lambda: EssentiaCheck(installed=False, error="ImportError"),
    )
    monkeypatch.setattr(
        preflight_mod,
        "check_models",
        lambda _d=None, engine="essentia_tf": ModelsCheck(models_dir="/x", found=["effnet"]),
    )
    monkeypatch.setattr(preflight_mod, "detect_wsl", lambda quick=True, venv_subdir="venv": _wsl_ready())

    r = preflight()
    assert r.ready is True
    assert r.analyze_via == "wsl"


def test_preflight_ready_when_wsl_engine_present_but_outdated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An OUTDATED WSL vibechek must still be 'ready' — the analyzer auto-updates
    it in place on first analyze. Regression for the silent upgrade trap: drift
    used to make preflight not-ready, so the GUI dead-ended at a dialog and the
    auto-update never got a chance to run."""
    monkeypatch.setattr(
        preflight_mod, "check_essentia",
        lambda: EssentiaCheck(installed=False, error="ImportError"),
    )
    monkeypatch.setattr(
        preflight_mod, "check_models",
        lambda _d=None, engine="essentia_tf": ModelsCheck(models_dir="/x", found=["effnet"]),
    )
    monkeypatch.setattr(
        preflight_mod, "detect_wsl",
        lambda quick=True, venv_subdir="venv": _wsl_drifted("0.1.0"),
    )
    r = preflight()
    assert r.ready is True
    assert r.analyze_via == "wsl"
    assert not any("out of date" in x.lower() for x in r.reasons_not_ready)


def test_preflight_native_preferred_over_wsl(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        preflight_mod,
        "check_essentia",
        lambda: EssentiaCheck(installed=True, version="2.1"),
    )
    monkeypatch.setattr(preflight_mod, "_essentia_has_tf_algos", lambda: True)  # TF-capable build
    monkeypatch.setattr(
        preflight_mod,
        "check_models",
        lambda _d=None, engine="essentia_tf": ModelsCheck(models_dir="/x", found=["effnet"]),
    )
    monkeypatch.setattr(preflight_mod, "detect_wsl", lambda quick=True, venv_subdir="venv": _wsl_ready())

    r = preflight()
    assert r.analyze_via == "native"


def test_preflight_models_missing_blocks_even_with_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        preflight_mod,
        "check_essentia",
        lambda: EssentiaCheck(installed=True, version="2.1"),
    )
    monkeypatch.setattr(preflight_mod, "_essentia_has_tf_algos", lambda: True)  # TF-capable build
    monkeypatch.setattr(
        preflight_mod,
        "check_models",
        lambda _d=None, engine="essentia_tf": ModelsCheck(models_dir="/x", missing=["effnet"]),
    )
    monkeypatch.setattr(
        preflight_mod,
        "detect_wsl",
        lambda quick=True, venv_subdir="venv": WSLStatus(False, False, False),
    )

    r = preflight()
    assert r.ready is False
    # analyze_via still tracks the engine availability, just not "ready"
    assert r.analyze_via == "native"


# ---------------------------------------------------------------------------
# to_dict + summary_lines smoke tests
# ---------------------------------------------------------------------------


def test_to_dict_includes_reasons_not_ready() -> None:
    r = PreflightResult(
        ready=False,
        essentia=EssentiaCheck(installed=False, error="ImportError"),
        models=ModelsCheck(models_dir="/x"),
        platform="Linux",
        wsl=None,
    )
    d = to_dict(r)
    assert "reasons_not_ready" in d
    assert isinstance(d["reasons_not_ready"], list)
    assert d["ready"] is False


def test_to_dict_includes_wsl_computed_props() -> None:
    r = PreflightResult(
        ready=True,
        essentia=EssentiaCheck(installed=True, version="2.1"),
        models=ModelsCheck(models_dir="/x", found=["effnet"]),
        platform="Windows",
        wsl=_wsl_ready(),
    )
    d = to_dict(r)
    assert d["wsl"]["can_run_vibechek"] is True
    assert d["wsl"]["usable_distro"] == "Ubuntu-24.04"


def test_summary_lines_ready_shows_ready() -> None:
    r = PreflightResult(
        ready=True,
        essentia=EssentiaCheck(installed=True, version="2.1"),
        models=ModelsCheck(models_dir="/x", found=["effnet"], total_size_mb=300.0),
        platform="Linux",
        wsl=None,
        analyze_via="native",
    )
    text = "\n".join(summary_lines(r))
    assert "READY" in text
    assert "native" in text


def test_summary_lines_not_ready_lists_problems() -> None:
    r = PreflightResult(
        ready=False,
        essentia=EssentiaCheck(installed=False, error="ImportError"),
        models=ModelsCheck(models_dir="/x", missing=["effnet"]),
        platform="Windows-10",
        wsl=_wsl_not_ready(),
    )
    text = "\n".join(summary_lines(r))
    assert "NOT READY" in text
    assert "NOT INSTALLED" in text


# ---------------------------------------------------------------------------
# onnxruntime is what the onnx/native engines actually infer through (audit
# F054). It's an optional extra, and the import only happens at model-load
# time — so preflight used to say READY for an engine that can't run a track.
# ---------------------------------------------------------------------------


def _onnx_env(monkeypatch: pytest.MonkeyPatch, *, onnxruntime: bool) -> None:
    """A Linux `pip install vibechek[ml]` box: essentia importable with TF
    algos, ONNX models downloaded, no WSL, no managed venv."""
    monkeypatch.setattr(
        preflight_mod, "check_essentia",
        lambda: EssentiaCheck(installed=True, version="2.1"),
    )
    monkeypatch.setattr(preflight_mod, "_essentia_has_tf_algos", lambda: True)
    monkeypatch.setattr(preflight_mod, "_onnxruntime_importable", lambda: onnxruntime)
    monkeypatch.setattr(
        preflight_mod, "check_models",
        lambda _d=None, engine="onnx": ModelsCheck(models_dir="/x", found=["effnet"]),
    )
    monkeypatch.setattr(
        preflight_mod, "detect_wsl",
        lambda quick=True, venv_subdir="venv-onnx": WSLStatus(False, False, False),
    )
    # preflight looks the probe up through the module at call time (so a stub
    # can never get frozen into it at import), hence the patch lands on
    # native_install, not on preflight.
    monkeypatch.setattr(
        preflight_mod.native_install, "probe_native_venv",
        lambda _engine="onnx": NativeVenvStatus(supported=True, venv_dir="/home/u/.vibechek/venv-onnx"),
    )


def test_onnx_not_ready_without_onnxruntime(monkeypatch: pytest.MonkeyPatch) -> None:
    """essentia + models present but no onnxruntime: analyze would raise
    'onnxruntime failed to load' inside every pool worker."""
    _onnx_env(monkeypatch, onnxruntime=False)

    r = preflight(engine="onnx")
    assert r.ready is False
    assert r.essentia_usable is False
    assert r.analyze_via is None
    assert r.onnxruntime_installed is False
    assert any("ONNX Runtime" in reason for reason in r.reasons_not_ready)
    assert "NOT READY" in summary_lines(r)[-1]


def test_onnx_ready_when_onnxruntime_importable(monkeypatch: pytest.MonkeyPatch) -> None:
    _onnx_env(monkeypatch, onnxruntime=True)

    r = preflight(engine="onnx")
    assert r.ready is True
    assert r.analyze_via == "native"
    assert r.onnxruntime_installed is True
    assert r.reasons_not_ready == []


def test_native_engine_also_needs_onnxruntime(monkeypatch: pytest.MonkeyPatch) -> None:
    """"native" is the same ONNX stack in-process — same requirement."""
    _onnx_env(monkeypatch, onnxruntime=False)

    r = preflight(engine="native")
    assert r.ready is False
    assert r.onnxruntime_installed is False


def test_essentia_tf_does_not_probe_onnxruntime(monkeypatch: pytest.MonkeyPatch) -> None:
    """The TF engine never touches onnxruntime; missing it must not block."""
    _onnx_env(monkeypatch, onnxruntime=False)

    r = preflight(engine="essentia_tf")
    assert r.ready is True
    assert r.analyze_via == "native"
    assert r.onnxruntime_installed is None


# ---------------------------------------------------------------------------
# check_models — the ONNX backbone's content pin
#
# The backbone is the first file the whole ONNX stack loads. rpc's
# verify_models and `vibechek verify-models` both check it against
# model_download.BACKBONE_ONNX_SHA256; preflight used to accept ANY file over
# 1 KB under that name, so a corrupt/tampered copy preflighted green and only
# blew up inside the worker pool at model-load time.
# ---------------------------------------------------------------------------


def _stage_onnx_models(root: Path, backbone_bytes: bytes) -> Path:
    """A models dir the `onnx` engine considers complete, backbone content given."""
    from vibechek.analyzer import _ONNX_HEAD_STEMS, _ONNX_SUBDIR
    from vibechek.onnx_backend import BACKBONE_ONNX_FILENAME

    onnx_dir = root / _ONNX_SUBDIR
    onnx_dir.mkdir(parents=True, exist_ok=True)
    (onnx_dir / BACKBONE_ONNX_FILENAME).write_bytes(backbone_bytes)
    # The class-label JSON is a WEIGHTS row here, so it faces the same
    # >1 KB "not truncated" threshold as the .onnx files.
    (onnx_dir / "genre_discogs400.json").write_text(
        '{"classes": [' + ",".join(f'"g{i}"' for i in range(400)) + "]}",
    )
    for stem in _ONNX_HEAD_STEMS:
        if stem == "genre_discogs400":
            continue
        (onnx_dir / f"{stem}.onnx").write_bytes(b"h" * 2048)
    return root


def test_onnx_backbone_row_carries_the_real_pin() -> None:
    """The pin in the model list is model_download's, and a genuine digest."""
    from vibechek.model_download import BACKBONE_ONNX_SHA256
    from vibechek.onnx_backend import BACKBONE_ONNX_FILENAME

    rows = preflight_mod._model_files_for_engine("onnx", Path("/models"))
    pins = {p.name: sha for _n, p, _m, _r, sha in rows}
    assert pins[BACKBONE_ONNX_FILENAME] == BACKBONE_ONNX_SHA256
    # A placeholder ("", "TODO", a short string) would make the check a no-op.
    assert len(BACKBONE_ONNX_SHA256) == 64
    assert all(c in "0123456789abcdef" for c in BACKBONE_ONNX_SHA256.lower())


def test_check_models_rejects_a_backbone_that_fails_its_pin(tmp_path: Path) -> None:
    """A big-enough-but-wrong backbone is reported like a missing one."""
    _stage_onnx_models(tmp_path, b"not the real backbone" * 200)

    result = check_models(tmp_path, engine="onnx")
    assert "effnet (onnx backbone)" in result.missing
    assert "effnet (onnx backbone)" not in result.found
    # Only the pinned file is faulted — the heads still read as present.
    assert "danceability" in result.found


def test_check_models_accepts_a_backbone_matching_its_pin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The happy path still passes — we re-pin to the staged bytes' digest.

    Staging the real 18 MB backbone in a unit test isn't practical, so pin the
    fixture instead: what's under test is that a MATCH is accepted, not the
    value of the constant (locked by
    `test_onnx_backbone_row_carries_the_real_pin`).
    """
    import hashlib

    payload = b"pretend backbone" * 200
    digest = hashlib.sha256(payload).hexdigest()
    monkeypatch.setattr(preflight_mod, "BACKBONE_ONNX_SHA256", digest)
    _stage_onnx_models(tmp_path, payload)

    result = check_models(tmp_path, engine="onnx")
    assert result.missing == []
    assert "effnet (onnx backbone)" in result.found


def test_check_models_unpinned_files_are_not_hashed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """essentia_tf's flat .pb set carries no pin here, so it never gets hashed.

    The GUI polls preflight on first render; silently adding a full-set hash to
    that path is a perf change, and the `.pb` digests are already enforced at
    download time and by `verify_models`.
    """
    from vibechek.analyzer import MODELS

    def _boom(_path: Path) -> str:
        raise AssertionError("check_models hashed an unpinned file")

    monkeypatch.setattr(preflight_mod, "_sha256_file", _boom)
    for name in MODELS:
        (tmp_path / f"{name}.pb").write_bytes(b"x" * 2048)
        (tmp_path / f"{name}.json").write_text("{}")

    result = check_models(tmp_path)
    assert result.missing == []
