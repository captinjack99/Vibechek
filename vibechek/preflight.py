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

from vibechek.analyzer import _ONNX_HEAD_STEMS, _ONNX_SUBDIR, MODELS
from vibechek.config import MODELS_DIR
from vibechek.native_install import NativeVenvStatus, probe_native_venv
from vibechek.onnx_backend import BACKBONE_ONNX_FILENAME
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
    # Managed-venv status — only meaningful on Linux/macOS. The GUI uses this
    # to render an "Install Essentia" button equivalent to the Windows WSL
    # flow. On Windows native_venv.supported is False.
    native_venv: NativeVenvStatus | None = None
    # How analyze will actually run when ready=True:
    #   "native"      — essentia in the sidecar's own Python (rare)
    #   "wsl"         — Windows + essentia inside a WSL distro
    #   "native_venv" — Linux/macOS + essentia inside ~/.vibechek/venv/
    analyze_via: str | None = None
    # Which inference engine this result was computed for ("essentia_tf" |
    # "onnx"). Drives engine-accurate "not ready" messaging.
    engine: str = "essentia_tf"

    @property
    def reasons_not_ready(self) -> list[str]:
        out: list[str] = []
        have_engine = (
            self.essentia.installed
            or (self.wsl and self.wsl.can_run_vibechek)
            or (
                self.native_venv
                and self.native_venv.essentia_installed
                and self.native_venv.vibechek_installed
            )
        )
        if not have_engine:
            pkg = (
                "the ONNX engine (plain essentia + onnxruntime)"
                if self.engine == "onnx"
                else "essentia-tensorflow"
            )
            out.append(
                f"{pkg} is not installed (native, in WSL, or in the managed venv)"
            )
        if self.models.missing:
            out.append(f"{len(self.models.missing)} ML model file(s) missing")
        # NOTE: WSL version drift is deliberately NOT a "not ready" reason. The
        # analyzer auto-updates an outdated WSL vibechek in place on the next
        # analyze (engine-aware, one-time, with a progress step) instead of
        # rejecting it — so a present-but-stale install IS usable and must not
        # dead-end here. (This used to append an "out of date — Update WSL
        # install" reason back when the analyzer *refused* drift; that premise
        # ended when the auto-update shipped, leaving this a silent trap that
        # fired on every app upgrade / reinstall.)
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


def _model_files_for_engine(
    engine: str, target: Path
) -> list[tuple[str, Path, Path | None, bool]]:
    """(name, weights_path, metadata_path|None, required) per model for an engine.

    essentia_tf → the `.pb` weights + `.json` metadata for every model in MODELS.
    onnx → the EffNet backbone `.onnx` + each converted head `.onnx`/`.json`. The
    genre head is OPTIONAL (the backbone emits genre directly), so a missing one
    doesn't block readiness — matching onnx_backend's loader.
    """
    if engine == "onnx":
        # ONNX models live in their own subdir (see analyzer._ONNX_SUBDIR) so
        # they never collide with the essentia `.pb` set in the parent dir.
        onnx_target = target / _ONNX_SUBDIR
        items: list[tuple[str, Path, Path | None, bool]] = [
            ("effnet (onnx backbone)", onnx_target / BACKBONE_ONNX_FILENAME, None, True),
            # Genre comes from the backbone, so the genre_discogs400.onnx HEAD is
            # optional — but its 400 class LABELS (genre_discogs400.json) are
            # REQUIRED, else the engine loads "ready" yet emits no genre.
            ("genre_discogs400 classes", onnx_target / "genre_discogs400.json", None, True),
        ]
        for stem in _ONNX_HEAD_STEMS:
            if stem == "genre_discogs400":
                continue  # head .onnx optional; its .json handled above
            # Non-genre head .onnx are required; their tiny class-label .json is
            # best-effort (not coupled here, so a missing label file doesn't
            # block readiness).
            items.append((stem, onnx_target / f"{stem}.onnx", None, True))
        return items
    return [
        (name, target / f"{name}.pb", target / f"{name}.json", True)
        for name in MODELS
    ]


def check_models(models_dir: Path | None = None, engine: str = "essentia_tf") -> ModelsCheck:
    """Verify every required ML model file for `engine` is present + non-trivial."""
    target = Path(models_dir or MODELS_DIR)
    result = ModelsCheck(models_dir=str(target))

    for name, weights, metadata, required in _model_files_for_engine(engine, target):
        # A 0-byte / truncated weights file is broken; treat as missing.
        weights_ok = weights.exists() and weights.stat().st_size > 1024
        metadata_ok = metadata is None or (metadata.exists() and metadata.stat().st_size > 0)
        size_mb = weights.stat().st_size / (1024 * 1024) if weights.exists() else 0.0
        present = weights_ok and metadata_ok

        result.per_model.append(ModelCheck(
            name=name,
            present=present,
            weights_path=str(weights),
            metadata_path=str(metadata) if metadata is not None else "",
            size_mb=round(size_mb, 1),
        ))
        result.total_size_mb += size_mb
        if present:
            result.found.append(name)
        elif required:
            result.missing.append(name)  # optional (e.g. genre head) never blocks

    result.total_size_mb = round(result.total_size_mb, 1)
    return result


def preflight(
    models_dir: Path | None = None,
    *,
    quick_wsl: bool = True,
    engine: str = "essentia_tf",
) -> PreflightResult:
    """Full check; ready=True iff analyze can actually run on `engine`.

    "Ready" means ONE of:
      - Native essentia is installed in the sidecar's Python AND models present
      - Windows: WSL has vibechek + essentia AND models present (models live on
        the Windows side, mounted into WSL via /mnt/c)
      - Linux/macOS: managed venv has essentia AND models present

    `engine` selects which engine's environment to check: "essentia_tf"
    (default) checks `~/.vibechek/venv` (essentia-tensorflow) + the `.pb`
    models; "onnx" checks `~/.vibechek/venv-onnx` (plain essentia + onnxruntime)
    + the `.onnx` models. This makes the ONNX path gate analyze exactly like the
    TF path — a not-ready result drives the same install/download prompts.

    `quick_wsl=True` (default) skips per-distro vibechek/essentia probes so the
    call returns in under a second — appropriate for the GUI's first-render
    Settings poll. The native_venv check is always fast (disk-only).

    Callers about to run a long ML operation (e.g. `analyze_directory`)
    should pass `quick_wsl=False` so they don't false-fail when WSL has
    essentia but quick mode couldn't see it.
    """
    venv_subdir = "venv-onnx" if engine == "onnx" else "venv"
    essentia = check_essentia()
    models = check_models(models_dir, engine=engine)
    wsl_status = detect_wsl(quick=quick_wsl, venv_subdir=venv_subdir)
    native_venv = probe_native_venv(engine)

    have_native = essentia.installed
    have_wsl = wsl_status.can_run_vibechek
    have_native_venv = native_venv.essentia_installed and native_venv.vibechek_installed
    have_engine = have_native or have_wsl or have_native_venv

    # WSL version drift does NOT block readiness: the analyzer auto-updates an
    # outdated WSL vibechek in place on the next analyze (engine-aware, one-time,
    # with a progress step) rather than rejecting it. Blocking here turned every
    # app upgrade / reinstall into a dead-end "out of date" dialog AND shadowed
    # that self-heal so it never ran (analyze_directory re-checks preflight and
    # raised before reaching the auto-update). A genuinely-missing engine still
    # blocks via have_engine; a stale-but-present one heals on first analyze.
    ready = have_engine and not models.missing
    if have_native:
        analyze_via = "native"
    elif have_wsl:
        analyze_via = "wsl"
    elif have_native_venv:
        analyze_via = "native_venv"
    else:
        analyze_via = None

    return PreflightResult(
        ready=ready,
        essentia=essentia,
        models=models,
        platform=platform.platform(),
        wsl=wsl_status,
        native_venv=native_venv,
        analyze_via=analyze_via,
        engine=engine,
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


def _native_install_summary_lines(r: PreflightResult) -> list[str]:
    """Render the native_venv block of the CLI preflight output."""
    lines: list[str] = []
    nv = r.native_venv
    if nv is None or not nv.supported:
        return lines
    lines.append("")
    lines.append("Managed venv (Linux/macOS auto-install path):")
    if nv.essentia_installed and nv.vibechek_installed:
        lines.append(
            f"  OK ({nv.venv_dir}: vibechek {nv.vibechek_version or '?'} + "
            f"essentia {nv.essentia_version or '?'})"
        )
    elif nv.vibechek_installed:
        lines.append(f"  Partial: vibechek installed at {nv.venv_dir}, essentia missing")
        lines.append("  In the desktop app: Settings → System → Install Essentia")
    else:
        lines.append(f"  Not installed at {nv.venv_dir}")
        lines.append("  In the desktop app: Settings → System → Install Essentia")
        lines.append("  Or by hand: pip install essentia-tensorflow")
    return lines


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
        lines.append("  NOT INSTALLED")
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

    # Linux/macOS auto-install path
    lines.extend(_native_install_summary_lines(r))

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
                if d.vibechek_installed:
                    bits.append("vibechek")
                if d.essentia_installed:
                    bits.append("essentia")
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
