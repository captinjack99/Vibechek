"""Readiness check for ML analysis.

Run before any `analyze` operation. Catches the two failure modes that would
otherwise hang multiprocessing.Pool's worker init: missing essentia install,
missing model files.

Returns a structured result instead of raising, so the GUI can present
actionable next steps rather than a stack trace.
"""

from __future__ import annotations

import logging
import platform
from dataclasses import asdict, dataclass, field
from pathlib import Path

from vibechek.analyzer import MODELS
from vibechek.config import MODELS_DIR
from vibechek.wsl import WSLStatus, detect_wsl

log = logging.getLogger(__name__)


@dataclass
class EssentiaCheck:
    installed: bool
    version: str | None = None
    error: str | None = None  # The ImportError message, when not installed


@dataclass
class ModelCheck:
    name: str
    present: bool
    weights_path: str
    metadata_path: str
    size_mb: float = 0.0


@dataclass
class ModelsCheck:
    models_dir: str
    found: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    total_size_mb: float = 0.0
    per_model: list[ModelCheck] = field(default_factory=list)


@dataclass
class PreflightResult:
    ready: bool
    essentia: EssentiaCheck
    models: ModelsCheck
    platform: str
    # WSL status — only meaningful on Windows. On other OSes wsl.is_windows
    # is False and the rest can be ignored.
    wsl: WSLStatus | None = None
    # How analyze will actually run when ready=True: "native" or "wsl"
    analyze_via: str | None = None

    @property
    def reasons_not_ready(self) -> list[str]:
        out: list[str] = []
        if not self.essentia.installed and not (self.wsl and self.wsl.can_run_vibechek):
            out.append("essentia-tensorflow is not installed (native or in WSL)")
        if self.models.missing:
            out.append(f"{len(self.models.missing)} ML model file(s) missing")
        return out


def check_essentia() -> EssentiaCheck:
    """Try to import essentia and report what we see."""
    try:
        import essentia  # noqa: PLC0415
        version = getattr(essentia, "__version__", None)
        return EssentiaCheck(installed=True, version=version)
    except ImportError as e:
        return EssentiaCheck(installed=False, error=str(e))
    except Exception as e:  # noqa: BLE001
        # essentia sometimes raises non-ImportError on broken installs
        return EssentiaCheck(installed=False, error=f"{type(e).__name__}: {e}")


def check_models(models_dir: Path | None = None) -> ModelsCheck:
    """Verify every required ML model file is present and non-trivial in size."""
    target = Path(models_dir or MODELS_DIR)
    result = ModelsCheck(models_dir=str(target))

    if not target.exists():
        # All models missing
        for name, (subdir, weights_name, metadata_name) in MODELS.items():
            result.per_model.append(ModelCheck(
                name=name,
                present=False,
                weights_path=str(target / f"{name}.pb"),
                metadata_path=str(target / f"{name}.json"),
            ))
            result.missing.append(name)
        return result

    for name, (subdir, weights_name, metadata_name) in MODELS.items():
        weights = target / f"{name}.pb"
        metadata = target / f"{name}.json"

        # A 0-byte .pb is broken; treat as missing
        weights_ok = weights.exists() and weights.stat().st_size > 1024
        metadata_ok = metadata.exists() and metadata.stat().st_size > 0
        size_mb = weights.stat().st_size / (1024 * 1024) if weights.exists() else 0.0

        check = ModelCheck(
            name=name,
            present=weights_ok and metadata_ok,
            weights_path=str(weights),
            metadata_path=str(metadata),
            size_mb=round(size_mb, 1),
        )
        result.per_model.append(check)
        result.total_size_mb += size_mb
        (result.found if check.present else result.missing).append(name)

    result.total_size_mb = round(result.total_size_mb, 1)
    return result


def preflight(models_dir: Path | None = None) -> PreflightResult:
    """Full check; ready=True iff analyze can actually run.

    "Ready" means EITHER:
      - Native essentia is installed AND models are present, OR
      - We're on Windows, WSL has vibechek + essentia, AND models are present
        (models live on the Windows side and are mounted into WSL via /mnt/c)
    """
    essentia = check_essentia()
    models = check_models(models_dir)
    # Quick WSL check: skip per-distro probes so preflight always returns
    # in under a second. The GUI calls `wsl_status` separately for the
    # full probe with a loading indicator.
    wsl_status = detect_wsl(quick=True)

    have_native = essentia.installed
    have_wsl = wsl_status.can_run_vibechek
    have_engine = have_native or have_wsl

    ready = have_engine and not models.missing
    analyze_via = "native" if have_native else ("wsl" if have_wsl else None)

    return PreflightResult(
        ready=ready,
        essentia=essentia,
        models=models,
        platform=platform.platform(),
        wsl=wsl_status,
        analyze_via=analyze_via,
    )


def to_dict(r: PreflightResult) -> dict:
    """JSON-serializable form, including derived reasons."""
    d = asdict(r)
    d["reasons_not_ready"] = r.reasons_not_ready
    # Hand-fill the WSL convenience properties (asdict won't include @property)
    if r.wsl is not None:
        d["wsl"]["can_run_vibechek"] = r.wsl.can_run_vibechek
        d["wsl"]["usable_distro"] = r.wsl.usable_distro
    return d


# ---------------------------------------------------------------------------
# Pretty CLI summary
# ---------------------------------------------------------------------------


def summary_lines(r: PreflightResult) -> list[str]:
    lines: list[str] = []
    lines.append("Vibechek preflight")
    lines.append("")
    lines.append("Essentia:")
    if r.essentia.installed:
        lines.append(f"  OK (version: {r.essentia.version or 'unknown'})")
    else:
        lines.append(f"  NOT INSTALLED")
        lines.append(f"  {r.essentia.error}")
        if "win" in r.platform.lower():
            lines.append("  Windows: essentia-tensorflow has no official wheel.")
            lines.append("  Run Vibechek inside WSL Ubuntu, or skip `analyze` (other commands still work).")
        else:
            lines.append("  Install with: pip install essentia-tensorflow")

    lines.append("")
    lines.append(f"Models ({r.models.models_dir}):")
    if not r.models.missing:
        lines.append(f"  OK ({len(r.models.found)} models, {r.models.total_size_mb:.0f} MB)")
    else:
        lines.append(f"  {len(r.models.missing)} of {len(r.models.found) + len(r.models.missing)} missing:")
        for name in r.models.missing[:8]:
            lines.append(f"    - {name}")
        if len(r.models.missing) > 8:
            lines.append(f"    ... and {len(r.models.missing) - 8} more")
        lines.append("  Run: vibechek download-models")

    if r.wsl and r.wsl.is_windows:
        lines.append("")
        lines.append("WSL (Windows fallback for analyze):")
        if not r.wsl.wsl_available:
            lines.append("  wsl.exe not on PATH")
        elif not r.wsl.wsl_feature_enabled:
            lines.append("  feature disabled; the GUI can install it for you")
        elif not r.wsl.distros:
            lines.append("  feature on, no distros installed yet")
        elif r.wsl.can_run_vibechek:
            lines.append(f"  OK ({r.wsl.usable_distro} has vibechek + essentia)")
        else:
            for d in r.wsl.distros:
                bits = []
                if d.vibechek_installed: bits.append("vibechek")
                if d.essentia_installed: bits.append("essentia")
                status = ", ".join(bits) if bits else "neither installed"
                lines.append(f"  - {d.name}: {status}")
            lines.append("  Run the GUI installer or `pip install essentia-tensorflow vibechek` inside your distro.")

    lines.append("")
    if r.ready:
        lines.append(f"READY (will analyze via: {r.analyze_via})")
    else:
        lines.append("NOT READY (cannot run `analyze`)")
    return lines


__all__ = [
    "EssentiaCheck",
    "ModelCheck",
    "ModelsCheck",
    "PreflightResult",
    "check_essentia",
    "check_models",
    "preflight",
    "to_dict",
    "summary_lines",
]
