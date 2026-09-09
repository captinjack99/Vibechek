"""Readiness check for ML analysis.

Run before any `analyze` operation. Catches the two failure modes that would
otherwise hang multiprocessing.Pool's worker init: missing essentia install,
missing model files.

Returns a structured result instead of raising, so the GUI can present
actionable next steps rather than a stack trace.
"""

from __future__ import annotations

import hashlib
import logging
import platform
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from pathlib import Path

from vibechek import native_install
from vibechek.analyzer import _ONNX_HEAD_STEMS, _ONNX_SUBDIR, MODELS
from vibechek.config import MODELS_DIR, engine_venv_subdir
from vibechek.model_download import BACKBONE_ONNX_SHA256
from vibechek.native_install import NativeVenvStatus
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
    # "onnx" | "native"). Drives engine-accurate "not ready" messaging.
    engine: str = "essentia_tf"
    # Whether the in-process (sidecar-Python) essentia can actually SERVE this
    # engine — installed AND, for essentia_tf/onnx, a TensorFlow-capable build.
    # The bundled DSP-only Windows wheel (USE_TENSORFLOW=OFF) serves only
    # "native"; this flag keeps it from hijacking essentia_tf/onnx routing into a
    # broken in-process path (those still fall through to WSL / the managed venv).
    essentia_usable: bool = False
    # Whether `import onnxruntime` works in the sidecar's own Python. Only
    # probed for the engines that run inference through it (onnx/native);
    # None on essentia_tf, which doesn't use it at all.
    onnxruntime_installed: bool | None = None

    @property
    def reasons_not_ready(self) -> list[str]:
        out: list[str] = []
        have_engine = (
            self.essentia_usable
            or (self.wsl and self.wsl.can_run_vibechek)
            or (
                self.native_venv
                and self.native_venv.essentia_installed
                and self.native_venv.vibechek_installed
            )
        )
        if not have_engine:
            # Plain, user-facing reason: the install MECHANISM
            # (native / WSL / managed venv) is a diagnostic detail, not the
            # headline a user reads on the setup screen.
            out.append("The analysis engine isn't set up yet.")
            # Name the ONE missing piece when essentia is already here and only
            # the ONNX runtime is absent — otherwise the generic line above
            # sends the user off to install essentia, which they already have.
            if (
                self.engine in ("onnx", "native")
                and self.onnxruntime_installed is False
                and self.essentia.installed
            ):
                out.append(
                    f"The {self.engine} engine also needs ONNX Runtime — install it "
                    "with `pip install onnxruntime`, or pick a different engine "
                    "in Settings."
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


@lru_cache(maxsize=1)
def _essentia_has_tf_algos() -> bool:
    """True iff the importable essentia exposes TensorflowInputMusiCNN.

    The DSP-only native Windows wheel is built ``USE_TENSORFLOW=OFF`` and lacks
    every ``Tensorflow*`` algorithm — including the mel frontend the essentia_tf
    and onnx engines run in-process. A full essentia / essentia-tensorflow build
    has them. Cached because the importable build can't change within a process,
    and importing ``essentia.standard`` for a TF build is multi-second.
    """
    try:
        import essentia.standard as es  # noqa: PLC0415
        return hasattr(es, "TensorflowInputMusiCNN")
    except Exception:  # noqa: BLE001
        return False


@lru_cache(maxsize=1)
def _onnxruntime_importable() -> bool:
    """True iff `import onnxruntime` works in the sidecar's own Python.

    onnxruntime is an OPTIONAL extra (`vibechek[onnx]`), but the onnx and
    native engines run every inference through it — the import happens lazily
    at model-load time (onnx_backend.load_onnx_models), long after preflight
    said READY. A `pip install vibechek[ml]` box (essentia-tensorflow, no
    onnxruntime) switched to the onnx engine used to preflight green and then
    die per-worker; with >1 worker that surfaces as the 5-minute "pool is
    wedged" stall this module exists to prevent. The managed-venv and WSL
    branches already gate on `import essentia, onnxruntime`
    (native_install._native_stack_imports); this is the in-process equivalent.

    Cached like `_essentia_has_tf_algos`: importability can't change within a
    process, and the import itself is slow.
    """
    try:
        import onnxruntime  # noqa: F401, PLC0415
        return True
    except Exception:  # noqa: BLE001
        # A broken install can raise something other than ImportError.
        return False


def essentia_serves_engine(engine: str, installed: bool) -> bool:
    """Can the in-process (sidecar-Python) essentia actually run ``engine``?

    "native" needs only DSP (decode/BPM/key) plus the pure-NumPy mel frontend,
    so ANY importable essentia — including the bundled DSP-only Windows wheel —
    serves it. "essentia_tf"/"onnx" run essentia's TensorFlow-input mel
    in-process, so they need a TF-capable build. This gate is what lets the
    Windows installer BUNDLE the DSP-only wheel (making ``native`` one-click)
    without that wheel hijacking the default essentia_tf/onnx routing: with the
    wheel present, essentia_tf/onnx see ``essentia_serves_engine() is False``
    and still fall through to WSL, while ``native`` runs in-process.

    Both ONNX-stack engines additionally need onnxruntime importable HERE —
    essentia does the DSP, but onnxruntime does the actual inference.
    """
    if not installed:
        return False
    if engine in ("onnx", "native") and not _onnxruntime_importable():
        return False
    if engine == "native":
        return True
    return _essentia_has_tf_algos()


def _model_files_for_engine(
    engine: str, target: Path
) -> list[tuple[str, Path, Path | None, bool, str | None]]:
    """(name, weights_path, metadata_path|None, required, pinned_sha256|None).

    essentia_tf → the `.pb` weights + `.json` metadata for every model in MODELS.
    onnx → the EffNet backbone `.onnx` + each converted head `.onnx`/`.json`. The
    genre head is OPTIONAL (the backbone emits genre directly), so a missing one
    doesn't block readiness — matching onnx_backend's loader.

    The last element is a content pin `check_models` verifies (see there). Only
    the ONNX backbone carries one today: it is the ONE file the whole ONNX stack
    loads first and it is ~18 MB, so hashing it on this (GUI-polled) path is
    cheap. The `.pb` set and the converted heads are pinned too, but their
    digests are enforced at DOWNLOAD time (model_download) and on demand by
    `verify_models` / `vibechek verify-models`; wiring them in here would be a
    behaviour change beyond the backbone gap this closes.
    """
    if engine in ("onnx", "native"):
        # ONNX models live in their own subdir (see analyzer._ONNX_SUBDIR) so
        # they never collide with the essentia `.pb` set in the parent dir.
        # "native" uses the same ONNX model set (it just runs the DSP in-process
        # via a native essentia wheel + the NumPy mel frontend).
        onnx_target = target / _ONNX_SUBDIR
        items: list[tuple[str, Path, Path | None, bool, str | None]] = [
            # The backbone's pin lives in model_download (it is FETCHED from
            # upstream, not converted by scripts/convert_heads_to_onnx.py) —
            # the same constant rpc._verify_models and cli's verify-models
            # check it against, so all three agree on what a good copy is.
            ("effnet (onnx backbone)", onnx_target / BACKBONE_ONNX_FILENAME, None,
             True, BACKBONE_ONNX_SHA256),
            # Genre comes from the backbone, so the genre_discogs400.onnx HEAD is
            # optional — but its 400 class LABELS (genre_discogs400.json) are
            # REQUIRED, else the engine loads "ready" yet emits no genre.
            ("genre_discogs400 classes", onnx_target / "genre_discogs400.json", None,
             True, None),
        ]
        for stem in _ONNX_HEAD_STEMS:
            if stem == "genre_discogs400":
                continue  # head .onnx optional; its .json handled above
            # Non-genre head .onnx are required; their tiny class-label .json is
            # best-effort (not coupled here, so a missing label file doesn't
            # block readiness).
            items.append((stem, onnx_target / f"{stem}.onnx", None, True, None))
        return items
    return [
        (name, target / f"{name}.pb", target / f"{name}.json", True, None)
        for name in MODELS
    ]


def _sha256_file(path: Path) -> str:
    """Stream a file's SHA256 in 1 MiB chunks (the backbone is ~18 MB)."""
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def check_models(models_dir: Path | None = None, engine: str = "essentia_tf") -> ModelsCheck:
    """Verify every required ML model file for `engine` is present + non-trivial.

    A file that carries a content pin (see `_model_files_for_engine`) must also
    MATCH it. A corrupt-but-present backbone used to preflight green and then
    blow up inside the worker pool at model-load time — the one failure mode
    this module exists to catch before multiprocessing hides it. There is no
    "corrupt" state on ModelCheck, and none is needed: a mismatched file is
    reported exactly like a missing one, and the remedy is the same button
    (download-models re-fetches anything that fails its pin).
    """
    target = Path(models_dir or MODELS_DIR)
    result = ModelsCheck(models_dir=str(target))

    for name, weights, metadata, required, pinned in _model_files_for_engine(engine, target):
        # A 0-byte / truncated weights file is broken; treat as missing.
        weights_ok = weights.exists() and weights.stat().st_size > 1024
        if weights_ok and pinned is not None:
            try:
                digest = _sha256_file(weights)
            except OSError as e:
                # Unreadable is unusable — don't report "ready" for a file the
                # worker pool won't be able to open either.
                log.warning("Could not read %s to verify it: %s", weights, e)
                weights_ok = False
            else:
                if digest.lower() != pinned.lower():
                    log.warning(
                        "%s failed its pinned SHA256 (expected %s, got %s) — "
                        "treating it as missing; re-download the models",
                        weights, pinned, digest,
                    )
                    weights_ok = False
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
    venv_subdir = engine_venv_subdir(engine)
    essentia = check_essentia()
    models = check_models(models_dir, engine=engine)
    wsl_status = detect_wsl(quick=quick_wsl, venv_subdir=venv_subdir)
    # Looked up through the module at call time, not bound at import: a test
    # that monkeypatches `native_install.probe_native_venv` while this module
    # is first imported would otherwise freeze the stub into this name for the
    # rest of the session (and take down unrelated later tests).
    native_venv = native_install.probe_native_venv(engine)

    onnxruntime_here = (
        _onnxruntime_importable() if engine in ("onnx", "native") else None
    )
    have_native = essentia_serves_engine(engine, essentia.installed)
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
        essentia_usable=have_native,
        onnxruntime_installed=onnxruntime_here,
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
    elif nv.error:
        # A functional probe failure (e.g. the venv's interpreter no longer
        # runs after a host-Python upgrade) — surface the real reason instead of
        # a bare "not installed" that sends the user to reinstall blindly.
        lines.append(f"  BROKEN: {nv.error}")
        lines.append("  In the desktop app: Settings → System → Install Essentia (reinstalls the venv)")
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

    if r.onnxruntime_installed is False:
        lines.append("")
        lines.append("ONNX Runtime:")
        lines.append("  NOT INSTALLED (the onnx/native engines run inference through it)")
        lines.append("  Install with: pip install onnxruntime")

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
