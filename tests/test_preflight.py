"""Tests for vibechek.preflight."""

from __future__ import annotations

from pathlib import Path

import pytest

from vibechek import preflight as preflight_mod
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
    assert "essentia" in reasons[0].lower()


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
    )
    assert r.reasons_not_ready == []


def test_reasons_not_ready_flags_wsl_version_drift() -> None:
    """WSL has the engine but an OLD vibechek -> the analyzer's drift guard will
    reject it, so preflight must NOT call it ready; it surfaces an actionable
    'Update WSL install' reason. Regression: preflight reported 'Ready to
    analyze' while a real analyze failed with 'out of date'."""
    r = PreflightResult(
        ready=False,
        essentia=EssentiaCheck(installed=False, error="ImportError (host)"),
        models=ModelsCheck(models_dir="/x", found=["effnet"]),  # models present
        platform="Windows-10",
        wsl=_wsl_drifted("0.1.0"),  # far older than any current build
        analyze_via="wsl",
    )
    reasons = r.reasons_not_ready
    assert any("out of date" in x.lower() for x in reasons), reasons
    assert any("update wsl install" in x.lower() for x in reasons), reasons


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


def test_preflight_native_preferred_over_wsl(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        preflight_mod,
        "check_essentia",
        lambda: EssentiaCheck(installed=True, version="2.1"),
    )
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
