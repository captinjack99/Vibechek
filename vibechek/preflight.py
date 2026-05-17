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

    @property
    def reasons_not_ready(self) -> list[str]:
        out: list[str] = []
        if not self.essentia.installed:
            out.append("essentia-tensorflow is not installed")
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
    """Full check; ready=True iff analyze can actually run."""
    essentia = check_essentia()
    models = check_models(models_dir)
    return PreflightResult(
        ready=essentia.installed and not models.missing,
        essentia=essentia,
        models=models,
        platform=platform.platform(),
    )


def to_dict(r: PreflightResult) -> dict:
    """JSON-serializable form, including derived reasons."""
    d = asdict(r)
    d["reasons_not_ready"] = r.reasons_not_ready
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

    lines.append("")
    lines.append("READY" if r.ready else "NOT READY (cannot run `analyze`)")
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
