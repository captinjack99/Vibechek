"""WSL (Windows Subsystem for Linux) detection, install, and automation.

This lets the desktop app give Windows users full ML analysis without asking
them to touch a command line. The flow the GUI walks them through:

  1. detect_wsl()           — is WSL installed? Which distros? Is essentia
                              available in any of them?
  2. install_wsl()          — `wsl --install -d Ubuntu-24.04` via elevated
                              PowerShell. Triggers UAC. ~5-15 min download.
  3. install_vibechek_in_wsl(distro)
                            — apt install + pip install vibechek + essentia-tensorflow
                              + chromaprint into the chosen distro. ~5 min.
  4. run_vibechek_in_wsl(distro, args)
                            — wrapper around `wsl.exe -d <distro> -- vibechek ...`
                              used by analyzer.py to route analyze through WSL.

Everything below is a no-op / returns empty results on non-Windows hosts so
imports don't fail there.
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

from vibechek.config import engine_venv_subdir
from vibechek.platform import IS_WINDOWS

# Distros that aren't real Linux environments — probing them with bash will
# either fail with garbage output or hang for the full subprocess timeout.
# Add new known-bad names here as we encounter them.
_NON_LINUX_DISTROS = {"docker-desktop", "docker-desktop-data", "rancher-desktop"}

# Allowed characters in a WSL distro name. Used to reject injection attempts
# before a name is interpolated into the PowerShell `install_wsl` command (a
# single quote would otherwise break out and run arbitrary elevated commands).
# Real distro names — "Ubuntu-24.04", "Debian", "kali-linux" — all match this.
_VALID_DISTRO_RE = re.compile(r"^[A-Za-z0-9._-]+$")

# onnxruntime-gpu version ceiling for the CUDA-12 wheel set we install alongside
# it (nvidia-*-cu12). This MUST stay coherent or `import onnxruntime` hard-crashes
# on a fresh GPU install: the default PyPI `onnxruntime-gpu` wheel has been built
# against CUDA 12.x since 1.19.0 and stayed CUDA-12 through the 1.26 line, but
# **1.27.0 removed CUDA-12 support** and made the default wheel target CUDA 13 —
# so an UNPINNED `pip install onnxruntime-gpu` resolves to 1.27.0, which then
# dlopens libcudart.so.13 at import and dies with "libcudart.so.13: cannot open
# shared object file" against the cu12 runtime we bundle. Pinning `<1.27` keeps
# us on the newest CUDA-12 release line (currently 1.26.x) so the import always
# matches the cu12 wheels. Bump this ceiling only together with a move to the
# nvidia-*-cu13 wheel set. (Verified 2026-07: onnxruntime 1.27.0 released Jun-2026
# is CUDA-13-only; 1.19–1.26 default wheels are CUDA-12.) Shared with the native
# (Linux/macOS) install path in native_install.py so both stay in lockstep.
ONNXRUNTIME_GPU_SPEC = "onnxruntime-gpu<1.27"

log = logging.getLogger(__name__)

ProgressCallback = Callable[[int, int, str], None]


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class DistroInfo:
    name: str
    version: str | None = None  # "2" for WSL2, "1" for WSL1
    state: str = "Unknown"      # "Running" | "Stopped" | "Unknown"
    is_default: bool = False
    vibechek_installed: bool = False
    essentia_installed: bool = False
    vibechek_path: str | None = None
    # The vibechek __version__ string read from the WSL install's
    # site-packages metadata. None when not yet probed; "0.1.0-dev" or any
    # older value means the user installed before the cap/stall-watchdog work
    # landed and analyze WILL crash silently on multi-worker runs.
    vibechek_version: str | None = None


@dataclass
class WSLStatus:
    is_windows: bool
    wsl_available: bool           # `wsl.exe` exists on PATH
    wsl_feature_enabled: bool     # `wsl --status` succeeds (feature is on)
    distros: list[DistroInfo] = field(default_factory=list)
    default_distro: str | None = None
    recommended_distro: str | None = None
    error: str | None = None

    @property
    def can_run_vibechek(self) -> bool:
        """True iff at least one distro has vibechek + essentia ready to go."""
        return any(
            d.vibechek_installed and d.essentia_installed for d in self.distros
        )

    @property
    def usable_distro(self) -> str | None:
        """Pick the distro to actually use. Prefers default, then first ready."""
        ready = [d for d in self.distros if d.vibechek_installed and d.essentia_installed]
        if not ready:
            return None
        for d in ready:
            if d.is_default:
                return d.name
        return ready[0].name


def to_dict(s: WSLStatus) -> dict:
    return asdict(s) | {
        "can_run_vibechek": s.can_run_vibechek,
        "usable_distro": s.usable_distro,
    }


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


def detect_wsl(quick: bool = False, venv_subdir: str = "venv") -> WSLStatus:
    """Snapshot the user's WSL setup.

    `quick=True` skips the per-distro vibechek/essentia probes (which boot
    Stopped distros and can take 30+ seconds total). Returns in under a
    second on typical machines. Used by `preflight()` so the GUI never
    hangs on first load. Call again with `quick=False` for full detail
    after the UI has rendered.

    `venv_subdir` selects which managed venv the per-distro probe inspects —
    "venv" (default, essentia-tensorflow) or "venv-onnx" (the TF-free ONNX
    engine). Preflight passes the one matching the selected inference engine.
    """
    status = WSLStatus(is_windows=IS_WINDOWS, wsl_available=False, wsl_feature_enabled=False)

    if not IS_WINDOWS:
        return status

    wsl = shutil.which("wsl") or shutil.which("wsl.exe")
    if not wsl:
        return status
    status.wsl_available = True

    # `wsl --status` is the cheapest "is the feature enabled?" probe
    try:
        result = _wsl_run([wsl, "--status"], timeout=5)
    except Exception as e:  # noqa: BLE001
        status.error = f"wsl --status failed: {e}"
        return status

    status.wsl_feature_enabled = result.returncode == 0
    if not status.wsl_feature_enabled:
        return status

    # Parse `wsl --list --verbose` for the distro inventory
    try:
        result = _wsl_run([wsl, "--list", "--verbose"], timeout=5)
        if result.returncode == 0:
            status.distros = _parse_distro_list(result.stdout)
            status.default_distro = next(
                (d.name for d in status.distros if d.is_default), None
            )
    except Exception as e:  # noqa: BLE001
        log.debug("wsl --list failed: %s", e)

    status.recommended_distro = "Ubuntu-24.04"

    if quick:
        return status

    # Slow path: probe each Linux distro for vibechek + essentia.
    # Parallel so total time ≈ slowest single probe, not sum.
    linux_distros = [d for d in status.distros if d.name.lower() not in _NON_LINUX_DISTROS]
    if linux_distros:
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(linux_distros)) as ex:
            futures = {ex.submit(_probe_distro, d, wsl, venv_subdir): d for d in linux_distros}
            for fut in concurrent.futures.as_completed(futures, timeout=30):
                d = futures[fut]
                try:
                    fut.result()
                except Exception as e:  # noqa: BLE001
                    log.debug("probe %s failed: %s", d.name, e)

    return status


def _parse_distro_list(stdout: str) -> list[DistroInfo]:
    """Parse the output of `wsl --list --verbose`.

    Format (encoded as UTF-16 on Windows; we handle that in _wsl_run):
        NAME                STATE     VERSION
      * Ubuntu-24.04        Running   2
        Debian              Stopped   2
    """
    distros: list[DistroInfo] = []
    lines = [ln.rstrip() for ln in stdout.splitlines() if ln.strip()]
    # Skip header
    for line in lines[1:]:
        is_default = line.lstrip().startswith("*")
        clean = line.lstrip().lstrip("*").strip()
        # The trailing two columns are always STATE and VERSION (single tokens);
        # everything before them is the distro NAME, which CAN contain spaces
        # (e.g. "Ubuntu 22.04 LTS"). Peeling from the RIGHT with maxsplit=2
        # keeps the name intact, whereas a left `split(r"\s+")` shredded it into
        # `["Ubuntu", "22.04", "LTS", ...]` and mis-assigned the columns.
        parts = clean.rsplit(maxsplit=2)
        if len(parts) == 3:
            distros.append(DistroInfo(
                name=parts[0],
                state=parts[1],
                version=parts[2],
                is_default=is_default,
            ))
        elif clean:
            distros.append(DistroInfo(name=clean, is_default=is_default))
    return distros


def _probe_distro(distro: DistroInfo, wsl_exe: str, venv_subdir: str = "venv") -> None:
    """Check whether `vibechek` and `essentia` are importable inside this distro.

    `venv_subdir` selects which managed venv to probe: "venv" (default,
    essentia-tensorflow) or "venv-onnx" (the TF-free ONNX engine). For the
    non-default venv we check ONLY that venv's binary — the shared
    `~/.local/bin/vibechek` symlink tracks the default venv, so trusting it
    would falsely report the ONNX engine as installed.

    A single bash invocation does both probes so we only pay the distro-boot
    cost once. We check known fixed paths directly (not `which`) because
    `bash -lc` on Ubuntu is non-interactive and won't add `~/.local/bin` to
    PATH — Ubuntu's default `.bashrc` returns early for non-interactive shells.
    """
    # The ~/.local/bin symlink only tracks the default venv; don't trust it for
    # the ONNX engine venv.
    extra_bin = ' "$HOME_DIR/.local/bin/vibechek"' if venv_subdir == "venv" else ""
    # Disk-only check — fast and reliable. We deliberately DO NOT `import
    # essentia` because that triggers a ~10s TensorFlow load.
    #
    # *Critical:* we run the script via `bash -s` over stdin instead of
    # `bash -c "<script>"`. wsl.exe on Windows has a preprocessor quirk
    # that breaks variable assignment in multi-line `-c` scripts (the LHS
    # variable ends up empty). The install path uses `bash -s` for the
    # same reason — see install_vibechek_in_wsl.
    # Self-healing: if the venv's vibechek shim contains the broken
    # `. cuda-env.sh` line (a bug that shipped in beta.6-beta.9), strip it
    # in-place. Without this, every analyze through WSL crashes with
    # `SyntaxError: invalid syntax` and the user gets a useless "Invalid
    # params: Expecting value" toast. The repair is a single-line sed, idempotent,
    # and safe — `cuda-env.sh` should never appear in a Python entry point.
    script = rf"""
HOME_DIR="$(printenv HOME)"
VENV="$HOME_DIR/.vibechek/{venv_subdir}"
SHIM="$VENV/bin/vibechek"
if [ -f "$SHIM" ] && grep -q "cuda-env.sh" "$SHIM"; then
    # Strip the bad line. Done at probe time so users don't have to think.
    TMP="$(mktemp)"
    # Atomic rewrite: only replace the live shim if grep produced a non-empty
    # result, then `mv` (rename) over it. The old `cat "$TMP" > "$SHIM"`
    # truncated the shim FIRST, so a disk-full between truncate and copy left
    # `vibechek` empty — and this repair runs on every status probe.
    if grep -v "cuda-env.sh" "$SHIM" > "$TMP" && [ -s "$TMP" ]; then
        mv "$TMP" "$SHIM"
        printf 'repaired=1\n'
    fi
    rm -f "$TMP"
fi
# FUNCTIONAL readiness, not existence-only. The shim being present on disk does
# NOT prove the venv's interpreter still runs: a distro `apt` release-upgrade
# that moves the base `python3`, or a WSL reinstall that rebuilt the distro,
# leaves the venv files intact while their shebang'd interpreter is gone. An
# existence-only `[ -x "$SHIM" ]` then reports READY and analyze later dies with
# an opaque "Invalid params: Expecting value" toast (empty output from a shim
# that can't exec). Gate `vibechek=` on the cheapest possible liveness check —
# `python -c "import sys"` — so a dead interpreter downgrades to "not ready" and
# drives the existing "Set up WSL" remediation UI instead.
VENV_PY="$VENV/bin/python"
PY_OK=0
if [ -x "$VENV_PY" ] && "$VENV_PY" -c "import sys" >/dev/null 2>&1; then
  PY_OK=1
fi
if [ "$PY_OK" = "1" ]; then
  for p in "$SHIM"{extra_bin}; do
    if [ -x "$p" ]; then
      printf 'vibechek=%s\n' "$p"
      break
    fi
  done
else
  printf 'py_broken=1\n'
fi
# Probe the installed vibechek __version__ from site-packages metadata. We
# prefer reading PKG-INFO over invoking `vibechek --version` because the
# latter imports the package (slow + can fail if the install is half-broken),
# and we need this probe to be fast + tolerant.
for d in "$VENV/lib/python3."*/site-packages/vibechek-*.dist-info; do
  if [ -d "$d" ]; then
    VER="$(basename "$d" | sed -E 's/^vibechek-([^-]+)\.dist-info$/\1/')"
    printf 'vibechek_version=%s\n' "$VER"
    break
  fi
done
for d in "$VENV/lib/python3."*/site-packages/essentia*.dist-info; do
  if [ -d "$d" ]; then
    printf 'essentia=%s\n' "$(basename "$d" | sed -E 's/^essentia[_-][^-]+-([^-]+)\.dist-info$/\1/')"
    break
  fi
done
"""
    try:
        proc = subprocess.Popen(
            [wsl_exe, "-d", distro.name, "--", "bash", "-s"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        # Send as raw bytes with \n only — see install_vibechek_in_wsl for
        # the same CRLF-stripping pattern.
        clean = script.replace("\r\n", "\n").replace("\r", "\n")
        stdout_bytes, _ = proc.communicate(input=clean.encode("utf-8"), timeout=30)
    except Exception as e:  # noqa: BLE001
        log.debug("probe %s failed: %s", distro.name, e)
        return

    if proc.returncode != 0:
        return

    # Decode like _wsl_run does — wsl can emit UTF-16 LE on some setups.
    # (Removed the dead _R wrapper class + tautological rc check
    # that used to live here. The proc.returncode check above already
    # short-circuits all non-zero exits.)
    stdout = ""
    for enc in ("utf-8", "utf-16-le", "cp1252"):
        try:
            stdout = stdout_bytes.decode(enc).replace("\x00", "")
            break
        except UnicodeDecodeError:
            continue

    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith("vibechek_version=") and len(line) > len("vibechek_version="):
            distro.vibechek_version = line[len("vibechek_version="):]
        elif line.startswith("vibechek=") and len(line) > len("vibechek="):
            distro.vibechek_installed = True
            distro.vibechek_path = line[len("vibechek="):]
        elif line.startswith("essentia=") and len(line) > len("essentia="):
            distro.essentia_installed = True
        elif line == "repaired=1":
            log.warning(
                "Auto-repaired broken vibechek shim in distro %s (stripped "
                "stale `. cuda-env.sh` line from a pre-beta.10 CUDA install)",
                distro.name,
            )
        elif line == "py_broken=1":
            # Interpreter is dead behind an otherwise-present venv. Leave
            # vibechek_installed False so preflight reports "not ready" and the
            # GUI offers "Set up WSL" instead of dispatching an analyze that
            # would fail with an opaque toast. Logged (never swallowed) so it's
            # visible in the sidecar log; ensure_engine_runtime repairs it on
            # the next analyze per the zero-setup doctrine.
            log.warning(
                "WSL venv interpreter is broken in distro %s (python -c "
                "'import sys' failed) — the engine venv needs a reinstall",
                distro.name,
            )


# ---------------------------------------------------------------------------
# Engine-side GPU probe — what TensorFlow inside WSL actually sees
# ---------------------------------------------------------------------------


@dataclass
class EngineGpuDevice:
    """A single GPU as seen by the analyze engine.

    `vendor` was added when GPU detection went cross-vendor: TF can only
    actually accelerate on NVIDIA today, but the *probe* may surface
    AMD/Intel/Apple cards that exist on the host so the UI can be honest.
    """
    name: str
    backend: str  # "cuda" | "rocm" | "metal" | "unknown"
    compute_capability: str | None = None
    memory_mb: int | None = None
    vendor: str = "nvidia"  # nvidia | amd | intel | apple | unknown


@dataclass
class EngineGpuInfo:
    """What the analyze engine (essentia/TF, native or in WSL) actually sees.

    This is the *ground truth* for the GPU question. The Windows-side
    `resources.detect()` only tells you "does the host have a GPU?". When
    analyze routes through WSL, the relevant question is "will TensorFlow
    inside the WSL distro actually use a GPU?" — which depends on the WSL
    kernel's GPU passthrough, NVIDIA driver, AND the full CUDA library
    chain (libcublas / libcufft / libcudnn / etc.) being installed.

    Three GPU states we care about, each surfaced via different fields:
      - No hardware:   gpu_available=False, gpu_hardware_visible=False
      - HW but TF can't use it (missing libs):
                       gpu_available=False, gpu_hardware_visible=True,
                       missing_cuda_libs populated
      - Fully usable:  gpu_available=True, gpu_hardware_visible=True
    """
    engine: str                     # "native" | "wsl" | "unknown"
    distro: str | None = None       # WSL distro name when engine="wsl"
    ok: bool = False                # True iff the probe ran successfully
    # True if TF will actually register the GPU and use it for inference.
    # Different from gpu_hardware_visible — see class docstring.
    gpu_available: bool = False
    gpu_count: int = 0
    devices: list[EngineGpuDevice] = field(default_factory=list)
    # True if TF saw the GPU hardware ("Found device 0 ..."), even if it
    # ultimately couldn't register it due to missing libs.
    gpu_hardware_visible: bool = False
    # CUDA libs TF tried to dlopen but couldn't find inside the engine.
    # Populated when the GPU is hardware-visible but TF skipped it.
    missing_cuda_libs: list[str] = field(default_factory=list)
    tf_version: str | None = None
    tf_built_with_cuda: bool | None = None
    nvidia_driver: str | None = None    # From nvidia-smi inside WSL
    nvidia_smi_available: bool = False
    error: str | None = None        # Error message when ok=False
    probed_at: float = 0.0          # epoch seconds
    # For the ONNX engine: the onnxruntime ExecutionProvider that actually
    # initialized ("CUDAExecutionProvider" / "ROCMExecutionProvider" /
    # "CoreMLExecutionProvider" / "DmlExecutionProvider"), or None. Lets the UI
    # say "GPU via ONNX Runtime (CUDA)" instead of TF wording. None for the TF
    # engine (which uses tf_version / missing_cuda_libs instead).
    provider: str | None = None
    runtime: str | None = None      # "onnxruntime X.Y.Z" for the onnx engine
    # Honest CPU-only story for engines that don't attempt the GPU at all — the
    # Windows-native engine runs the BUNDLED ONNX Runtime in-process, and that
    # wheel ships no GPU execution providers (a roadmap item). Set only by the
    # native-bundled probe; the UI renders it verbatim instead of the old
    # host-only "GPU available … via native TensorFlow" (which was doubly wrong:
    # no TF, and the host GPU is never used by that engine).
    note: str | None = None


# Process-level cache: probes take ~10s (TF import + GPU enumeration). The UI
# re-renders often and we want subsequent calls to be free. TTL covers the
# typical "user is poking around Settings" window without being so long that
# a hot-plugged GPU (or a driver change) goes unnoticed across sessions.
_ENGINE_GPU_CACHE: dict[str, tuple[EngineGpuInfo, float]] = {}
_ENGINE_GPU_CACHE_LOCK = threading.Lock()
_ENGINE_GPU_CACHE_TTL_SEC = 300.0  # 5 minutes


# This is the Python script we run *inside the WSL venv* to ask the actual
# analyze engine what GPUs it can use. essentia-tensorflow bundles TensorFlow
# as a native C++ library (under essentia_tensorflow.libs/) — TF is NOT a
# top-level Python module. So we probe in three layers, each more expensive
# than the last:
#
#   1. Cheap: does essentia_tensorflow.libs/ contain libcudart.so*? That tells
#      us whether the bundled TF was compiled with CUDA support at all.
#   2. Medium: try `import tensorflow` in case the user installed it
#      separately — gives the most authoritative version + device list.
#   3. Expensive: instantiate a minimal essentia TensorflowPredict* with a
#      dummy graph and capture the device messages TF emits to stderr.
#      Failure here just means "no GPU", not a probe failure.
#
# Output is one JSON object on stdout. The bash wrapper around this prefixes
# it with TF_JSON= so we can pluck it out of any TF startup chatter.
_ENGINE_GPU_PROBE_PY = r"""
import glob, json, os, re, subprocess, sys

# Silence TF's startup noise on stdout. TF logs to stderr anyway, but some
# native CUDA libraries occasionally print to stdout via printf().
os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "3")
# Tell TF to log device placement so the lower-level probe can pick up GPU info.
os.environ.setdefault("TF_LOG_DEVICE_PLACEMENT", "0")

out = {"ok": False, "devices": [], "gpu_count": 0}

# ---- Layer 1: CUDA libs bundled with essentia_tensorflow ----
try:
    import essentia_tensorflow as _et
    bundle_dir = os.path.dirname(_et.__file__) if hasattr(_et, "__file__") else None
except Exception:
    bundle_dir = None
if not bundle_dir:
    # Fall back to looking under site-packages directly
    for sp in sys.path:
        candidate = os.path.join(sp, "essentia_tensorflow.libs")
        if os.path.isdir(candidate):
            bundle_dir = candidate
            break

cuda_libs = []
if bundle_dir:
    # essentia_tensorflow.libs/ vs the parent package — try both
    for d in (bundle_dir, os.path.join(os.path.dirname(bundle_dir), "essentia_tensorflow.libs")):
        if os.path.isdir(d):
            for pattern in ("libcudart*", "libcudnn*", "libcublas*"):
                cuda_libs += [os.path.basename(p) for p in glob.glob(os.path.join(d, pattern))]
            break
out["bundle_dir"] = bundle_dir
out["cuda_libs"] = sorted(set(cuda_libs))
out["tf_built_with_cuda"] = bool(cuda_libs) or None

# ---- Layer 2: try the standalone tensorflow module (rare for essentia) ----
try:
    import tensorflow as tf  # type: ignore
    out["tf_version"] = getattr(tf, "__version__", None)
    try:
        out["tf_built_with_cuda"] = bool(tf.test.is_built_with_cuda())
    except Exception:
        pass
    try:
        gpus = tf.config.list_physical_devices("GPU")
        for g in gpus:
            d = {"name": str(g.name)}
            try:
                details = tf.config.experimental.get_device_details(g)
                if details.get("device_name"):
                    d["device_name"] = str(details["device_name"])
                cc = details.get("compute_capability")
                if cc:
                    d["compute_capability"] = ".".join(str(x) for x in cc)
            except Exception:
                pass
            out["devices"].append(d)
        out["gpu_count"] = len(gpus)
        out["ok"] = True
    except Exception as e:
        out["error_tf_layer"] = f"{type(e).__name__}: {e}"
except ImportError:
    pass

# ---- Layer 3: actually instantiate essentia's TF wrapper and parse stderr ----
# Only run if Layer 2 didn't already give us authoritative info. We re-spawn
# this Python script as a child so we can capture *all* stderr (TF init logs
# from native libs would otherwise leak past sys.stderr capture).
if not out["devices"]:
    probe_code = (
        "import os; os.environ['TF_CPP_MIN_LOG_LEVEL']='0';\n"
        "try:\n"
        "    import essentia.standard as es\n"
        "    # Bogus graph path: model load will fail, but TF + CUDA init runs first.\n"
        "    try:\n"
        "        es.TensorflowPredict2D(graphFilename='/nonexistent/probe.pb',\n"
        "                               input='x', output='y')\n"
        "    except Exception:\n"
        "        pass\n"
        "except Exception as e:\n"
        "    print('PROBE_ERROR:', type(e).__name__, e)\n"
    )
    try:
        # Re-use the same interpreter so we get the venv's deps
        result = subprocess.run(
            [sys.executable, "-c", probe_code],
            capture_output=True, text=True, timeout=30,
        )
        combined = (result.stdout or "") + "\n" + (result.stderr or "")
        # Extract every CUDA lib TF tried to dlopen but failed on. The exact
        # message is:
        #   "Could not load dynamic library 'libcublas.so.11'; dlerror: ..."
        missing = sorted(set(re.findall(
            r"Could not load dynamic library '([^']+)'", combined
        )))
        out["missing_cuda_libs"] = missing

        # Hardware visibility: TF reports "Found device N with properties:"
        # even when it later skips registration. We parse all of them.
        hw_devices = []
        for m in re.finditer(
            r"Found device \d+ with properties:.*?name:\s*([^\n,]+)"
            r"(?:.*?computeCapability:\s*([\d.]+))?",
            combined,
            flags=re.DOTALL,
        ):
            d = {"name": m.group(1).strip(), "via": "essentia_stderr"}
            if m.group(2):
                d["compute_capability"] = m.group(2)
            hw_devices.append(d)
        # Dedupe (TF logs the device multiple times during init)
        seen_names = set()
        deduped = []
        for d in hw_devices:
            if d["name"] not in seen_names:
                seen_names.add(d["name"])
                deduped.append(d)
        out["gpu_hardware_visible"] = bool(deduped)

        # The decisive check: TF either registers the GPU ("Adding visible
        # gpu devices: 0") or it skips it ("Skipping registering GPU devices").
        skipped = "Skipping registering GPU devices" in combined
        registered = bool(re.search(
            r"(?:Adding visible gpu devices|Created TensorFlow device.*?GPU)",
            combined,
        ))

        if deduped and not skipped and registered:
            # GPU usable: TF registered it, no skip message.
            out["devices"] = deduped
            out["gpu_count"] = len(deduped)
            out["ok"] = True
            out["probed_via"] = "essentia_stderr_gpu_registered"
        elif deduped and skipped:
            # GPU hardware visible but TF won't use it (usually missing libs).
            out["ok"] = True
            out["probed_via"] = "essentia_stderr_gpu_skipped"
            # devices stays empty — engine won't use them.
        elif deduped:
            # Ambiguous: hardware seen, no explicit "skipping" or "registering"
            # message. Lean conservative — assume TF will use it.
            out["devices"] = deduped
            out["gpu_count"] = len(deduped)
            out["ok"] = True
            out["probed_via"] = "essentia_stderr_ambiguous"
        else:
            # No GPUs detected via essentia either — definitive "no GPU".
            out["ok"] = True
            out["probed_via"] = "essentia_stderr_no_gpu"
    except subprocess.TimeoutExpired:
        out["error"] = "essentia probe timed out"
    except Exception as e:
        out["error"] = f"essentia probe failed: {type(e).__name__}: {e}"

if not out["ok"] and "error" not in out:
    out["error"] = "no probe layer succeeded"

# ---- Layer 4: cross-vendor inventory (AMD/Intel/etc) via lspci ----
# This runs *inside* the WSL distro so it sees the GPU as the Linux kernel
# sees it (typically only the NVIDIA card under WSLg + dxg passthrough; AMD
# iGPUs sometimes appear, Intel iGPUs almost never). Surfaces non-NVIDIA
# devices to the UI so we can be honest that they're not accelerated.
out["other_gpus"] = []
try:
    lspci_out = subprocess.run(
        ["bash", "-c", "command -v lspci >/dev/null 2>&1 && lspci || true"],
        capture_output=True, text=True, timeout=5,
    ).stdout or ""
except Exception:
    lspci_out = ""
for ln in lspci_out.splitlines():
    if not re.search(r"\b(VGA compatible controller|3D controller|Display controller)\b", ln):
        continue
    vendor = None
    if re.search(r"(Advanced Micro Devices|AMD|ATI)", ln, flags=re.IGNORECASE):
        vendor = "amd"
    elif re.search(r"Intel Corporation", ln):
        vendor = "intel"
    elif re.search(r"NVIDIA", ln, flags=re.IGNORECASE):
        # NVIDIA already covered by the TF probe; skip to avoid double-counting.
        continue
    else:
        vendor = "unknown"
    m = re.search(r"\[([^\[\]]+)\]\s*(?:\(rev\s+[^\)]+\))?\s*$", ln)
    if m:
        name = m.group(1).strip()
    else:
        name = ln.split(":", 2)[-1].strip() or "GPU"
    out["other_gpus"].append({"vendor": vendor, "name": name})

print(json.dumps(out))
"""


# ONNX-engine GPU probe. Runs in venv-onnx and asks onnxruntime which GPU
# ExecutionProvider actually INITIALIZES — the definitive test, since
# onnxruntime drops an EP that can't allocate the device, so an EP surviving in
# session.get_providers() means the GPU is genuinely usable. Validated on an
# RTX 4070: CUDAExecutionProvider survives via onnxruntime-gpu + nvidia-*-cu12 +
# preload_dlls(). Prints one JSON object on stdout.
_ONNX_GPU_PROBE_PY = r'''
import base64, json
out = {"ok": False, "providers": [], "provider": None, "gpu_available": False,
       "runtime": None, "error": None}
try:
    import onnxruntime as ort
    out["runtime"] = "onnxruntime " + ort.__version__
    if hasattr(ort, "preload_dlls"):
        try:
            ort.preload_dlls()
        except Exception as e:
            out["preload_error"] = str(e)[:200]
    avail = list(ort.get_available_providers())
    out["providers"] = avail
    GPU_EPS = ["CUDAExecutionProvider", "ROCMExecutionProvider",
               "DmlExecutionProvider", "CoreMLExecutionProvider"]
    model = base64.b64decode("CA06NwoQCgF4EgF5IghJZGVudGl0eRIBZ1oPCgF4EgoKCAgBEgQKAggBYg8KAXkSCgoICAESBAoCCAFCBAoAEA0=")
    so = ort.SessionOptions()
    so.log_severity_level = 3
    for ep in GPU_EPS:
        if ep not in avail:
            continue
        try:
            sess = ort.InferenceSession(model, sess_options=so, providers=[ep, "CPUExecutionProvider"])
            if ep in sess.get_providers():
                out["provider"] = ep
                out["gpu_available"] = True
                break
        except Exception as e:
            out.setdefault("ep_errors", {})[ep] = str(e)[:200]
    out["ok"] = True
except Exception as e:
    out["error"] = "%s: %s" % (type(e).__name__, e)
print(json.dumps(out))
'''

_ONNX_PROVIDER_BACKEND = {
    "CUDAExecutionProvider": ("cuda", "nvidia"),
    "ROCMExecutionProvider": ("rocm", "amd"),
    "DmlExecutionProvider": ("directml", "unknown"),
    "CoreMLExecutionProvider": ("coreml", "apple"),
}


def _apply_onnx_json(info: EngineGpuInfo, onnx_out: dict, gpu_name: str | None) -> None:
    """Populate an EngineGpuInfo from the onnxruntime GPU probe JSON."""
    info.ok = bool(onnx_out.get("ok"))
    info.provider = onnx_out.get("provider")
    info.runtime = onnx_out.get("runtime")
    info.gpu_available = bool(onnx_out.get("gpu_available"))
    if onnx_out.get("error") and not info.error:
        info.error = onnx_out["error"]
    backend, vendor = _ONNX_PROVIDER_BACKEND.get(info.provider or "", ("unknown", "unknown"))
    info.gpu_hardware_visible = info.gpu_available or info.nvidia_smi_available
    if info.gpu_available:
        info.devices.append(EngineGpuDevice(
            name=gpu_name or (info.provider or "GPU"), backend=backend, vendor=vendor,
        ))
        info.gpu_count = len(info.devices)


def _probe_wsl_onnx_gpu(distro: str) -> EngineGpuInfo:
    """ONNX GPU probe inside `distro`'s venv-onnx (onnxruntime EPs)."""
    info = EngineGpuInfo(engine="wsl", distro=distro)
    wsl = shutil.which("wsl") or shutil.which("wsl.exe")
    if not wsl:
        info.error = "wsl.exe not on PATH"
        return info
    script = r"""
set +e
HOME_DIR="$(printenv HOME)"
DRIVER=""
GPUNAME=""
if command -v nvidia-smi >/dev/null 2>&1; then
    DRIVER="$(nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null | head -n1 | tr -d ' \r\n')"
    GPUNAME="$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -n1 | sed 's/[[:space:]]*$//' | tr -d '\r')"
fi
echo "NVIDIA_DRIVER=${DRIVER}"
echo "GPU_NAME=${GPUNAME}"
VENV_PY="$HOME_DIR/.vibechek/venv-onnx/bin/python"
if [ ! -x "$VENV_PY" ]; then
    echo "ONNX_JSON={\"ok\":false,\"error\":\"ONNX engine not set up (venv-onnx missing)\"}"
    exit 0
fi
echo "ONNX_JSON=$("$VENV_PY" - <<'PY' 2>/dev/null
__PROBE__
PY
)"
"""
    script = script.replace("__PROBE__", _ONNX_GPU_PROBE_PY, 1)
    try:
        proc = subprocess.Popen(
            [wsl, "-d", distro, "--", "bash", "-s"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        clean = script.replace("\r\n", "\n").replace("\r", "\n")
        stdout_bytes, stderr_bytes = proc.communicate(input=clean.encode("utf-8"), timeout=60)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except OSError:
            pass
        info.error = "onnx GPU probe timed out after 60s"
        return info
    except Exception as e:  # noqa: BLE001
        info.error = f"onnx GPU probe failed: {type(e).__name__}: {e}"
        return info
    stdout = ""
    for enc in ("utf-8", "utf-16-le", "cp1252"):
        try:
            stdout = stdout_bytes.decode(enc).replace("\x00", "")
            break
        except UnicodeDecodeError:
            continue
    gpu_name: str | None = None
    onnx_out: dict = {}
    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if line.startswith("NVIDIA_DRIVER="):
            val = line[len("NVIDIA_DRIVER="):].strip()
            if val:
                info.nvidia_driver = val
                info.nvidia_smi_available = True
        elif line.startswith("GPU_NAME="):
            val = line[len("GPU_NAME="):].strip()
            if val:
                gpu_name = val
        elif line.startswith("ONNX_JSON="):
            payload = line[len("ONNX_JSON="):].strip()
            if payload:
                try:
                    onnx_out = json.loads(payload)
                except json.JSONDecodeError as e:
                    info.error = f"could not parse onnx probe JSON: {e}; raw={payload[:200]}"
    if onnx_out:
        _apply_onnx_json(info, onnx_out, gpu_name)
    elif not info.error:
        info.error = "onnx GPU probe produced no output"
    return info


def _probe_native_onnx_gpu() -> EngineGpuInfo:
    """ONNX GPU probe via the native venv-onnx python (Linux/macOS)."""
    info = EngineGpuInfo(engine="native")
    from vibechek.native_install import probe_native_venv  # noqa: PLC0415
    nv = probe_native_venv("onnx")
    if not nv.supported or not nv.venv_python:
        info.error = "ONNX engine not set up (venv-onnx missing)"
        return info
    try:
        result = subprocess.run(
            [nv.venv_python, "-c", _ONNX_GPU_PROBE_PY],
            capture_output=True, text=True, timeout=60,
        )
    except Exception as e:  # noqa: BLE001
        info.error = f"onnx GPU probe failed: {type(e).__name__}: {e}"
        return info
    onnx_out: dict = {}
    for line in (result.stdout or "").splitlines():
        s = line.strip()
        if s.startswith("{"):
            try:
                onnx_out = json.loads(s)
            except json.JSONDecodeError:
                pass
    if onnx_out:
        _apply_onnx_json(info, onnx_out, None)
    elif not info.error:
        info.error = "onnx GPU probe produced no output"
    return info


def probe_engine_gpu(distro: str | None, *, force: bool = False, engine: str = "essentia_tf") -> EngineGpuInfo:
    """Ask the *actual analyze engine* what GPUs it can use.

    If `distro` is None or we're not on Windows, falls back to a native probe
    using `vibechek.resources` so the API is unified across platforms.

    If `distro` is set (WSL path), runs a Python script inside that distro's
    venv that imports TensorFlow and enumerates GPUs. This is the ground truth
    for "will analyze actually use the GPU?".

    Results are cached for 5 minutes (per distro). Pass `force=True` to bypass.
    """
    cache_key = f"{engine}:wsl:{distro}" if distro else f"{engine}:native"
    now = time.time()
    if not force:
        with _ENGINE_GPU_CACHE_LOCK:
            cached = _ENGINE_GPU_CACHE.get(cache_key)
        if cached is not None and (now - cached[1]) < _ENGINE_GPU_CACHE_TTL_SEC:
            return cached[0]

    native = not distro or not IS_WINDOWS
    if engine == "native" and IS_WINDOWS:
        # The Windows-native engine runs the BUNDLED ONNX Runtime in-process.
        # That wheel is CPU-only (no GPU EPs bundled — roadmap), so the honest
        # answer is "no GPU" regardless of host hardware. The old code fell into
        # the `elif native` TF branch below → a host-only nvidia-smi probe
        # rendered as a green "GPU available … via native TensorFlow", doubly
        # wrong. On Linux/macOS "native" is the managed venv-onnx, so it keeps
        # the real onnxruntime EP probe below.
        info = _probe_native_bundled_gpu()
    elif engine == "onnx":
        # ONNX engine: query onnxruntime's EPs in venv-onnx, not TF.
        info = _probe_native_onnx_gpu() if native else _probe_wsl_onnx_gpu(distro)
    elif engine == "native" and native:
        # Linux/macOS native = managed venv-onnx: use the real ONNX EP probe.
        info = _probe_native_onnx_gpu()
    elif native:
        info = _probe_native_engine_gpu()
    else:
        info = _probe_wsl_engine_gpu(distro)

    info.probed_at = now
    with _ENGINE_GPU_CACHE_LOCK:
        _ENGINE_GPU_CACHE[cache_key] = (info, now)
    return info


def _probe_native_engine_gpu() -> EngineGpuInfo:
    """Native probe: ask `~/.vibechek/venv` if it has essentia + GPU.

    On Linux/macOS the analyze engine is the managed venv (not the sidecar's
    own Python, which is the PyInstaller bundle without essentia). So we
    run the same layered TF probe we use for WSL — just without the wsl.exe
    wrapper. If the managed venv isn't installed yet, we fall back to the
    host-only view from `resources.detect()`.

    Skipped on Windows entirely: Windows uses the WSL probe path.
    """
    if IS_WINDOWS:
        # On Windows, the "native" engine is essentia in the sidecar's Python,
        # which never has it. Just use host hardware view.
        return _probe_host_only_native_gpu()

    from vibechek.native_install import probe_native_venv  # noqa: PLC0415
    nv = probe_native_venv()
    if not nv.supported or not nv.essentia_installed or not nv.venv_python:
        # Managed venv not set up → fall back to host hardware probe.
        return _probe_host_only_native_gpu()

    info = EngineGpuInfo(engine="native")

    # Same layered probe as WSL, but run directly via the venv python — no
    # bash -s wrapper, no wsl.exe.
    script = _ENGINE_GPU_PROBE_PY
    try:
        result = subprocess.run(
            [nv.venv_python, "-c", script],
            capture_output=True,
            timeout=60,
            check=False,
        )
    except subprocess.TimeoutExpired:
        info.error = "engine GPU probe timed out after 60s"
        return info
    except OSError as e:
        info.error = f"engine GPU probe failed: {e}"
        return info

    # Parse the JSON line. The probe script prints one JSON object on stdout.
    stdout = result.stdout.decode("utf-8", errors="replace") if isinstance(result.stdout, bytes) else result.stdout
    try:
        tf_out = json.loads(stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError) as e:
        info.error = f"could not parse engine GPU probe output: {e}; stdout={stdout[:200]}"
        return info

    info.ok = bool(tf_out.get("ok"))
    info.tf_version = tf_out.get("tf_version")
    info.tf_built_with_cuda = tf_out.get("tf_built_with_cuda")
    info.gpu_hardware_visible = bool(tf_out.get("gpu_hardware_visible"))
    info.missing_cuda_libs = list(tf_out.get("missing_cuda_libs") or [])
    for d in tf_out.get("devices", []):
        info.devices.append(EngineGpuDevice(
            name=d.get("device_name") or d.get("name", "?"),
            backend="cuda",
            compute_capability=d.get("compute_capability"),
            vendor="nvidia",
        ))
    # Cross-vendor inventory from Layer 4 (lspci inside the venv host).
    for og in tf_out.get("other_gpus", []):
        info.devices.append(EngineGpuDevice(
            name=og.get("name", "?"),
            backend={
                "amd": "rocm",
                "intel": "unknown",
                "apple": "metal",
            }.get(og.get("vendor", "unknown"), "unknown"),
            vendor=og.get("vendor", "unknown"),
        ))
    info.gpu_count = int(tf_out.get("gpu_count") or sum(
        1 for d in info.devices if d.vendor == "nvidia"
    ))
    info.gpu_available = info.gpu_count > 0

    # nvidia-smi truth (separate from TF probe — works on Linux/macOS too)
    smi = shutil.which("nvidia-smi")
    if smi:
        try:
            r = subprocess.run(
                [smi, "--query-gpu=driver_version", "--format=csv,noheader"],
                capture_output=True, text=True, timeout=5, check=False,
            )
            if r.returncode == 0 and r.stdout.strip():
                info.nvidia_driver = r.stdout.strip().splitlines()[0].strip()
                info.nvidia_smi_available = True
        except (OSError, subprocess.TimeoutExpired):
            pass

    if not info.ok and not info.error:
        info.error = tf_out.get("error", "engine GPU probe returned ok=false")
    return info


def _probe_host_only_native_gpu() -> EngineGpuInfo:
    """Host-only fallback when the managed venv isn't usable yet."""
    try:
        from vibechek.resources import detect  # noqa: PLC0415
        res = detect()
    except Exception as e:  # noqa: BLE001
        return EngineGpuInfo(engine="native", ok=False, error=f"{type(e).__name__}: {e}")

    devices = [
        EngineGpuDevice(
            name=g.name,
            backend=g.backend,
            memory_mb=g.memory_mb,
            vendor=getattr(g, "vendor", "nvidia"),
        )
        for g in res.gpu_devices
    ]
    # gpu_count and gpu_available stay NVIDIA-scoped — they mean "engine can
    # accelerate". A discovered AMD card adds a device row but not to the count.
    nvidia_count = sum(1 for d in devices if d.vendor == "nvidia")
    return EngineGpuInfo(
        engine="native",
        ok=True,
        gpu_available=nvidia_count > 0,
        gpu_count=nvidia_count,
        devices=devices,
        gpu_hardware_visible=nvidia_count > 0,
        nvidia_driver=res.cuda_runtime,
        nvidia_smi_available=res.cuda_runtime is not None,
    )


def _probe_native_bundled_gpu() -> EngineGpuInfo:
    """The Windows-native engine's honest CPU-only GPU story.

    The native engine runs the bundled ONNX Runtime IN-PROCESS in the sidecar's
    own Python. That wheel is CPU-only — no GPU execution providers are bundled
    (a roadmap item) — so it runs on CPU no matter what GPU the host has. We
    still note whether the host has GPU hardware so the panel can say "your card
    isn't used by this engine" rather than pretend there's no card.
    """
    info = EngineGpuInfo(
        engine="native",
        ok=True,
        gpu_available=False,
        runtime="bundled ONNX Runtime (CPU-only)",
        note="The bundled ONNX Runtime is CPU-only; GPU support is a roadmap item.",
    )
    try:
        from vibechek.resources import detect  # noqa: PLC0415
        res = detect()
        if res.gpu_devices:
            info.gpu_hardware_visible = True
            for g in res.gpu_devices:
                info.devices.append(EngineGpuDevice(
                    name=g.name, backend=g.backend, memory_mb=g.memory_mb,
                    vendor=getattr(g, "vendor", "nvidia"),
                ))
    except Exception as e:  # noqa: BLE001 — never break the probe on detection
        log.debug("host GPU inventory failed in native-bundled probe: %s", e)
    return info


def _probe_wsl_engine_gpu(distro: str) -> EngineGpuInfo:
    """Run the engine GPU probe script inside `distro`'s venv.

    The probe is split into two parts and they run in a single bash session:
      1. `nvidia-smi` inside WSL — instant, tells us if the GPU is even
         visible to the WSL kernel (driver passthrough is working).
      2. Python+TF inside the venv — slow (~10s), tells us if TF can use it.

    If step 1 fails (no nvidia-smi or no GPU visible), we skip step 2 since
    TF will definitely not see a GPU either.
    """
    info = EngineGpuInfo(engine="wsl", distro=distro)

    wsl = shutil.which("wsl") or shutil.which("wsl.exe")
    if not wsl:
        info.error = "wsl.exe not on PATH"
        return info

    # Shell script: emits `NVIDIA_DRIVER=<ver>` first (or `NVIDIA_DRIVER=`),
    # then `TF_JSON=<one-line-json>` on the last line. We parse both.
    script = r"""
set +e
HOME_DIR="$(printenv HOME)"
DRIVER=""
if command -v nvidia-smi >/dev/null 2>&1; then
    DRIVER="$(nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null | head -n1 | tr -d ' \r\n')"
fi
echo "NVIDIA_DRIVER=${DRIVER}"

VENV_PY="$HOME_DIR/.vibechek/venv/bin/python"
if [ ! -x "$VENV_PY" ]; then
    echo "TF_JSON={\"ok\":false,\"error\":\"venv python missing: $VENV_PY\"}"
    exit 0
fi
# Source the CUDA env file so TF sees the wheels we pip-installed via
# install_cuda_libs_in_wsl. Silent no-op if no GPU install happened yet.
. "$HOME_DIR/.vibechek/cuda-env.sh" 2>/dev/null || true
echo "TF_JSON=$("$VENV_PY" - <<'PY' 2>/dev/null
__PROBE__
PY
)"
"""
    # count=1: same defensive pattern as install_cuda_libs_in_wsl's
    # __TRY_CHAIN__ — a future comment mentioning the placeholder name
    # mustn't get its tail spliced into the bash heredoc body.
    script = script.replace("__PROBE__", _ENGINE_GPU_PROBE_PY, 1)

    try:
        # Same pattern as install / probe_distro: binary stdin, strip \r\n.
        proc = subprocess.Popen(
            [wsl, "-d", distro, "--", "bash", "-s"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        clean = script.replace("\r\n", "\n").replace("\r", "\n")
        # 60s timeout: covers slow TF import (~10s) + cold-start of stopped distro.
        stdout_bytes, stderr_bytes = proc.communicate(input=clean.encode("utf-8"), timeout=60)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except OSError:
            pass
        info.error = "engine GPU probe timed out after 60s"
        return info
    except Exception as e:  # noqa: BLE001
        info.error = f"engine GPU probe failed: {type(e).__name__}: {e}"
        return info

    # Decode like the other probes — WSL on some setups emits UTF-16 LE.
    stdout = ""
    for enc in ("utf-8", "utf-16-le", "cp1252"):
        try:
            stdout = stdout_bytes.decode(enc).replace("\x00", "")
            break
        except UnicodeDecodeError:
            continue

    if proc.returncode != 0:
        info.error = (
            f"engine GPU probe exited with {proc.returncode}: "
            f"{stderr_bytes[-500:].decode('utf-8', errors='replace')}"
        )
        return info

    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if line.startswith("NVIDIA_DRIVER="):
            val = line[len("NVIDIA_DRIVER="):].strip()
            if val:
                info.nvidia_driver = val
                info.nvidia_smi_available = True
        elif line.startswith("TF_JSON="):
            payload = line[len("TF_JSON="):].strip()
            if not payload:
                continue
            try:
                tf_out = json.loads(payload)
            except json.JSONDecodeError as e:
                info.error = f"could not parse TF probe JSON: {e}; raw={payload[:200]}"
                continue
            info.ok = bool(tf_out.get("ok"))
            info.tf_version = tf_out.get("tf_version")
            info.tf_built_with_cuda = tf_out.get("tf_built_with_cuda")
            info.gpu_hardware_visible = bool(tf_out.get("gpu_hardware_visible"))
            info.missing_cuda_libs = list(tf_out.get("missing_cuda_libs") or [])
            for d in tf_out.get("devices", []):
                info.devices.append(EngineGpuDevice(
                    name=d.get("device_name") or d.get("name", "?"),
                    backend="cuda",  # essentia's bundled TF is CUDA-only on Linux
                    compute_capability=d.get("compute_capability"),
                    vendor="nvidia",
                ))
            info.gpu_count = int(tf_out.get("gpu_count") or len(info.devices))
            # Layer 4: cross-vendor inventory. AMD/Intel cards discovered via
            # lspci inside WSL are appended *after* NVIDIA devices, with
            # backend="unknown" so the UI knows they're not accelerated.
            for og in tf_out.get("other_gpus", []):
                info.devices.append(EngineGpuDevice(
                    name=og.get("name", "?"),
                    backend={
                        "amd": "rocm",
                        "intel": "unknown",
                        "apple": "metal",
                    }.get(og.get("vendor", "unknown"), "unknown"),
                    vendor=og.get("vendor", "unknown"),
                ))
            # `gpu_available` means "TF will actually use this GPU at runtime".
            # When hardware is visible but TF skipped it (missing libs), that's
            # False — analyze will silently fall back to CPU.
            info.gpu_available = info.gpu_count > 0
            if not info.ok and not info.error:
                # Hand the user the most actionable error we have.
                err = tf_out.get("error") or tf_out.get("error_tf_layer")
                if not err and tf_out.get("cuda_libs"):
                    err = (
                        f"essentia bundled CUDA libs are present "
                        f"({len(tf_out['cuda_libs'])}) but no GPU was enumerated"
                    )
                info.error = err or "engine GPU probe returned ok=false"

    if not info.ok and not info.error:
        info.error = "engine GPU probe produced no parseable output"
    return info


def engine_gpu_info_to_dict(info: EngineGpuInfo) -> dict:
    return asdict(info)


# ---------------------------------------------------------------------------
# Path translation
# ---------------------------------------------------------------------------

_WIN_PATH_RE = re.compile(r"^([A-Za-z]):[/\\](.*)$")
_WSL_MNT_RE = re.compile(r"^/mnt/([a-z])(/.*)?$")
# UNC / network-share inputs: `\\server\share\...`, `//server/share/...`, or the
# `\\?\UNC\server\share\...` long-path form. These have no `/mnt/<drive>/`
# equivalent inside WSL (the share isn't mounted there), so analyze would either
# fail or silently scan zero files. We detect and refuse them with a clear,
# actionable error rather than passing the unusable path through.
_UNC_PATH_RE = re.compile(r"^(?:\\\\|//)(?:\?\\UNC\\|\?/UNC/)?[^\\/]")
# `\\?\` / `\\.\` long-path and device prefixes wrapping a normal drive path,
# e.g. `\\?\C:\Music`. We strip the prefix before translating.
_LONGPATH_DRIVE_RE = re.compile(r"^[\\/]{2}[?.][\\/]([A-Za-z]:[\\/].*)$")


class UnsupportedWslPathError(ValueError):
    """Raised when a Windows path can't be represented inside WSL.

    Subclasses ValueError so existing `except ValueError` handlers (and the
    RPC/analyzer error surfacing) catch it without code changes.
    """


def win_to_wsl_path(path: str) -> str:
    """Convert a Windows path to its WSL `/mnt/<drive>/...` form.

    Idempotent: returns the input unchanged if it doesn't look like a Win path.

    Raises `UnsupportedWslPathError` (a `ValueError`) for UNC / network-share
    paths (`\\\\server\\share\\...`), which have no `/mnt/<drive>/...` equivalent
    inside WSL — letting one through would make analyze fail opaquely or find
    zero files instead of telling the user what's wrong.
    """
    if not path:
        return path

    # Reject UNC / network-share paths up front with an actionable message.
    # `\\?\C:\...` (long-path-prefixed *drive* paths) are NOT UNC — strip the
    # prefix below and translate normally. `\\?\UNC\server\share` IS a share.
    longpath = _LONGPATH_DRIVE_RE.match(path)
    if longpath:
        path = longpath.group(1)
    elif _UNC_PATH_RE.match(path):
        raise UnsupportedWslPathError(
            "Network/UNC library paths aren't supported through WSL — "
            "copy the library to a local drive (e.g. C:\\ or D:\\) and try "
            f"again. Got: {path}"
        )

    m = _WIN_PATH_RE.match(path)
    if not m:
        return path  # Already WSL-style or unparseable
    drive = m.group(1).lower()
    rest = m.group(2).replace("\\", "/")
    # Windows silently strips trailing dots/spaces from each path component
    # (`C:\Foo.\Bar ` opens `C:\Foo\Bar`), but Linux treats them literally, so
    # the translated path wouldn't exist inside WSL. Normalize to match
    # Windows' own behaviour. We never strip a lone "." or ".." segment.
    parts = []
    for seg in rest.split("/"):
        if seg not in (".", ".."):
            seg = seg.rstrip(". ")
        parts.append(seg)
    rest = "/".join(parts)
    return f"/mnt/{drive}/{rest}"


def wsl_to_win_path(path: str) -> str:
    """Convert a WSL `/mnt/<drive>/...` path back to Windows form."""
    if not path:
        return path
    m = _WSL_MNT_RE.match(path)
    if not m:
        return path
    drive = m.group(1).upper()
    rest = (m.group(2) or "/").lstrip("/").replace("/", "\\")
    return f"{drive}:\\{rest}" if rest else f"{drive}:\\"


# ---------------------------------------------------------------------------
# Script staging — write bash scripts to a Windows tempfile WSL can read
# ---------------------------------------------------------------------------


def _stage_script_for_wsl(script: str) -> Path:
    """Write `script` to a Windows tempfile and return the host-side Path.

    The caller translates the path to WSL form (via `win_to_wsl_path`) and
    invokes `bash <wsl-path>` directly — *no stdin pipe*. This sidesteps two
    cross-platform bugs at once:

      1. apt postinst scripts (especially nvidia-cudnn's) read from stdin
         during their dpkg run. If we piped our bash script via stdin
         (`bash -s`), they'd eat bytes we hadn't read yet, truncating the
         script and causing mid-install syntax errors.
      2. `wsl.exe -c "<multi-line bash with $(...)>"` on Windows silently
         empties out variable assignments. Tempfile staging avoids needing
         `bash -c` at all.

    The script content is line-ending normalized (CRLF/CR → LF) before write
    because Drive sync can flip files to CRLF and Linux bash chokes on `\r`.

    Caller is responsible for `path.unlink(missing_ok=True)` after use.
    """
    import tempfile

    clean = script.replace("\r\n", "\n").replace("\r", "\n")
    # delete=False so we can close + reopen across the WSL subprocess boundary
    # without Windows tearing the file down between handle drops.
    fd = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", newline="\n",
        suffix=".sh", prefix="vibechek-wsl-", delete=False,
    )
    try:
        fd.write(clean)
    finally:
        fd.close()
    return Path(fd.name)


# ---------------------------------------------------------------------------
# Install: WSL itself
# ---------------------------------------------------------------------------


def _start_cancellation_watchdog(
    proc: subprocess.Popen,
    *,
    on_cancel: Callable[[], None] | None = None,
) -> tuple[threading.Event, dict[str, bool]]:
    """Start a daemon thread that terminates `proc` when cancellation flips.

    Polls `cancellation.is_cancelled()` every 500ms — the same cadence as
    `run_vibechek_in_wsl`. Without this, the 5 install functions could run for
    up to 30 minutes after the user clicked Cancel (the long-op lock would
    stay held that whole time). See `_LONG_OP_LOCK` in rpc.py.

    Returns `(cancel_event, state)`. Caller signals work done by `cancel_event.set()`;
    `state["v"]` is True if cancellation actually fired.

    `on_cancel` runs *before* the process termination — used by WSL ops to
    `wsl kill -TERM` the bash process group via the token-file pattern, so
    workers inside the distro die before we lose the parent's PID handle.
    """
    from vibechek import cancellation  # noqa: PLC0415

    cancel_done = threading.Event()
    state: dict[str, bool] = {"v": False}

    def _watch() -> None:
        while not cancel_done.is_set() and proc.poll() is None:
            if cancellation.is_cancelled():
                state["v"] = True
                log.info(
                    "Install subprocess cancellation requested — terminating PID %s",
                    proc.pid,
                )
                if on_cancel is not None:
                    try:
                        on_cancel()
                    except Exception:  # noqa: BLE001
                        log.exception("on_cancel hook raised")
                try:
                    proc.terminate()
                except OSError:
                    pass
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    try:
                        proc.kill()
                    except OSError:
                        pass
                return
            cancel_done.wait(0.5)

    threading.Thread(target=_watch, daemon=True).start()
    return cancel_done, state


def _kill_wsl_pgid(wsl_exe: str, distro: str, token_file: Path) -> None:
    """Tear down the bash + child process group running inside `distro`.

    Same pattern as `run_vibechek_in_wsl._kill_wsl_tree`: reads the PID we
    wrote at launch (via setsid, so bash is the process-group leader) and
    sends SIGTERM then SIGKILL to the negative pgid. Just terminating the
    Windows-side wsl.exe wrapper would leave bash + workers running until the
    WSL VM eventually reaps them.
    """
    if not token_file.exists():
        return
    try:
        bash_pid = token_file.read_text(encoding="utf-8").strip()
    except OSError:
        return
    if not bash_pid.isdigit():
        return
    for sig in ("-TERM", "-KILL"):
        try:
            subprocess.run(
                [wsl_exe, "-d", distro, "--", "kill", sig, f"-{bash_pid}"],
                capture_output=True, timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
        if sig == "-TERM":
            time.sleep(0.5)


def install_wsl(
    distro: str = "Ubuntu-24.04",
    on_progress: ProgressCallback | None = None,
) -> dict:
    """Install WSL + a default distro via elevated PowerShell.

    Triggers a UAC prompt. Blocks until the install completes (or fails). The
    user may need to reboot afterward.

    Cooperatively cancellable: a watchdog polls `cancellation.is_cancelled()`
    every 500ms and terminates the PowerShell wrapper if the user cancels.
    Note that the elevated wsl.exe spawned by Start-Process runs in a separate
    UAC-elevated process — once UAC is granted we can only terminate our
    PowerShell launcher; the actual WSL install may continue under its own
    SYSTEM/elevated process. Best-effort.

    Returns a dict suitable for direct RPC return.
    """
    if not IS_WINDOWS:
        return {"ok": False, "error": "Not running on Windows"}

    # `distro` is interpolated into a PowerShell command string below. A single
    # quote (or any shell metacharacter) would escape the literal and run
    # arbitrary PowerShell behind the UAC elevation. Real WSL distro names only
    # use letters, digits, dot, underscore, and hyphen (e.g. "Ubuntu-24.04"),
    # so we hard-reject anything else. The RPC boundary adds the same guard.
    if not _VALID_DISTRO_RE.match(distro):
        return {
            "ok": False,
            "error": (
                f"Invalid distro name {distro!r}: only letters, digits, "
                f"'.', '_', and '-' are allowed."
            ),
        }

    from vibechek import cancellation  # noqa: PLC0415

    if on_progress:
        on_progress(0, 100, "Requesting admin elevation...")

    # `Start-Process -Verb RunAs` shows the UAC prompt. We `-Wait` so the
    # parent process blocks until the elevated install finishes. We capture
    # the exit code through the PowerShell exit code.
    ps_command = (
        f"$p = Start-Process -FilePath 'wsl.exe' "
        f"-ArgumentList '--install','-d','{distro}' "
        f"-Verb RunAs -Wait -PassThru; exit $p.ExitCode"
    )

    if on_progress:
        on_progress(10, 100, "Installing WSL (admin prompt + ~5-15 min download)...")

    try:
        proc = subprocess.Popen(
            ["powershell.exe", "-NoProfile", "-Command", ps_command],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as e:
        return {"ok": False, "error": f"Could not invoke PowerShell: {e}"}

    cancel_done, cancel_state = _start_cancellation_watchdog(proc)
    try:
        stdout, stderr = proc.communicate(timeout=60 * 30)
        rc = proc.returncode
    except subprocess.TimeoutExpired:
        proc.kill()
        cancel_done.set()
        return {"ok": False, "error": "WSL install timed out after 30 min"}
    cancel_done.set()

    if cancel_state["v"] or cancellation.is_cancelled():
        return {"ok": False, "error": "Cancelled by user", "cancelled": True}

    if rc != 0:
        return {
            "ok": False,
            "error": f"WSL install exited with {rc}",
            "stderr": (stderr or "")[-2000:],
        }

    if on_progress:
        on_progress(100, 100, "WSL installed")

    return {
        "ok": True,
        "distro": distro,
        "note": "Vibechek may need a reboot to fully initialize WSL.",
    }


# ---------------------------------------------------------------------------
# Install: essentia + vibechek inside an existing distro
# ---------------------------------------------------------------------------


# Phase 1: apt installs. Runs as root via `wsl -u root` — sidesteps the
# "non-interactive sudo needs a password" issue some Ubuntu WSL installs have.
_ROOT_BOOTSTRAP = r"""
set -e
export DEBIAN_FRONTEND=noninteractive

echo "[1/4] Updating apt..."
apt-get update -y -q

echo "[2/4] Installing system deps..."
apt-get install -y -q --no-install-recommends \
    python3 python3-pip python3-venv \
    libchromaprint-tools \
    git ca-certificates

echo "ROOT_DONE"
"""

# Phase 2: venv + pip. Runs as the default user so files land in their home.
# `$DEFAULT_USER_HOME` is filled in by Python before the script is sent.
def _user_bootstrap(engine: str = "essentia_tf") -> str:
    """Phase-2 (pip-as-user) install script for the given inference engine.

    "essentia_tf" → ``~/.vibechek/venv`` with **essentia-tensorflow** (the
    default; NVIDIA-only GPU). "onnx" AND "native" → ``~/.vibechek/venv-onnx``
    with **plain essentia + onnxruntime** (the TF-free stack; native is the
    same ONNX backbone/heads, so its WSL fallback runs the identical venv —
    installing essentia-tensorflow into "venv" for it left preflight, which
    correctly gates native on venv-onnx, reporting "not ready" after a
    10-minute install). The two essentia builds can't share a venv (both ship
    the ``essentia`` module). `run_vibechek_in_wsl(venv_subdir=...)` routes
    analyze to whichever the user picked.

    Re-running "Set up" is an idempotent version-drift fix: the vibechek package
    is force-reinstalled from this build's release tag (a plain ``--upgrade``
    does NOT re-pull a ``git+`` install once any version is present), so every
    code-side bump becomes available without deleting the venv.
    """
    from vibechek.config import vibechek_pip_source  # noqa: PLC0415

    src = vibechek_pip_source()
    subdir = engine_venv_subdir(engine)
    pip = f'"$HOME/.vibechek/{subdir}/bin/pip"'
    if subdir == "venv-onnx":
        # GPU acceleration is the whole point of the ONNX engine. When an NVIDIA
        # GPU is visible (nvidia-smi), install onnxruntime-gpu + the CUDA 12
        # runtime wheels — the CUDA EP's libs, loaded at runtime via
        # onnxruntime.preload_dlls() (see onnx_backend.load_onnx_models).
        # Otherwise fall back to CPU onnxruntime. (AMD/ROCm is a future variant.)
        cuda_wheels = (
            "nvidia-cuda-runtime-cu12 nvidia-cudnn-cu12 nvidia-cublas-cu12 "
            "nvidia-cufft-cu12 nvidia-curand-cu12 nvidia-cusparse-cu12 "
            "nvidia-cuda-nvrtc-cu12"
        )
        ml_line = (
            # Clean swap: onnxruntime / -gpu / -rocm all ship the same `onnxruntime`
            # module, so re-running setup (e.g. CPU→GPU after this fix) must drop
            # the old one first, else two distributions clobber each other.
            f'{pip} uninstall -y onnxruntime onnxruntime-gpu onnxruntime-rocm >/dev/null 2>&1 || true\n'
            'if command -v nvidia-smi >/dev/null 2>&1; then\n'
            f'    echo "  NVIDIA GPU detected — installing onnxruntime-gpu + CUDA 12 runtime"\n'
            # The pin is DOUBLE-quoted for bash so the `<` in "<1.27" is a literal
            # part of the pip version specifier, not a shell input redirect. See
            # ONNXRUNTIME_GPU_SPEC for why an unpinned install hard-crashes on cu12.
            f'    {pip} install --quiet essentia "{ONNXRUNTIME_GPU_SPEC}" {cuda_wheels}\n'
            'else\n'
            f'    echo "  No NVIDIA GPU — installing CPU onnxruntime"\n'
            f'    {pip} install --quiet essentia onnxruntime\n'
            'fi'
        )
        label = "plain essentia + onnxruntime (TF-free ONNX engine)"
    else:
        ml_line = f'{pip} install --quiet essentia-tensorflow'
        label = "essentia-tensorflow"
    # The ~/.local/bin symlink only tracks the DEFAULT venv; the onnx/native
    # engines are invoked by their explicit venv path (run_vibechek_in_wsl),
    # so we don't repoint the shared symlink for them.
    if subdir == "venv-onnx":
        symlink_block = ""
    else:
        symlink_block = (
            'mkdir -p "$HOME/.local/bin"\n'
            f'ln -sf "$HOME/.vibechek/{subdir}/bin/vibechek" "$HOME/.local/bin/vibechek"\n'
            'case ":$PATH:" in\n'
            '  *":$HOME/.local/bin:"*) ;;\n'
            '  *) echo \'export PATH="$HOME/.local/bin:$PATH"\' >> "$HOME/.bashrc" ;;\n'
            'esac\n'
        )
    return f"""set -e

echo "[3/4] Creating ~/.vibechek/{subdir} venv..."
mkdir -p "$HOME/.vibechek"
if [ ! -d "$HOME/.vibechek/{subdir}" ]; then
    python3 -m venv "$HOME/.vibechek/{subdir}"
fi

echo "[4/4] Installing Vibechek + {label} (this is the slow part)..."
{pip} install --upgrade --quiet pip wheel
{ml_line}
{pip} install --upgrade --quiet {src}
# `--upgrade` alone does NOT re-pull a git+ (VCS) install when a version is
# already present — pip treats the URL requirement as satisfied without cloning
# to compare. So re-running "Set up WSL" on a drifted install would silently
# leave the stale package and never clear the analyzer's version-drift guard.
# Force-reinstall just the vibechek package (its deps were handled above) so a
# re-run always lands on this build's release tag. Matches upgrade_vibechek_in_wsl.
{pip} install --upgrade --force-reinstall --no-deps --quiet {src}

{symlink_block}
echo "DONE"
"$HOME/.vibechek/{subdir}/bin/vibechek" --version
"$HOME/.vibechek/{subdir}/bin/python" -c "import essentia; print('essentia OK', essentia.__version__)"
"""


# Back-compat: the default-engine script as a module constant (referenced by
# the existing tests + the cold-install path).
_USER_BOOTSTRAP = _user_bootstrap("essentia_tf")


def install_vibechek_in_wsl(
    distro: str,
    on_progress: ProgressCallback | None = None,
    engine: str = "essentia_tf",
) -> dict:
    """Install vibechek + essentia + chromaprint inside `distro`.

    Runs in two phases to avoid the "sudo needs a password" trap:
      1. apt installs as root (via `wsl -u root`)
      2. venv + pip as the default user (files land in their home)

    Streams each script's stdout to `on_progress` line-by-line so the GUI
    shows live install progress.
    """
    if not IS_WINDOWS:
        return {"ok": False, "error": "Not running on Windows"}

    wsl = shutil.which("wsl") or shutil.which("wsl.exe")
    if not wsl:
        return {"ok": False, "error": "wsl.exe not found"}

    from vibechek import cancellation  # noqa: PLC0415

    if on_progress:
        on_progress(0, 100, f"Starting install inside {distro}...")

    # Step progress map across BOTH phases (same [N/4] markers).
    step_pct = {"[1/4]": 10, "[2/4]": 25, "[3/4]": 40, "[4/4]": 60, "DONE": 95}
    full_tail: list[str] = []

    def _run_phase(
        distro_args: list[str],
        script: str,
        label: str,
        run_as_root: bool,
    ) -> tuple[int, list[str], bool]:
        """Stage `script` to a Windows tempfile, run via `wsl bash <wsl-path>`.

        Why not pipe via stdin? See `_stage_script_for_wsl` — apt postinst
        scripts read from stdin and would eat parts of our bash script.
        Stdin is closed for the WSL bash process; the script is read from
        the staged file instead.

        Wraps the install in a setsid+token-file launcher so the cancellation
        watchdog can pkill the bash process group inside WSL — without this,
        a Cancel click leaves apt / pip running for the full 30-min timeout.
        Returns `(rc, tail, cancelled)`.
        """
        import tempfile as _tempfile

        if on_progress:
            on_progress(step_pct.get("[1/4]" if "ROOT" in label else "[3/4]", 0), 100, label)
        # Token file: setsid writes the bash pgid here; the watchdog reads it
        # and `wsl kill -TERM -<pgid>` to take down apt / pip cleanly.
        token_file = Path(_tempfile.gettempdir()) / (
            f"vibechek-wsl-install-pid-{os.getpid()}-{id(script)}.txt"
        )
        wsl_token = win_to_wsl_path(str(token_file))
        # Stage the inner script FIRST and keep its path in a variable. The old
        # code inlined `_stage_script_for_wsl(script)` inside the f-string and
        # then reverse-engineered the path by splitting the launcher's last
        # line on whitespace — which broke when `_shell_quote` wrapped a path
        # containing a space (e.g. `%TEMP%` under `C:\Users\name with spaces\`),
        # leaking the tempfile and potentially unlinking the wrong path.
        inner_script_path = _stage_script_for_wsl(script)
        inner_script_wsl = win_to_wsl_path(str(inner_script_path))
        launcher = (
            "#!/usr/bin/env bash\n"
            "set -e\n"
            f'echo $$ > {_shell_quote(wsl_token)}\n'
            'trap "kill -TERM 0 2>/dev/null; exit 130" SIGTERM SIGINT\n'
            f"exec bash {_shell_quote(inner_script_wsl)}\n"
        )
        launcher_path = _stage_script_for_wsl(launcher)
        wsl_launcher = win_to_wsl_path(str(launcher_path))
        try:
            try:
                # `setsid -w`: WAIT for the child instead of fork-and-exit.
                # Without -w, setsid backgrounds bash and exits 0 immediately —
                # wsl.exe sees instant success + EOF, the stdout loop ends, and
                # `proc.wait()` returns 0 while apt/pip run orphaned inside WSL.
                # The GUI then reports "Install complete" before anything is
                # actually installed. This is the exact landmine documented in
                # run_vibechek_in_wsl; both install paths must match it.
                proc = subprocess.Popen(
                    distro_args + ["setsid", "-w", "bash", wsl_launcher],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                )
            except OSError as e:
                return -1, [f"Could not invoke wsl: {e}"], False
            assert proc.stdout

            cancel_done, cancel_state = _start_cancellation_watchdog(
                proc,
                on_cancel=lambda: _kill_wsl_pgid(wsl, distro, token_file),
            )

            tail: list[str] = []
            for raw_line in proc.stdout:
                line = raw_line.decode("utf-8", errors="replace")
                stripped = line.rstrip("\r\n")
                tail.append(stripped)
                full_tail.append(stripped)
                if len(full_tail) > 200:
                    full_tail.pop(0)
                for marker, pct in step_pct.items():
                    if stripped.startswith(marker):
                        if on_progress:
                            on_progress(pct, 100, stripped)
                        break
            try:
                rc = proc.wait(timeout=60 * 30)
            except subprocess.TimeoutExpired:
                proc.kill()
                cancel_done.set()
                return -1, tail + ["Killed after 30 min timeout"], False
            cancel_done.set()
            return rc, tail, cancel_state["v"]
        finally:
            launcher_path.unlink(missing_ok=True)
            # Clean up the inner script via the Path we captured up front (no
            # more fragile reparse of the launcher text).
            inner_script_path.unlink(missing_ok=True)
            token_file.unlink(missing_ok=True)

    # ---- Phase 1: apt as root ----
    log.info("WSL install phase 1 (apt as root) in %s", distro)
    rc, tail, cancelled = _run_phase(
        [wsl, "-d", distro, "-u", "root", "--"],
        _ROOT_BOOTSTRAP,
        "Phase 1: ROOT — installing system packages",
        run_as_root=True,
    )
    if cancelled or cancellation.is_cancelled():
        return {"ok": False, "error": "Cancelled by user", "cancelled": True,
                "tail": "\n".join(full_tail)}
    if rc != 0:
        return {
            "ok": False,
            "error": _explain_install_failure(rc, tail, phase="apt"),
            "tail": "\n".join(full_tail),
        }

    # ---- Phase 2: pip as default user ----
    log.info("WSL install phase 2 (pip as default user) in %s", distro)
    rc, tail, cancelled = _run_phase(
        [wsl, "-d", distro, "--"],
        _user_bootstrap(engine),
        f"Phase 2: USER — installing Vibechek + {'plain essentia + onnxruntime' if engine == 'onnx' else 'essentia-tensorflow'} (slow)",
        run_as_root=False,
    )
    if cancelled or cancellation.is_cancelled():
        return {"ok": False, "error": "Cancelled by user", "cancelled": True,
                "tail": "\n".join(full_tail)}
    if rc != 0:
        return {
            "ok": False,
            "error": _explain_install_failure(rc, tail, phase="pip"),
            "tail": "\n".join(full_tail),
        }

    if on_progress:
        on_progress(100, 100, "Install complete")

    return {"ok": True, "distro": distro, "tail": "\n".join(full_tail)}


# Fast path that only re-installs vibechek itself (no apt, no essentia
# rebuild). Designed for the version-drift case: the WSL install is healthy
# but stuck on an older code revision, so we just bump the package and skip
# the 5-10 minute essentia re-download. The full bootstrap remains the
# correct entry for cold installs.
def _vibechek_upgrade_script(venv_subdir: str = "venv") -> str:
    """Fast vibechek-package-only upgrade script for `~/.vibechek/<venv_subdir>`.

    `venv_subdir` is "venv" (essentia-tensorflow engine) or "venv-onnx" (the
    TF-free stack shared by the onnx and native engines) so the auto-update
    targets whichever venv the analyze will actually use.
    """
    from vibechek.config import vibechek_pip_source  # noqa: PLC0415

    src = vibechek_pip_source()
    return f"""
set -e

VENV="$HOME/.vibechek/{venv_subdir}"
if [ ! -x "$VENV/bin/pip" ]; then
    echo "ERROR: $VENV is missing — run the full WSL setup first" >&2
    exit 2
fi

echo "[1/1] Upgrading vibechek package (essentia + apt unchanged)..."
# Pinned to THIS build's release tag so the drift guard converges the WSL
# install onto exactly the sidecar's version — installing `main` HEAD here
# could land code newer than the sidecar with an incompatible wire schema.
# --force-reinstall guarantees the re-pull even when pip's resolver sees the
# existing version as satisfying the requirement. --no-deps keeps us from
# touching essentia/numpy/tensorflow, which is the whole point of the fast
# path.
"$VENV/bin/pip" install --upgrade --force-reinstall --no-deps --quiet \\
    {src}

echo "DONE"
"$VENV/bin/vibechek" --version
"""


def upgrade_vibechek_in_wsl(
    distro: str,
    on_progress: ProgressCallback | None = None,
    engine: str = "essentia_tf",
) -> dict:
    """Re-install vibechek inside `distro` from GitHub, skipping apt + essentia.

    `engine` selects the venv to upgrade: "essentia_tf" → ~/.vibechek/venv,
    "onnx"/"native" → ~/.vibechek/venv-onnx (so auto-update targets the venv
    the analyze actually uses).

    This is the fast repair path for version drift: when the sidecar is on
    v0.4.0-beta.3 but the WSL install is stuck on v0.4.0-beta.1, the user can
    click "Update WSL install" without paying for a full apt + essentia
    re-install (~5-10 min). Apt and essentia don't change between betas, only
    the Python code does.

    Returns the same shape as `install_vibechek_in_wsl` so the GUI can route
    both through the same progress / error UI.
    """
    if not IS_WINDOWS:
        return {"ok": False, "error": "Not running on Windows"}
    wsl = shutil.which("wsl") or shutil.which("wsl.exe")
    if not wsl:
        return {"ok": False, "error": "wsl.exe not found"}

    from vibechek import cancellation  # noqa: PLC0415

    if on_progress:
        on_progress(0, 100, f"Upgrading vibechek inside {distro}...")

    tail_lines: list[str] = []

    # We reuse the same setsid+token-file launcher pattern as
    # install_vibechek_in_wsl so cancellation works identically. Inlining the
    # subset of `_run_phase` we need keeps this function self-contained.
    import tempfile as _tempfile  # noqa: PLC0415
    token_file = Path(_tempfile.gettempdir()) / (
        f"vibechek-wsl-upgrade-pid-{os.getpid()}.txt"
    )
    wsl_token = win_to_wsl_path(str(token_file))
    venv_subdir = engine_venv_subdir(engine)
    inner_script = _stage_script_for_wsl(_vibechek_upgrade_script(venv_subdir))
    inner_wsl = win_to_wsl_path(str(inner_script))
    launcher = (
        "#!/usr/bin/env bash\n"
        "set -e\n"
        f'echo $$ > {_shell_quote(wsl_token)}\n'
        'trap "kill -TERM 0 2>/dev/null; exit 130" SIGTERM SIGINT\n'
        f"exec bash {_shell_quote(inner_wsl)}\n"
    )
    launcher_path = _stage_script_for_wsl(launcher)
    wsl_launcher = win_to_wsl_path(str(launcher_path))

    try:
        proc = subprocess.Popen(
            [wsl, "-d", distro, "--", "setsid", "-w", "bash", wsl_launcher],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
    except OSError as e:
        return {"ok": False, "error": f"Could not invoke wsl: {e}"}
    assert proc.stdout

    cancel_done, cancel_state = _start_cancellation_watchdog(
        proc, on_cancel=lambda: _kill_wsl_pgid(wsl, distro, token_file),
    )

    try:
        for raw in proc.stdout:
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            tail_lines.append(line)
            if line.startswith("[1/1]") and on_progress:
                on_progress(40, 100, line)
            if line == "DONE" and on_progress:
                on_progress(95, 100, line)
        try:
            rc = proc.wait(timeout=60 * 10)  # 10 min cap — pure pip install
        except subprocess.TimeoutExpired:
            proc.kill()
            cancel_done.set()
            return {"ok": False, "error": "Upgrade killed after 10 min timeout",
                    "tail": "\n".join(tail_lines)}
        cancel_done.set()
    finally:
        launcher_path.unlink(missing_ok=True)
        inner_script.unlink(missing_ok=True)
        token_file.unlink(missing_ok=True)

    if cancel_state["v"] or cancellation.is_cancelled():
        return {"ok": False, "error": "Cancelled by user", "cancelled": True,
                "tail": "\n".join(tail_lines)}
    if rc != 0:
        return {"ok": False, "error": f"vibechek upgrade exited with {rc}",
                "tail": "\n".join(tail_lines)}

    # Post-upgrade honesty gate. This fast path is a `--no-deps` code-only
    # reinstall (apt + essentia deliberately untouched — only the Python code
    # changes between betas), so it CANNOT reconcile an ML-dependency skew
    # carried over from the initial install (e.g. an onnxruntime-gpu build stuck
    # against the wrong CUDA runtime). Printing "Upgrade complete" while
    # `import onnxruntime` / `import essentia` still crashes is the status-lie
    # the audit flagged. Verify the venv can import its ML stack; if not, DON'T
    # claim success — return ok:False carrying the REAL import error plus a
    # `stack_broken` flag so the analyzer's self-heal (ensure_engine_runtime)
    # repairs it in place rather than the user hitting a raw crash on analyze.
    stack_ok, stack_detail = _probe_engine_stack_import(distro, engine)
    if not stack_ok:
        return {
            "ok": False,
            "stack_broken": True,
            "stack_error": stack_detail,
            "error": (
                f"The WSL vibechek code updated, but the {engine} engine can't "
                f"import its ML libraries in {distro}: {stack_detail}. This is a "
                f"dependency skew the code-only update can't fix; it will be "
                f"repaired automatically."
            ),
            "distro": distro,
            "tail": "\n".join(tail_lines),
        }

    if on_progress:
        on_progress(100, 100, "Upgrade complete")
    return {"ok": True, "distro": distro, "tail": "\n".join(tail_lines)}


# ---------------------------------------------------------------------------
# Opt-in genre engines: CLAP audio student + the online web-synthesis resolver
# ---------------------------------------------------------------------------

# Pinned no-sudo Ollama release (the WSL distro default user has no passwordless
# sudo, so we install the standalone tarball into ~/ollama). Bump on maintenance.
_OLLAMA_RELEASE = "v0.30.4"
_OLLAMA_TARBALL_URL = (
    f"https://github.com/ollama/ollama/releases/download/{_OLLAMA_RELEASE}/"
    "ollama-linux-amd64.tar.zst"
)
# Per-asset SHA256 for the pinned release, from the release's own asset
# digests (also in its sha256sum.txt). Bump together with _OLLAMA_RELEASE.
# Verified after download in both the WSL script and the native setup — the
# tarball is unpacked and EXECUTED as a long-lived local server, so it gets
# the same content pinning as every other fetched artifact.
_OLLAMA_TARBALL_SHA256 = {
    "ollama-linux-amd64.tar.zst":
        "78e317889c907d9853336c8d834f424c7dc6ccd8958772f44fadf78f421ea907",
    "ollama-linux-arm64.tar.zst":
        "877981499ab2ccc8ffd674a5c2fe1788ebd67a4c31df8d399fca3a488072e551",
    "ollama-darwin.tgz":
        "fa36a0e6fbf5716a5cc85ad15454862a987adbc2bed9e9ef82f1e9d77e082554",
}


def _clap_setup_script(venv_subdir: str, ckpt_url: str, ckpt_sha256: str) -> str:
    """Install the CLAP deps into the analysis venv + fetch the checkpoint."""
    return f"""
set -e
VENV="$HOME/.vibechek/{venv_subdir}"
[ -x "$VENV/bin/pip" ] || {{ echo "ERROR: $VENV missing — run the WSL setup first" >&2; exit 2; }}
echo "[1/3] Installing CLAP deps (torch, torchvision, laion-clap)..."
"$VENV/bin/pip" install --quiet torch torchvision --index-url https://download.pytorch.org/whl/cpu \
  || "$VENV/bin/pip" install --quiet torch torchvision
"$VENV/bin/pip" install --quiet laion-clap soundfile
echo "[2/3] Downloading CLAP checkpoint (~2.2 GB, one-time)..."
mkdir -p "$HOME/.vibechek/clap"
CKPT="$HOME/.vibechek/clap/music_clap.pt"
# Clean the partial on ANY exit (cancel/SIGTERM included) so a cancelled run
# doesn't strand 2 GB in the WSL home; on success the mv already happened so
# the rm is a no-op. --speed-limit turns a stalled TCP connection into a fast
# failure instead of an indefinite hang (the Python-side timeout only starts
# counting after stdout EOF).
trap 'rm -f "$CKPT.partial"' EXIT
if [ ! -s "$CKPT" ]; then
  curl -fSL --speed-limit 1024 --speed-time 60 "{ckpt_url}" -o "$CKPT.partial"
  SIZE=$(stat -c%s "$CKPT.partial" 2>/dev/null || echo 0)
  if [ "$SIZE" -lt 1500000000 ]; then
    echo "ERROR: checkpoint download incomplete ($SIZE bytes)" >&2
    exit 3
  fi
  # Content-hash gate (the checkpoint is a torch pickle — executable on
  # load): a size floor alone is not integrity. Mirrors the SHA256 pin the
  # native setup + load_clap_model enforce.
  echo "Verifying checkpoint SHA256..."
  echo "{ckpt_sha256}  $CKPT.partial" | sha256sum -c - >/dev/null || {{
    echo "ERROR: checkpoint failed SHA256 verification — refusing to install it" >&2
    exit 4
  }}
  mv "$CKPT.partial" "$CKPT"
fi
echo "[3/3] Verifying..."
"$VENV/bin/python" -c "import laion_clap, soundfile; print('clap import ok')"
echo "DONE"
"""


def _resolver_setup_script(venv_subdir: str, model: str) -> str:
    """Install ddgs + a no-sudo Ollama + pull the model + start the server."""
    return f"""
set -e
VENV="$HOME/.vibechek/{venv_subdir}"
[ -x "$VENV/bin/pip" ] || {{ echo "ERROR: $VENV missing — run the WSL setup first" >&2; exit 2; }}
echo "[1/4] Installing ddgs + zstandard..."
"$VENV/bin/pip" install --quiet ddgs zstandard
echo "[2/4] Installing Ollama (no-sudo)..."
trap 'rm -f "$HOME/ollama.tar.zst" "$HOME/ollama.tar"' EXIT
if [ ! -x "$HOME/ollama/bin/ollama" ]; then
  curl -fSL --speed-limit 1024 --speed-time 60 "{_OLLAMA_TARBALL_URL}" -o "$HOME/ollama.tar.zst"
  # Content-hash gate: this tarball is unpacked and run as a local server.
  echo "{_OLLAMA_TARBALL_SHA256["ollama-linux-amd64.tar.zst"]}  $HOME/ollama.tar.zst" | sha256sum -c - >/dev/null || {{
    echo "ERROR: Ollama tarball failed SHA256 verification — refusing to install it" >&2
    exit 4
  }}
  "$VENV/bin/python" -c "import zstandard,sys; fi=open('$HOME/ollama.tar.zst','rb'); fo=open('$HOME/ollama.tar','wb'); zstandard.ZstdDecompressor().copy_stream(fi,fo)"
  mkdir -p "$HOME/ollama"; tar -xf "$HOME/ollama.tar" -C "$HOME/ollama"
  rm -f "$HOME/ollama.tar" "$HOME/ollama.tar.zst"
fi
echo "[3/4] Starting Ollama + pulling {model} (~4.7 GB, one-time)..."
mkdir -p "$HOME/.vibechek"
if ! curl -s --max-time 3 http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then
  OLLAMA_HOST=127.0.0.1:11434 nohup "$HOME/ollama/bin/ollama" serve >"$HOME/.vibechek/ollama.log" 2>&1 &
  for i in $(seq 1 20); do curl -s --max-time 2 http://127.0.0.1:11434/api/tags >/dev/null 2>&1 && break; sleep 1; done
fi
OLLAMA_HOST=127.0.0.1:11434 "$HOME/ollama/bin/ollama" pull {model}
echo "[4/4] Verifying..."
curl -s --max-time 5 http://127.0.0.1:11434/api/tags | head -c 60
echo ""
echo "DONE"
"""


def _run_managed_wsl_script(
    distro: str,
    inner_script: str,
    on_progress: ProgressCallback | None,
    timeout_s: int,
    start_msg: str,
) -> dict:
    """Run a one-off setup `inner_script` inside `distro` with cancellation +
    `[N/M] ...` progress parsing. Mirrors `upgrade_vibechek_in_wsl`'s launcher
    (setsid + token-file so Cancel kills the whole process group). Returns the
    same dict shape as the install/upgrade helpers."""
    if not IS_WINDOWS:
        return {"ok": False, "error": "Not running on Windows"}
    wsl = shutil.which("wsl") or shutil.which("wsl.exe")
    if not wsl:
        return {"ok": False, "error": "wsl.exe not found"}

    import re as _re  # noqa: PLC0415
    import tempfile as _tempfile  # noqa: PLC0415

    from vibechek import cancellation  # noqa: PLC0415

    if on_progress:
        on_progress(0, 100, start_msg)
    tail_lines: list[str] = []
    token_file = Path(_tempfile.gettempdir()) / f"vibechek-wsl-setup-pid-{os.getpid()}.txt"
    wsl_token = win_to_wsl_path(str(token_file))
    inner_path = _stage_script_for_wsl(inner_script)
    inner_wsl = win_to_wsl_path(str(inner_path))
    launcher = (
        "#!/usr/bin/env bash\nset -e\n"
        f'echo $$ > {_shell_quote(wsl_token)}\n'
        'trap "kill -TERM 0 2>/dev/null; exit 130" SIGTERM SIGINT\n'
        f"exec bash {_shell_quote(inner_wsl)}\n"
    )
    launcher_path = _stage_script_for_wsl(launcher)
    wsl_launcher = win_to_wsl_path(str(launcher_path))
    try:
        proc = subprocess.Popen(
            [wsl, "-d", distro, "--", "setsid", "-w", "bash", wsl_launcher],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
    except OSError as e:
        return {"ok": False, "error": f"Could not invoke wsl: {e}"}
    assert proc.stdout
    cancel_done, cancel_state = _start_cancellation_watchdog(
        proc, on_cancel=lambda: _kill_wsl_pgid(wsl, distro, token_file),
    )
    marker = _re.compile(r"^\[(\d+)/(\d+)\]\s*(.*)")
    try:
        for raw in proc.stdout:
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            tail_lines.append(line)
            m = marker.match(line)
            if m and on_progress:
                cur, tot = int(m.group(1)), int(m.group(2))
                on_progress(int(cur / max(tot, 1) * 95), 100, m.group(3) or line)
            elif line == "DONE" and on_progress:
                on_progress(98, 100, "Finalizing…")
        try:
            rc = proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            # Kill the WSL-side process GROUP first — proc.kill() only takes
            # down wsl.exe on the Windows side, orphaning pip/curl/ollama
            # inside the VM.
            _kill_wsl_pgid(wsl, distro, token_file)
            proc.kill(); cancel_done.set()
            return {"ok": False, "error": f"Setup timed out after {timeout_s}s",
                    "tail": "\n".join(tail_lines[-40:])}
        cancel_done.set()
    finally:
        launcher_path.unlink(missing_ok=True)
        inner_path.unlink(missing_ok=True)
        token_file.unlink(missing_ok=True)

    if cancel_state["v"] or cancellation.is_cancelled():
        return {"ok": False, "error": "Cancelled by user", "cancelled": True,
                "tail": "\n".join(tail_lines[-40:])}
    if rc != 0:
        return {"ok": False, "error": f"Setup exited with {rc}",
                "tail": "\n".join(tail_lines[-40:])}
    if on_progress:
        on_progress(100, 100, "Setup complete")
    return {"ok": True, "distro": distro, "tail": "\n".join(tail_lines[-40:])}


def setup_clap_in_wsl(distro: str, on_progress: ProgressCallback | None = None,
                      engine: str = "essentia_tf") -> dict:
    """Install the CLAP genre student into the analysis venv inside `distro`."""
    from vibechek.clap_genre import _CHECKPOINT_SHA256, _CHECKPOINT_URL  # noqa: PLC0415
    venv_subdir = engine_venv_subdir(engine)
    # 2 h wall clock: these are multi-GB downloads on arbitrary connections.
    # Real stalls die fast via curl --speed-limit, so the generous ceiling only
    # bounds genuinely slow-but-progressing links instead of aborting them.
    return _run_managed_wsl_script(
        distro, _clap_setup_script(venv_subdir, _CHECKPOINT_URL, _CHECKPOINT_SHA256),
        on_progress,
        timeout_s=60 * 120, start_msg=f"Setting up the CLAP genre engine in {distro}…",
    )


def setup_resolver_in_wsl(distro: str, on_progress: ProgressCallback | None = None,
                          engine: str = "essentia_tf", model: str = "qwen2.5:7b") -> dict:
    """Install the online genre resolver (ddgs + Ollama + model) inside `distro`."""
    venv_subdir = engine_venv_subdir(engine)
    return _run_managed_wsl_script(
        distro, _resolver_setup_script(venv_subdir, model), on_progress,
        timeout_s=60 * 120, start_msg=f"Setting up the online genre resolver in {distro}…",
    )


# ---------------------------------------------------------------------------
# CUDA library installer (optional GPU enablement inside WSL)
# ---------------------------------------------------------------------------


# Maps essentia's bundled TF 2.5 dlopen targets → the PyPI nvidia wheel that
# ships the .so file inside it. We install via pip into the WSL venv rather
# than via apt because:
#
#   1. apt's per-library packages (libcublas-11-8, libcufft-11-8, ...) aren't
#      reachable on every Ubuntu version. Ubuntu 24.04 in particular has no
#      CUDA 11.x packages in NVIDIA's apt repo at all (they only ship 12.x
#      for noble). The user hits `E: Unable to locate package` and the
#      install fails before installing anything.
#   2. pip wheels are platform-agnostic — they include the .so file directly,
#      installed to `~/.vibechek/venv/lib/python*/site-packages/nvidia/<lib>/lib/`.
#      Works on any Linux distribution, any Ubuntu version, no apt repo
#      configuration, no NVIDIA keyring, no root required.
#
# We map both the .so name and an optional minimum version. The CUDA 11.x
# wheels pin to ~11.10 / cuDNN 8.6 which works fine with TF 2.5 thanks to
# CUDA's binary compatibility guarantees.
_CUDA_PIP_PACKAGES_BY_LIB = {
    "libcudart.so.11.0": "nvidia-cuda-runtime-cu11",
    "libcublas.so.11":   "nvidia-cublas-cu11",
    "libcublasLt.so.11": "nvidia-cublas-cu11",   # ships with libcublas
    "libcufft.so.10":    "nvidia-cufft-cu11",
    "libcurand.so.10":   "nvidia-curand-cu11",
    "libcusolver.so.11": "nvidia-cusolver-cu11",
    "libcusparse.so.11": "nvidia-cusparse-cu11",
    "libcudnn.so.8":     "nvidia-cudnn-cu11",
}

# Future-proofing for TF 2.13+ which bundles CUDA 12 and dlopens libcudart.so.12,
# libcudnn.so.9, etc. The probe's `missing_cuda_libs` would list those .so names,
# and we route to the cu12 wheels. Detected via the .so suffix — no separate
# TF-version branching needed.
_CUDA12_PIP_PACKAGES_BY_LIB = {
    "libcudart.so.12":   "nvidia-cuda-runtime-cu12",
    "libcublas.so.12":   "nvidia-cublas-cu12",
    "libcublasLt.so.12": "nvidia-cublas-cu12",
    "libcufft.so.11":    "nvidia-cufft-cu12",
    "libcurand.so.10":   "nvidia-curand-cu12",
    "libcusolver.so.11": "nvidia-cusolver-cu12",
    "libcusparse.so.12": "nvidia-cusparse-cu12",
    "libcudnn.so.9":     "nvidia-cudnn-cu12",
    "libcudnn.so.8":     "nvidia-cudnn-cu12",  # cu12 wheel covers older soname
}


def _resolve_cuda_packages(missing_libs: list[str]) -> tuple[list[str], list[str]]:
    """Translate missing .so names to pip wheel names, handling cu11 + cu12.

    Returns (wheel_packages_sorted, unknown_libs). Routing rule:
      - Any `.so.12`-suffixed lib → cu12 wheel set
      - Any `.so.11.0` / `.so.11` / `.so.10` / `.so.8` → cu11 wheel set
      - Mixed: pick cu12 (forward-compatible — cu12 runtime supports cu11
        apps via CUDA's binary compat guarantee)
    """
    # A CUDA-12 install dlopens .so.12 (cudart/cublas/...) or .so.9 (cudnn).
    # Detect by soname suffix only — the membership test the old code ANDed in
    # was dead (every .so.12 name is in the cu12 table anyway) and the missing
    # parentheses meant `and` bound tighter than `or`, making the logic
    # accidental rather than intended.
    has_cu12 = any(
        (".so.12" in lib) or (".so.9" in lib)
        for lib in missing_libs
    )
    table = _CUDA12_PIP_PACKAGES_BY_LIB if has_cu12 else _CUDA_PIP_PACKAGES_BY_LIB
    packages: set[str] = set()
    unknown: list[str] = []
    for lib in missing_libs:
        wheel = table.get(lib)
        if wheel:
            packages.add(wheel)
        else:
            # Fall back to the OTHER table — handles mixed-soname situations
            other = (_CUDA_PIP_PACKAGES_BY_LIB
                     if table is _CUDA12_PIP_PACKAGES_BY_LIB
                     else _CUDA12_PIP_PACKAGES_BY_LIB)
            wheel = other.get(lib)
            if wheel:
                packages.add(wheel)
            else:
                unknown.append(lib)
    return sorted(packages), unknown

# Legacy apt mapping retained for the regression tests and any callers that
# might still reference it. New code paths use the pip mapping above.
_CUDA_APT_PACKAGES_BY_LIB = {
    "libcublas.so.11":   ["cuda-libraries-11-8", "libcublas-11-8"],
    "libcublasLt.so.11": ["cuda-libraries-11-8", "libcublas-11-8"],
    "libcufft.so.10":    ["cuda-libraries-11-8", "libcufft-11-8"],
    "libcurand.so.10":   ["cuda-libraries-11-8", "libcurand-11-8"],
    "libcusolver.so.11": ["cuda-libraries-11-8", "libcusolver-11-8"],
    "libcusparse.so.11": ["cuda-libraries-11-8", "libcusparse-11-8"],
    "libcudnn.so.8":     ["libcudnn8", "nvidia-cudnn"],
}

# Pip-based bootstrap: install NVIDIA's CUDA runtime wheels into the WSL venv,
# then generate `~/.vibechek/cuda-env.sh` which sets LD_LIBRARY_PATH so
# essentia's bundled TF can find the .so files at runtime.
#
# Why pip and not apt? Apt's CUDA 11.x packages aren't available on Ubuntu
# 24.04 at all, and on 22.04 the keyring registration silently fails on many
# WSL installs. Pip wheels work on every Linux, no exceptions.
_CUDA_LIBS_PIP_BOOTSTRAP = r"""
set -e
export PIP_DISABLE_PIP_VERSION_CHECK=1
export PIP_ROOT_USER_ACTION=ignore

PIP="$HOME/.vibechek/venv/bin/pip"
PYTHON="$HOME/.vibechek/venv/bin/python"

if [ ! -x "$PIP" ]; then
    echo "ERROR: vibechek venv not found at ~/.vibechek/venv."
    echo "ERROR: Install Essentia first (Settings -> Set up now)."
    exit 2
fi

echo "[1/3] Installing NVIDIA CUDA runtime wheels into managed venv..."
PACKAGES="__PACKAGES__"
echo "      Packages: $PACKAGES"
# --upgrade so we replace stale versions if the user re-runs after an upgrade.
# Each package is ~50-800 MB; cudnn is the heaviest.
if ! "$PIP" install --upgrade --no-warn-script-location $PACKAGES 2>&1; then
    echo "ERROR: pip install of NVIDIA wheels failed."
    echo "ERROR: Check internet connectivity inside WSL: wsl -- curl https://pypi.org"
    exit 3
fi

echo "[2/3] Locating installed library directories..."
LIB_DIRS=$("$PYTHON" -c "
import os
try:
    import nvidia
except ImportError:
    raise SystemExit('nvidia namespace package missing after pip install')
base = os.path.dirname(nvidia.__file__)
dirs = []
for sub in sorted(os.listdir(base)):
    libdir = os.path.join(base, sub, 'lib')
    if os.path.isdir(libdir):
        dirs.append(libdir)
print(':'.join(dirs))
")
if [ -z "$LIB_DIRS" ]; then
    echo "ERROR: wheels installed but no nvidia/*/lib directories found."
    exit 4
fi
echo "      Found $(echo "$LIB_DIRS" | tr ':' '\n' | wc -l) directories"

echo "[3/3] Generating ~/.vibechek/cuda-env.sh..."
ENV_FILE="$HOME/.vibechek/cuda-env.sh"
# Use printf so the redirect doesn't expand subshells. The leading colon trick
# (${LD_LIBRARY_PATH:+...}) appends if LD_LIBRARY_PATH was already set, else
# leaves it as just our paths.
printf '%s\n' \
    '# Auto-generated by Vibechek. Source this to make NVIDIA CUDA runtime' \
    '# libraries visible to essentia-tensorflow.' \
    "export LD_LIBRARY_PATH=\"${LIB_DIRS}\${LD_LIBRARY_PATH:+:\$LD_LIBRARY_PATH}\"" \
    > "$ENV_FILE"
chmod +x "$ENV_FILE"

# SHIM PATCHING REMOVED. The previous version of this script inserted a bash
# `source` line into the venv's vibechek shim — but the shim is a Python
# script (pip-generated entry point), not bash. The injected line turned every
# subsequent invocation into a SyntaxError ("invalid syntax") at line 2,
# silently breaking analyze for every user who ran the GPU install.
#
# The sourcing now happens exclusively from the launcher generated by
# `run_vibechek_in_wsl` (which is a real bash file, where bash syntax is
# valid). That's the only path that needs LD_LIBRARY_PATH set, because
# essentia + TF only load when `vibechek analyze` actually runs.
#
# If you're tempted to add `source` to a Python entry point: DON'T. The
# launcher pattern is intentional.
#
# Repair shim if it was broken by the old version of this code:
SHIM="$HOME/.vibechek/venv/bin/vibechek"
if [ -f "$SHIM" ] && grep -q "cuda-env.sh" "$SHIM"; then
    echo "      Repairing previously-corrupted vibechek shim..."
    # Strip any line containing cuda-env.sh from the shim. Atomic: write to a
    # temp, verify non-empty, then `mv` over the live shim — never truncate it
    # first (a disk-full mid-copy would zero the entry point).
    TMP_SHIM="$(mktemp)"
    if grep -v "cuda-env.sh" "$SHIM" > "$TMP_SHIM" && [ -s "$TMP_SHIM" ]; then
        mv "$TMP_SHIM" "$SHIM"
        echo "      Shim repaired."
    fi
    rm -f "$TMP_SHIM"
fi

echo "INSTALLED: ${LIB_DIRS}"
echo "DONE"
"""


# The legacy apt-based bootstrap. Kept around for reference / regression tests
# but new installs go through the pip path. Marked underscore-prefixed and not
# called by install_cuda_libs_in_wsl anymore.
_CUDA_LIBS_BOOTSTRAP = r"""
set +e  # don't bail on individual package failures
export DEBIAN_FRONTEND=noninteractive

# Find Ubuntu version for the right NVIDIA repo URL.
. /etc/os-release
UBUNTU_VER="${VERSION_ID/./}"  # 22.04 -> 2204
case "$UBUNTU_VER" in
    2004|2204|2404) ;;
    *)
        echo "Unsupported Ubuntu version $VERSION_ID; CUDA libs install skipped."
        exit 2
        ;;
esac

echo "[1/4] Adding NVIDIA CUDA repository for Ubuntu $VERSION_ID..."
KEYRING_URL="https://developer.download.nvidia.com/compute/cuda/repos/ubuntu${UBUNTU_VER}/x86_64/cuda-keyring_1.1-1_all.deb"
TMPDEB="$(mktemp --suffix=.deb)"
if ! curl -fsSL "$KEYRING_URL" -o "$TMPDEB" 2>&1; then
    echo "ERROR: Failed to download CUDA keyring from $KEYRING_URL"
    echo "ERROR: Your WSL distro may not have internet access right now."
    exit 3
fi
if ! dpkg -i "$TMPDEB" 2>&1; then
    echo "ERROR: Failed to install CUDA keyring deb."
    exit 4
fi
rm -f "$TMPDEB"

echo "[2/4] Enabling Ubuntu multiverse (for nvidia-cudnn fallback)..."
# add-apt-repository may not be installed — install it lazily if needed.
if ! command -v add-apt-repository >/dev/null 2>&1; then
    apt-get install -y -q --no-install-recommends software-properties-common 2>&1 | tail -3
fi
add-apt-repository -y multiverse 2>&1 | tail -3 || true

echo "[3/4] Updating apt..."
# `apt-get update` returns 0 even when some repos fail to fetch, so we can't
# rely on its exit code. Instead capture stderr and look for the actual
# success signal: at least one new repo line under the NVIDIA cuda domain.
APT_UPDATE_OUT="$(apt-get update 2>&1)"
APT_UPDATE_RC=$?
echo "$APT_UPDATE_OUT" | tail -10
if [ "$APT_UPDATE_RC" -ne 0 ]; then
    echo "ERROR: apt-get update returned $APT_UPDATE_RC"
    exit 5
fi
# Sanity-check: did the NVIDIA repo actually load? If not, every install
# below will fail with `E: Unable to locate package` and we should tell the
# user up front rather than after a half-dozen "not found" messages.
if ! apt-cache policy 2>/dev/null | grep -q "developer.download.nvidia.com"; then
    echo "WARN: NVIDIA CUDA repo isn't in apt-cache after keyring install."
    echo "WARN: cuda-libraries-11-8 will likely fail to find. Possible causes:"
    echo "      - Ubuntu version $VERSION_ID has no matching NVIDIA cuda repo"
    echo "      - The keyring deb installed but didn't write /etc/apt/sources.list.d/cuda-*"
    # Continue anyway — the multiverse fallback for libcudnn8 may still work.
fi

# Each "TRY:" line below is a fallback chain: try the first package, fall
# through to the next on failure. Generated by _build_try_chain() in Python.
INSTALLED_PKGS=""
FAILED_LIBS=""

echo "[4/4] Installing CUDA runtime libraries..."
__TRY_CHAIN__

if [ -n "$FAILED_LIBS" ]; then
    echo "PARTIAL: installed=[${INSTALLED_PKGS# }] failed=[${FAILED_LIBS# }]"
    # Soft-fail with exit 0 — we want the Python side to read the markers and
    # decide. A hard failure here would lose all progress info.
    exit 0
fi

echo "INSTALLED: ${INSTALLED_PKGS# }"
echo "DONE"
"""


def _build_try_chain(missing_libs: list[str]) -> tuple[str, list[str]]:
    """Build the shell try-chain block for a list of missing libs.

    Returns (bash_block, all_packages_attempted).
    """
    lines: list[str] = []
    all_pkgs: set[str] = set()
    for lib in missing_libs:
        fallbacks = _CUDA_APT_PACKAGES_BY_LIB.get(lib, [])
        if not fallbacks:
            lines.append(f'echo "WARN: no apt mapping for {lib}; skipping"')
            lines.append(f'FAILED_LIBS="$FAILED_LIBS {lib}"')
            continue
        all_pkgs.update(fallbacks)
        lines.append(f'# --- {lib} ---')
        lines.append('INSTALLED_THIS=""')
        for pkg in fallbacks:
            # `apt-get install` returns 100 on missing package; we let that
            # fall through to the next fallback.
            lines.append(
                f'if [ -z "$INSTALLED_THIS" ]; then\n'
                f'  echo "[install] {lib} -> {pkg}"\n'
                f'  if apt-get install -y -q --no-install-recommends {pkg} 2>&1 | tail -5 ; then\n'
                f'    if dpkg -l {pkg} >/dev/null 2>&1; then\n'
                f'      INSTALLED_THIS="{pkg}"\n'
                f'      INSTALLED_PKGS="$INSTALLED_PKGS {pkg}"\n'
                f'    fi\n'
                f'  fi\n'
                f'fi'
            )
        lines.append(
            f'if [ -z "$INSTALLED_THIS" ]; then\n'
            f'  FAILED_LIBS="$FAILED_LIBS {lib}"\n'
            f'fi'
        )
    return "\n".join(lines), sorted(all_pkgs)


def repair_wsl_shim(distro: str) -> dict:
    """Strip any `cuda-env.sh` source line from the venv's vibechek shim.

    The pre-beta.10 CUDA installer mistakenly injected a bash `. cuda-env.sh`
    line into the venv's vibechek shim, which is a *Python* script. The
    injection turned every subsequent run into a SyntaxError, silently
    breaking analyze. This function detects and removes the bad line.

    Safe to call repeatedly — if no broken line is present, it's a no-op.
    """
    if not IS_WINDOWS:
        return {"ok": False, "error": "Not running on Windows"}

    wsl = shutil.which("wsl") or shutil.which("wsl.exe")
    if not wsl:
        return {"ok": False, "error": "wsl.exe not found"}

    # Use a staged tempfile so the bash one-liner doesn't trip wsl.exe's
    # variable-substitution quirk.
    script = r"""#!/usr/bin/env bash
SHIM="$HOME/.vibechek/venv/bin/vibechek"
if [ ! -f "$SHIM" ]; then
    echo "NO_SHIM"
    exit 0
fi
if grep -q "cuda-env.sh" "$SHIM"; then
    # Atomic rewrite: write the cleaned shim to a temp, verify it's non-empty,
    # then `mv` (rename) over the live shim. Truncating the shim first (the old
    # `cat "$TMP" > "$SHIM"`) risked leaving `vibechek` empty on a disk-full.
    TMP="$(mktemp)"
    if grep -v "cuda-env.sh" "$SHIM" > "$TMP" && [ -s "$TMP" ]; then
        mv "$TMP" "$SHIM"
        rm -f "$TMP"
        echo "REPAIRED"
    else
        rm -f "$TMP"
        echo "REPAIR_FAILED"
    fi
else
    echo "ALREADY_OK"
fi
"""
    from vibechek import cancellation  # noqa: PLC0415

    script_path = _stage_script_for_wsl(script)
    wsl_script_path = win_to_wsl_path(str(script_path))
    try:
        try:
            proc = subprocess.Popen(
                [wsl, "-d", distro, "--", "bash", wsl_script_path],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except OSError as e:
            return {"ok": False, "error": f"Could not invoke wsl: {e}"}

        cancel_done, cancel_state = _start_cancellation_watchdog(proc)
        try:
            stdout_bytes, _stderr = proc.communicate(timeout=30)
            rc = proc.returncode
        except subprocess.TimeoutExpired:
            proc.kill()
            cancel_done.set()
            return {"ok": False, "error": "Shim repair timed out after 30s"}
        cancel_done.set()

        if cancel_state["v"] or cancellation.is_cancelled():
            return {"ok": False, "error": "Cancelled by user", "cancelled": True}

        result = subprocess.CompletedProcess(
            [wsl, "-d", distro, "--", "bash", wsl_script_path],
            rc, stdout_bytes, b"",
        )
    finally:
        script_path.unlink(missing_ok=True)

    # Decode like the other probes — WSL on some setups emits UTF-16 LE, and a
    # plain utf-8 decode there interleaves NULs so none of the marker checks
    # below match (the repair succeeds but we'd report "Unexpected output").
    stdout = ""
    for enc in ("utf-8", "utf-16-le", "cp1252"):
        try:
            stdout = result.stdout.decode(enc).replace("\x00", "").strip()
            break
        except UnicodeDecodeError:
            continue
    if "REPAIR_FAILED" in stdout:
        return {"ok": False, "error": "Shim repair aborted: the cleaned shim "
                "would have been empty (possible disk-full). The original shim "
                "was left intact. Free up disk space in WSL and try again."}
    if "REPAIRED" in stdout:
        return {"ok": True, "repaired": True,
                "message": "Shim repaired. Analyze should work now."}
    if "ALREADY_OK" in stdout:
        return {"ok": True, "repaired": False,
                "message": "Shim is already clean — no action needed."}
    if "NO_SHIM" in stdout:
        return {"ok": False, "error": "vibechek shim not found in WSL venv. "
                "Run the Essentia install first."}
    return {"ok": False, "error": f"Unexpected output: {stdout[:200]}"}


def install_cuda_libs_in_wsl(
    distro: str,
    missing_libs: list[str],
    on_progress: ProgressCallback | None = None,
) -> dict:
    """Install the CUDA runtime libs essentia's bundled TF needs to use the GPU.

    Uses NVIDIA's PyPI wheels (`nvidia-cublas-cu11`, `nvidia-cudnn-cu11`, ...)
    installed into the user's WSL venv. Pip wheels are platform-agnostic —
    they ship the .so file inside the wheel — so this works on every Ubuntu
    version regardless of whether NVIDIA's apt repo is reachable.

    After install we generate `~/.vibechek/cuda-env.sh` which exports the
    correct `LD_LIBRARY_PATH`, and patch the venv's vibechek shim to source
    it on launch. Net effect: the GPU "just works" the next time analyze
    runs, with no further user action.

    `missing_libs` is the `missing_cuda_libs` list from `probe_engine_gpu`.
    """
    if not IS_WINDOWS:
        return {"ok": False, "error": "Not running on Windows"}

    wsl = shutil.which("wsl") or shutil.which("wsl.exe")
    if not wsl:
        return {"ok": False, "error": "wsl.exe not found"}

    # Translate missing .so names to PyPI wheel names. Routes to cu12 wheels
    # automatically when TF dlopens .so.12 / .so.9 — future-proofs against
    # the inevitable essentia-tensorflow upgrade to TF 2.13+.
    pip_packages, unknown = _resolve_cuda_packages(missing_libs)

    if not pip_packages:
        return {
            "ok": False,
            "error": (
                f"No PyPI wheels mapped for: {', '.join(missing_libs)}. "
                f"You may need to install them manually."
            ),
            "unknown_libs": unknown,
        }

    # count=1 so a future comment mentioning `__PACKAGES__` can't get its
    # tail spliced into the script (we hit that exact bug with __TRY_CHAIN__).
    script = _CUDA_LIBS_PIP_BOOTSTRAP.replace("__PACKAGES__", " ".join(pip_packages), 1)

    if on_progress:
        on_progress(0, 100,
                    f"Installing {len(pip_packages)} CUDA wheel(s) into {distro} venv...")

    # Step markers from the pip bootstrap: [1/3] pip install (slow),
    # [2/3] locate dirs, [3/3] write env file.
    step_pct = {"[1/3]": 10, "[2/3]": 75, "[3/3]": 85, "DONE": 95}
    tail: list[str] = []
    installed_lib_dirs: list[str] = []

    from vibechek import cancellation  # noqa: PLC0415

    # See _stage_script_for_wsl for why we don't pipe via stdin.
    # We run as the *default user* (not root) because pip installs into the
    # user's venv at ~/.vibechek/venv/.
    script_path = _stage_script_for_wsl(script)
    wsl_script_path = win_to_wsl_path(str(script_path))

    # Watchdog setup: token file holds the bash pgid so a Cancel reaches
    # every pip download inside WSL (a 1+ GB cudnn wheel pull would otherwise
    # finish even after the user clicked Cancel).
    import tempfile as _tempfile
    token_file = Path(_tempfile.gettempdir()) / f"vibechek-wsl-cuda-pid-{os.getpid()}.txt"
    wsl_token = win_to_wsl_path(str(token_file))
    launcher_script = (
        "#!/usr/bin/env bash\n"
        "set -e\n"
        f'echo $$ > {_shell_quote(wsl_token)}\n'
        'trap "kill -TERM 0 2>/dev/null; exit 130" SIGTERM SIGINT\n'
        f"exec bash {_shell_quote(wsl_script_path)}\n"
    )
    launcher_path = _stage_script_for_wsl(launcher_script)
    wsl_launcher = win_to_wsl_path(str(launcher_path))

    try:
        try:
            # `setsid -w`: WAIT for the child (see install_vibechek_in_wsl /
            # run_vibechek_in_wsl). Without -w the CUDA wheel install forks to
            # the background, the parent sees instant exit 0 + EOF, and reports
            # success with an empty lib_dirs list while the multi-hundred-MB
            # pip pulls run orphaned inside WSL.
            proc = subprocess.Popen(
                [wsl, "-d", distro, "--", "setsid", "-w", "bash", wsl_launcher],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
        except OSError as e:
            return {"ok": False, "error": f"Could not invoke wsl: {e}"}
        assert proc.stdout

        cancel_done, cancel_state = _start_cancellation_watchdog(
            proc,
            on_cancel=lambda: _kill_wsl_pgid(wsl, distro, token_file),
        )

        for raw_line in proc.stdout:
            line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
            tail.append(line)
            if len(tail) > 600:
                tail.pop(0)
            for marker, pct in step_pct.items():
                if line.startswith(marker):
                    if on_progress:
                        on_progress(pct, 100, line[:120])
                    break
            # Bottom marker emitted right before DONE: list of lib dirs we
            # registered into cuda-env.sh.
            if line.startswith("INSTALLED: "):
                installed_lib_dirs = line[len("INSTALLED: "):].split(":")
            elif on_progress and line.startswith("  "):
                # Surface pip's per-package progress lines to the GUI
                on_progress(40, 100, line[:120])

        try:
            rc = proc.wait(timeout=60 * 30)
        except subprocess.TimeoutExpired:
            proc.kill()
            cancel_done.set()
            return {"ok": False, "error": "CUDA wheel install timed out after 30 min"}
        cancel_done.set()

        if cancel_state["v"] or cancellation.is_cancelled():
            return {"ok": False, "error": "Cancelled by user", "cancelled": True,
                    "tail": "\n".join(tail[-100:])}
    finally:
        script_path.unlink(missing_ok=True)
        launcher_path.unlink(missing_ok=True)
        token_file.unlink(missing_ok=True)

    if rc != 0:
        # Route through the shared cause-detector instead of hardcoding a
        # network-only hint. The old message always blamed "pip couldn't reach
        # PyPI" regardless of the real tail — so a disk-full WSL or a pip
        # resolver miss sent the user to run `curl pypi.org` (which succeeds and
        # proves nothing) while the actual fix went unmentioned.
        # `_explain_install_failure` already discriminates disk-full / apt-lock /
        # DNS / pip-version failures from the tail and is used for the sibling
        # essentia install paths; the "cuda-libs" phase adds the network+venv
        # fallback hint when nothing more specific matched.
        return {
            "ok": False,
            "error": _explain_install_failure(rc, tail, phase="cuda-libs"),
            "tail": "\n".join(tail[-100:]),
            "unknown_libs": unknown,
            "packages_attempted": pip_packages,
        }

    # Invalidate engine GPU cache so the next probe sees the new libs
    with _ENGINE_GPU_CACHE_LOCK:
        _ENGINE_GPU_CACHE.clear()

    if on_progress:
        on_progress(100, 100, "CUDA wheels installed; re-probing GPU...")

    return {
        "ok": True,
        "distro": distro,
        "packages_installed": pip_packages,
        "lib_dirs": installed_lib_dirs,
        "unknown_libs": unknown,
        "tail": "\n".join(tail[-100:]),
    }


def _explain_install_failure(rc: int, tail: list[str], phase: str) -> str:
    """Build a human-friendly error message from the bootstrap tail."""
    last_lines = "\n  ".join(tail[-10:]) if tail else "(no output)"
    hints: list[str] = []

    # Detect common failure modes from the output
    tail_text = "\n".join(tail)
    if "Could not get lock" in tail_text or "dpkg" in tail_text.lower() and "in use" in tail_text.lower():
        hints.append("Another apt/dpkg process is running. Wait for it to finish and try again.")
    elif "No space left on device" in tail_text:
        hints.append("Out of disk space inside WSL. Free some space and retry.")
    elif "Temporary failure resolving" in tail_text or "Network is unreachable" in tail_text:
        hints.append("WSL can't reach the internet. Check your network / VPN / firewall.")
    elif phase == "pip" and ("Could not find a version" in tail_text or "No matching distribution" in tail_text):
        hints.append("pip can't find essentia-tensorflow. Ensure your WSL Ubuntu is 22.04+ with python 3.10+.")
    elif phase == "cuda-libs" and ("Could not find a version" in tail_text or "No matching distribution" in tail_text):
        hints.append("pip couldn't resolve a CUDA runtime wheel for this platform/Python. "
                     "Ensure your WSL Ubuntu is 22.04+ with python 3.10+.")
    elif "ERROR: Could not install packages" in tail_text:
        hints.append("pip install failed — see the tail for the package and reason.")

    # Fallback for the CUDA-libs phase: when the tail matched none of the
    # specific cases above, keep the original network + missing-venv guidance
    # (the two most common real causes) rather than leaving no hint at all.
    if not hints and phase == "cuda-libs":
        hints.append(
            "If pip couldn't reach PyPI, check WSL's network (VPN / firewall / DNS). "
            "If the analysis venv is missing, install Essentia first via Settings -> Set up now."
        )

    hint_str = ("\n\nHint: " + " ".join(hints)) if hints else ""
    return (
        f"Phase '{phase}' exited with {rc}.\n\nLast output:\n  {last_lines}{hint_str}"
    )


# ---------------------------------------------------------------------------
# Zero-setup self-heal: verify + repair the engine runtime on drift
# ---------------------------------------------------------------------------
#
# Product doctrine (zero-setup-doctrine): DETECT -> SELF-HEAL -> RUN. The user
# should never need a manual "repair" button. Two real incidents motivate this:
# (1) a WSL reinstall wiped the CUDA-11 libs essentia's TF needs and nothing
# regenerated cuda-env.sh, so the GPU was silently dead for a month; (2) the
# code-only drift auto-update can't reconcile an onnxruntime/CUDA skew, so the
# venv stayed import-broken while the update reported success. ensure_engine_runtime
# closes both gaps transparently on the next analyze.


def _autoheal_disabled() -> bool:
    """True iff the user opted out of automatic environment repair.

    ``VIBECHEK_NO_AUTOHEAL=1`` (or true/yes/on) suppresses the *repair* actions
    (multi-GB reinstalls / CUDA-lib downloads) — DETECTION and honest failure
    reporting still happen, so a power user who manages their own venv isn't
    surprised by a background reinstall but also isn't lied to. Default is ON
    (unset -> healing enabled) per the doctrine: no manual step may be the ONLY
    path to a working engine.
    """
    return os.environ.get("VIBECHEK_NO_AUTOHEAL", "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def _engine_stack_imports(engine: str) -> str:
    """The Python import that proves `engine`'s ML stack is loadable.

    onnx/native run onnxruntime (the exact thing that hard-crashes on a CUDA
    12/13 wheel skew — ``libcudart.so.13: cannot open shared object file``) plus
    essentia for the DSP; essentia_tf runs essentia-tensorflow. We import only
    the top-level packages — enough to catch a broken shared-library dlopen
    without paying the multi-second full-backend init.
    """
    if engine_venv_subdir(engine) == "venv-onnx":
        return "import essentia, onnxruntime"
    return "import essentia"


def _probe_engine_stack_import(distro: str, engine: str) -> tuple[bool, str]:
    """Run ``<venv python> -c "import <ml stack>"`` inside `distro`; report health.

    Returns ``(ok, detail)``. ``ok=False`` ONLY on a definite negative — the venv
    python is missing, or the import exits non-zero (``detail`` then carries the
    real stderr tail, e.g. the libcudart mismatch). A probe that can't even
    launch (no wsl.exe, Popen/timeout error) returns ``(True, "<reason>")`` so we
    never false-flag a healthy install as broken and trigger a needless reinstall.
    """
    wsl = shutil.which("wsl") or shutil.which("wsl.exe")
    if not wsl:
        return True, "probe skipped: wsl.exe not on PATH"
    subdir = engine_venv_subdir(engine)
    imports = _engine_stack_imports(engine)
    # `bash -s` over stdin (not `-c`) to dodge wsl.exe's multi-line -c variable
    # mangling — same pattern as _probe_distro and the install scripts.
    script = (
        'HOME_DIR="$(printenv HOME)"\n'
        f'PY="$HOME_DIR/.vibechek/{subdir}/bin/python"\n'
        'if [ ! -x "$PY" ]; then echo "VIBECHEK_STACK_NO_PY"; exit 90; fi\n'
        f'"$PY" -c "{imports}"\n'
    )
    try:
        proc = subprocess.Popen(
            [wsl, "-d", distro, "--", "bash", "-s"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        clean = script.replace("\r\n", "\n").replace("\r", "\n")
        stdout_bytes, stderr_bytes = proc.communicate(
            input=clean.encode("utf-8"), timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return True, f"probe inconclusive: {type(e).__name__}: {e}"
    if proc.returncode == 0:
        return True, ""
    out = (stdout_bytes or b"").decode("utf-8", errors="replace")
    err = (stderr_bytes or b"").decode("utf-8", errors="replace")
    if "VIBECHEK_STACK_NO_PY" in out:
        return False, f"the {subdir} venv's Python interpreter is missing"
    # Surface the last few real stderr lines (the actual ImportError / dlopen
    # failure) — never a generic "not installed".
    tail = [ln for ln in err.splitlines() if ln.strip()][-4:]
    detail = " / ".join(tail) if tail else f"import exited {proc.returncode}"
    return False, detail


def ensure_engine_runtime(
    distro: str,
    engine: str = "essentia_tf",
    on_progress: ProgressCallback | None = None,
) -> dict:
    """DETECT -> SELF-HEAL -> RUN the WSL engine runtime for `engine`.

    Called transparently from the analyzer's drift-update path so a WSL reinstall
    / app upgrade self-heals on the next analyze instead of dead-ending:

      (a) verify the venv imports its ML stack (essentia / onnxruntime);
      (b) on failure, reinstall the wheel set in place (reuses
          ``install_vibechek_in_wsl`` — same progress + cancellation), re-verify;
      (c) for essentia_tf, if a GPU is physically visible but the engine can't use
          it because the CUDA libs / cuda-env.sh are absent (the classic
          post-WSL-reinstall wipe), restore them via ``install_cuda_libs_in_wsl``,
          announcing "Restoring GPU libraries…".

    Returns ``{ok, healed:[...], ...}``. ``ok=False`` only for a FATAL problem
    (the ML stack can't import and couldn't be repaired) — a GPU-lib restore
    failure is non-fatal (analyze still runs on CPU) and returned as ``ok:True``
    with a ``gpu_heal_failed`` note. ``VIBECHEK_NO_AUTOHEAL`` suppresses the
    repairs (but not detection): a broken stack then returns ``ok:False`` with the
    real reason so the user gets an honest error instead of a raw crash.
    """
    if not IS_WINDOWS:
        return {"ok": True, "skipped": "not-windows"}
    wsl = shutil.which("wsl") or shutil.which("wsl.exe")
    if not wsl:
        return {"ok": False, "error": "wsl.exe not found"}

    from vibechek import cancellation  # noqa: PLC0415

    subdir = engine_venv_subdir(engine)
    autoheal = not _autoheal_disabled()
    healed: list[str] = []

    # (a) DETECT — can the venv import its ML stack?
    stack_ok, detail = _probe_engine_stack_import(distro, engine)
    if not stack_ok:
        if not autoheal:
            return {
                "ok": False,
                "error": (
                    f"The {engine} engine can't import its ML stack in {distro}: "
                    f"{detail}. Automatic repair is disabled (VIBECHEK_NO_AUTOHEAL); "
                    f"re-run Settings -> Set up WSL to fix it."
                ),
                "stack_error": detail,
                "autoheal_disabled": True,
            }
        # (b) SELF-HEAL — reinstall the wheel set in place.
        if on_progress:
            on_progress(
                0, 0,
                f"Repairing the {engine} analysis engine in {distro} "
                f"(reinstalling its ML libraries; one-time)…",
            )
        log.warning(
            "Engine ML stack import failed in %s (%s) — auto-repairing in place",
            distro, detail,
        )
        res = install_vibechek_in_wsl(distro, on_progress=on_progress, engine=engine)
        if res.get("cancelled") or cancellation.is_cancelled():
            return {"ok": False, "cancelled": True, "error": "Cancelled by user"}
        if not res.get("ok"):
            return {
                "ok": False,
                "phase": "stack-repair",
                "error": (
                    f"Automatic repair of the {engine} engine failed: "
                    f"{res.get('error', 'unknown error')}"
                ),
                "tail": res.get("tail"),
            }
        stack_ok2, detail2 = _probe_engine_stack_import(distro, engine)
        if not stack_ok2:
            return {
                "ok": False,
                "phase": "stack-repair",
                "error": (
                    f"The {engine} engine still can't import its ML stack after an "
                    f"in-place reinstall: {detail2}. Re-run Settings -> Set up WSL."
                ),
                "stack_error": detail2,
            }
        healed.append("ml-stack")

    # (c) CUDA libs / cuda-env.sh — essentia_tf only. The TF build needs the
    # CUDA runtime libs on LD_LIBRARY_PATH via ~/.vibechek/cuda-env.sh; a WSL
    # reinstall wipes them and nothing else restores them (the "GPU silently
    # dead for a month" incident). onnx/native get their cu12 wheels IN the venv
    # from step (b)'s reinstall and don't use cuda-env.sh at all, so they're
    # covered there (and install_cuda_libs_in_wsl targets the essentia_tf venv).
    if subdir == "venv":
        try:
            ginfo = probe_engine_gpu(distro, force=True, engine="essentia_tf")
        except Exception as e:  # noqa: BLE001
            log.warning("GPU probe failed during self-heal in %s: %s", distro, e)
            ginfo = None
        if ginfo is not None and ginfo.gpu_hardware_visible and ginfo.missing_cuda_libs:
            if not autoheal:
                log.warning(
                    "GPU visible in %s but CUDA libs are missing (%s); auto-repair "
                    "disabled — analyze will run on CPU",
                    distro, ginfo.missing_cuda_libs,
                )
                return {"ok": True, "healed": healed,
                        "gpu_heal_skipped": "autoheal-disabled"}
            if on_progress:
                on_progress(0, 0, f"Restoring GPU libraries in {distro}…")
            log.info(
                "Self-heal: restoring %d CUDA lib(s) in %s (%s)",
                len(ginfo.missing_cuda_libs), distro, ginfo.missing_cuda_libs,
            )
            res = install_cuda_libs_in_wsl(
                distro, ginfo.missing_cuda_libs, on_progress=on_progress,
            )
            if res.get("cancelled") or cancellation.is_cancelled():
                return {"ok": False, "cancelled": True, "error": "Cancelled by user"}
            if not res.get("ok"):
                # GPU is an accelerator, not a requirement — don't fail the
                # analyze, just note the degradation (it runs on CPU).
                log.warning("GPU-lib restore failed in %s: %s", distro, res.get("error"))
                return {"ok": True, "healed": healed,
                        "gpu_heal_failed": res.get("error")}
            healed.append("cuda-libs")

    return {"ok": True, "distro": distro, "engine": engine, "healed": healed}


# ---------------------------------------------------------------------------
# Running vibechek inside WSL
# ---------------------------------------------------------------------------


def run_vibechek_in_wsl(
    distro: str,
    args: list[str],
    on_stderr_line: Callable[[str], None] | None = None,
    timeout: int | None = None,
    venv_subdir: str = "venv",
) -> subprocess.CompletedProcess:
    """Run `vibechek <args>` inside `distro` and return the completed process.

    `venv_subdir` selects which managed venv runs the binary: "venv" (default,
    essentia-tensorflow) or "venv-onnx" (plain essentia + onnxruntime — the
    TF-free ONNX engine). The analyze router passes the one matching
    `config.inference_engine`. Defaulting to "venv" keeps the TF path
    byte-identical.

    stderr lines are streamed live to `on_stderr_line` if provided — useful
    for parsing CLI progress output. stdout is captured and returned.

    Cooperatively cancellable: if `vibechek.cancellation.is_cancelled()`
    returns True at any point, the child process is terminated. Without this,
    a Cancel click during a multi-hour analyze inside WSL would do nothing.
    """
    import threading as _threading

    from vibechek import cancellation

    wsl = shutil.which("wsl") or shutil.which("wsl.exe")
    if not wsl:
        raise FileNotFoundError("wsl.exe not on PATH")

    # We stage the launcher as a Windows tempfile rather than using
    # `bash -lc <multi-line cmd>` because wsl.exe on Windows mangles
    # variable substitution in multi-line `-c` strings (see
    # _stage_script_for_wsl). The launcher:
    #
    #   - Calls `setsid` to start a new process group so SIGTERM to our PID
    #     kills the whole tree (bash + python workers).
    #   - Writes its PID to a token file the watchdog reads on cancel.
    #   - Optionally sources cuda-env.sh (only if GPU mode is on — skipping
    #     it for --gpu off keeps CUDA libs out of LD_LIBRARY_PATH so TF
    #     doesn't pre-init).
    #   - execs vibechek with the user's args.
    import tempfile as _tempfile
    token_file = Path(_tempfile.gettempdir()) / f"vibechek-wsl-pid-{os.getpid()}.txt"
    wsl_token = win_to_wsl_path(str(token_file))
    use_gpu = _gpu_mode_from_args(args)
    # cuda-env.sh holds TF's CUDA libs on LD_LIBRARY_PATH (written by
    # install_cuda_libs_in_wsl for the essentia-tensorflow venv). Only source it
    # for the TF engine: the onnx venv uses onnxruntime's own EP discovery, so
    # injecting TF CUDA libs there is at best a no-op, at worst pollution.
    source_cuda_env = (
        '. "$HOME/.vibechek/cuda-env.sh" 2>/dev/null || true'
        if use_gpu != "off" and venv_subdir == "venv"
        else 'true  # cuda-env.sh skipped (--gpu off or non-TF engine)'
    )
    # *Critical*: resolve vibechek's FULL path. Plain `exec vibechek` fails
    # silently here because we run bash non-interactively (via `setsid bash
    # <script>`, no `-l`), so .bashrc / .profile don't run and `~/.local/bin`
    # is NOT on PATH. Earlier betas used `bash -lc` which is a login shell
    # and handled this — switching to setsid+staged-tempfile (beta.8 fix for
    # cancel + apt stdin pollution) accidentally broke it.
    #
    # We search for vibechek in this order:
    #   1. The managed venv (canonical install path from install_vibechek_in_wsl)
    #   2. ~/.local/bin/vibechek (the symlink the installer creates)
    #   3. `command -v vibechek` (PATH fallback for users who installed
    #      another way)
    # The launcher exits with a clear error if none resolve, so the user sees
    # "vibechek binary not found" instead of "exit 0 + no output written".
    # Resolve which managed venv's vibechek to run. The default "venv"
    # (essentia-tensorflow) keeps the historical fallback chain
    # (venv → ~/.local/bin symlink → PATH). A non-default engine venv
    # (e.g. "venv-onnx") must use ONLY its own binary — never fall back to
    # the TF venv, whose essentia build / onnxruntime presence differs.
    venv_bin = f"$HOME/.vibechek/{venv_subdir}/bin/vibechek"
    if venv_subdir == "venv":
        bin_resolution = (
            'VIBECHEK_BIN=""\n'
            f'if [ -x "{venv_bin}" ]; then\n'
            f'  VIBECHEK_BIN="{venv_bin}"\n'
            'elif [ -x "$HOME/.local/bin/vibechek" ]; then\n'
            '  VIBECHEK_BIN="$HOME/.local/bin/vibechek"\n'
            'elif command -v vibechek >/dev/null 2>&1; then\n'
            '  VIBECHEK_BIN="$(command -v vibechek)"\n'
            'else\n'
            '  echo "ERROR: vibechek binary not found in WSL. Re-run Settings -> Set up now." >&2\n'
            '  exit 127\n'
            'fi\n'
        )
    else:
        bin_resolution = (
            f'if [ -x "{venv_bin}" ]; then\n'
            f'  VIBECHEK_BIN="{venv_bin}"\n'
            'else\n'
            f'  echo "ERROR: ONNX engine not set up ({venv_bin} missing). '
            'Re-run Settings -> Set up the ONNX engine." >&2\n'
            '  exit 127\n'
            'fi\n'
        )
    launcher_script = (
        "#!/usr/bin/env bash\n"
        "set -e\n"
        "# vibechek launcher (auto-generated, see vibechek/wsl.py)\n"
        f'echo $$ > {_shell_quote(wsl_token)}\n'
        'trap "kill -TERM 0 2>/dev/null; exit 130" SIGTERM SIGINT\n'
        f"{source_cuda_env}\n"
        # Activate the structured-progress event channel inside the WSL
        # `vibechek analyze` process so the parent sidecar can show per-stage
        # progress (scanning → preflight → spawning workers → first track)
        # AND per-track records as they complete. See
        # vibechek/analyzer.py:_emit_event for the line schema.
        'export VIBECHEK_STREAM_PROGRESS=1\n'
        '# Resolve vibechek binary explicitly — non-interactive bash has no PATH for it.\n'
        f"{bin_resolution}"
        f"exec \"$VIBECHEK_BIN\" {' '.join(_shell_quote(a) for a in args)}\n"
    )
    launcher_path = _stage_script_for_wsl(launcher_script)
    wsl_launcher = win_to_wsl_path(str(launcher_path))
    # `setsid -w` makes our bash the leader of a brand-new process group
    # (`kill -TERM 0` in the trap reliably hits every descendant) AND waits
    # for the child to complete instead of fork-and-exit. Without `-w`,
    # setsid forks bash into the background and immediately returns exit 0,
    # so wsl.exe sees "success + no output" while vibechek is still running
    # (or dying silently) in WSL. Cost us a day of debugging in beta.11.
    proc_cmd = [wsl, "-d", distro, "--", "setsid", "-w", "bash", wsl_launcher]

    log.info("WSL exec: %s", proc_cmd)
    proc = subprocess.Popen(
        proc_cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )

    # Watchdog: poll the cancellation flag every 500ms; tear down the entire
    # WSL process tree if a cancel comes through. Just terminating `wsl.exe`
    # on Windows does NOT kill bash + python inside WSL — those leak until
    # the VM eventually reaps them. We do the proper takedown here.
    cancel_event = _threading.Event()

    def _kill_wsl_tree() -> None:
        """Best-effort takedown of the bash + python tree inside the distro.

        Uses ONLY the bash PID we wrote to the token file — never `pkill -f
        vibechek`, which would also kill unrelated user processes (vim editing
        a vibechek file, a separate dev checkout running tests, etc).

        Because our launcher runs under `setsid`, the bash is a process group
        leader; `kill -TERM -<pgid>` reaches every descendant.
        """
        if not token_file.exists():
            return
        try:
            bash_pid = token_file.read_text(encoding="utf-8").strip()
        except OSError as e:
            log.debug("Could not read token file: %s", e)
            return
        if not bash_pid.isdigit():
            return

        # Step 1: SIGTERM the process group. setsid made bash the pgid leader.
        try:
            subprocess.run(
                [wsl, "-d", distro, "--", "kill", "-TERM", f"-{bash_pid}"],
                capture_output=True, timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            log.debug("SIGTERM to pgid %s failed: %s", bash_pid, e)

        # Step 2: brief grace, then SIGKILL the same group.
        import time as _time
        _time.sleep(1.0)
        try:
            subprocess.run(
                [wsl, "-d", distro, "--", "kill", "-KILL", f"-{bash_pid}"],
                capture_output=True, timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            log.debug("SIGKILL to pgid %s failed: %s", bash_pid, e)

    def _watch_cancel() -> None:
        while not cancel_event.is_set() and proc.poll() is None:
            if cancellation.is_cancelled():
                log.info(
                    "WSL subprocess cancellation requested — terminating PID %s "
                    "and WSL process tree", proc.pid,
                )
                # Kill INSIDE WSL first (so workers die before we lose the
                # process group reference)
                _kill_wsl_tree()
                # Then terminate the Windows-side wsl.exe wrapper
                try:
                    proc.terminate()
                except OSError:
                    pass
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    try:
                        proc.kill()
                    except OSError:
                        pass
                return
            cancel_event.wait(0.5)

    watchdog = _threading.Thread(target=_watch_cancel, daemon=True)
    watchdog.start()

    stdout_chunks: list[str] = []

    # ALWAYS drain stderr on a background thread, even when the caller didn't
    # supply on_stderr_line. stderr is a PIPE; if we leave it unread while the
    # main thread blocks reading stdout, a verbose child filling the ~64KB
    # stderr pipe buffer deadlocks both sides (child blocks on write, parent
    # blocks on stdout read). The callback is optional; the drain is not.
    if proc.stderr is not None:
        def _reader() -> None:
            for line in proc.stderr:  # type: ignore[union-attr]
                if on_stderr_line:
                    on_stderr_line(line.rstrip())

        t = _threading.Thread(target=_reader, daemon=True)
        t.start()

    if proc.stdout is not None:
        for line in proc.stdout:
            stdout_chunks.append(line)

    rc = proc.wait(timeout=timeout)
    cancel_event.set()  # tell the watchdog we're done
    token_file.unlink(missing_ok=True)  # PID handoff file
    launcher_path.unlink(missing_ok=True)  # staged bash launcher

    if cancellation.is_cancelled():
        raise cancellation.CancelledError("WSL analyze cancelled by user")

    return subprocess.CompletedProcess(
        args=proc_cmd,
        returncode=rc,
        stdout="".join(stdout_chunks),
        stderr="",  # already streamed via on_stderr_line
    )


def _gpu_mode_from_args(args: list[str]) -> str:
    """Find the --gpu value in a CLI arg list. Returns 'auto' if not present.

    Used by run_vibechek_in_wsl to decide whether to source cuda-env.sh:
    `--gpu off` means the user wants CPU-only, so we skip the LD_LIBRARY_PATH
    injection that would otherwise prime TF's CUDA detection.
    """
    for i, a in enumerate(args):
        if a == "--gpu" and i + 1 < len(args):
            return args[i + 1]
        if a.startswith("--gpu="):
            return a.split("=", 1)[1]
    return "auto"


def _shell_quote(s: str) -> str:
    """Minimal POSIX shell quoting."""
    if not s:
        return "''"
    if re.fullmatch(r"[A-Za-z0-9_./=:-]+", s):
        return s
    return "'" + s.replace("'", "'\"'\"'") + "'"


# ---------------------------------------------------------------------------
# Internal subprocess helper
# ---------------------------------------------------------------------------


def _detect_wsl_encoding_order(sample: bytes) -> tuple[str, ...]:
    """Pick the decode-attempt order for wsl.exe output by sniffing `sample`.

    wsl.exe historically emits UTF-16 LE on Windows, but some builds (and
    non-default code pages) emit UTF-8. Blindly trying utf-16-le first would
    mis-decode UTF-8 output into CJK/garbage when the byte stream happens to
    have even length and no invalid surrogate. We detect the actual encoding:

      - A BOM is decisive: ``\\xff\\xfe`` → UTF-16 LE, ``\\xef\\xbb\\xbf`` → UTF-8.
      - Otherwise, genuine UTF-16-LE ASCII text has a NUL as every other byte
        (``A`` → ``\\x41\\x00``). If we see no interleaved NULs, it's not
        UTF-16 — prefer UTF-8.
    """
    if sample.startswith(b"\xff\xfe"):
        return ("utf-16-le", "utf-8", "cp1252")
    if sample.startswith((b"\xef\xbb\xbf", b"\xff\xfe\x00\x00")):
        return ("utf-8", "utf-16-le", "cp1252")
    # No BOM: UTF-16-LE ASCII has a high density of NUL bytes (one per char).
    # UTF-8 ASCII has essentially none. Use that to order the attempts.
    if sample and b"\x00" in sample[: min(len(sample), 64)]:
        return ("utf-16-le", "utf-8", "cp1252")
    return ("utf-8", "utf-16-le", "cp1252")


# Cache the WSL VM's RAM readout — `free -m` inside a distro is a ~1s wsl.exe
# round-trip and the Settings slider re-fetches on every engine/genre change.
_WSL_VM_MEM_CACHE: dict[str, tuple[int | None, float]] = {}
_WSL_VM_MEM_CACHE_LOCK = threading.Lock()
_WSL_VM_MEM_CACHE_TTL_SEC = 60.0


def wsl_vm_memory_mb(distro: str, *, force: bool = False) -> int | None:
    """Total RAM (MB) the WSL VM sees — the pool analyze workers actually draw from.

    On Windows the WSL VM gets a SLICE of host RAM (default ~50%, or whatever
    `.wslconfig memory=` sets), NOT the full host total the Settings panel used
    to show. Sizing the worker budget against the host's 31.7 GB while the VM
    only has 15.8 GB is exactly why the slider and the run disagreed. This reads
    `free -m` inside `distro` so the budget measures the right pool. Cached 60s.

    Returns None when it can't be measured (not Windows, no wsl.exe, bad distro
    name, probe failure) — the caller then falls back to the host total.
    """
    if not IS_WINDOWS or not distro or not _VALID_DISTRO_RE.match(distro):
        return None
    now = time.time()
    if not force:
        with _WSL_VM_MEM_CACHE_LOCK:
            cached = _WSL_VM_MEM_CACHE.get(distro)
        if cached is not None and (now - cached[1]) < _WSL_VM_MEM_CACHE_TTL_SEC:
            return cached[0]

    wsl = shutil.which("wsl") or shutil.which("wsl.exe")
    mem: int | None = None
    if wsl:
        try:
            # `free -m` prints "Mem:  <total> <used> ..."; field 2 is total MB.
            proc = _wsl_run(
                [wsl, "-d", distro, "--", "bash", "-lc",
                 "free -m | awk '/^Mem:/{print $2}'"],
                timeout=15,
            )
            if proc.returncode == 0:
                for line in proc.stdout.splitlines():
                    s = line.strip()
                    if s.isdigit():
                        mem = int(s)
                        break
        except (OSError, subprocess.TimeoutExpired) as e:
            log.debug("wsl_vm_memory_mb probe failed for %s: %s", distro, e)

    with _WSL_VM_MEM_CACHE_LOCK:
        _WSL_VM_MEM_CACHE[distro] = (mem, now)
    return mem


def _wsl_run(cmd: list[str], timeout: int = 10) -> subprocess.CompletedProcess:
    """Run a wsl.exe command, decoding the UTF-16-or-UTF-8 output Windows uses."""
    result = subprocess.run(cmd, capture_output=True, timeout=timeout, check=False)
    # Sniff the encoding instead of assuming UTF-16-LE: a UTF-8 wsl.exe build
    # would otherwise be mis-decoded into garbage on the first attempt.
    order = _detect_wsl_encoding_order(result.stdout or result.stderr)
    for enc in order:
        try:
            stdout = result.stdout.decode(enc).replace("\x00", "")
            stderr = result.stderr.decode(enc).replace("\x00", "")
            return subprocess.CompletedProcess(
                cmd, result.returncode, stdout, stderr,
            )
        except UnicodeDecodeError:
            continue
    # Final fallback: lossy
    return subprocess.CompletedProcess(
        cmd, result.returncode,
        result.stdout.decode("utf-8", errors="replace"),
        result.stderr.decode("utf-8", errors="replace"),
    )


__all__ = [
    "IS_WINDOWS",
    "DistroInfo",
    "WSLStatus",
    "EngineGpuDevice",
    "EngineGpuInfo",
    "detect_wsl",
    "install_wsl",
    "install_vibechek_in_wsl",
    "install_cuda_libs_in_wsl",
    "run_vibechek_in_wsl",
    "probe_engine_gpu",
    "wsl_vm_memory_mb",
    "engine_gpu_info_to_dict",
    "win_to_wsl_path",
    "wsl_to_win_path",
    "UnsupportedWslPathError",
    "to_dict",
]
