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
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

# Distros that aren't real Linux environments — probing them with bash will
# either fail with garbage output or hang for the full subprocess timeout.
# Add new known-bad names here as we encounter them.
_NON_LINUX_DISTROS = {"docker-desktop", "docker-desktop-data", "rancher-desktop"}

log = logging.getLogger(__name__)

IS_WINDOWS = sys.platform == "win32"

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


def detect_wsl(quick: bool = False) -> WSLStatus:
    """Snapshot the user's WSL setup.

    `quick=True` skips the per-distro vibechek/essentia probes (which boot
    Stopped distros and can take 30+ seconds total). Returns in under a
    second on typical machines. Used by `preflight()` so the GUI never
    hangs on first load. Call again with `quick=False` for full detail
    after the UI has rendered.
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
            futures = {ex.submit(_probe_distro, d, wsl): d for d in linux_distros}
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
    lines = [l.rstrip() for l in stdout.splitlines() if l.strip()]
    # Skip header
    for line in lines[1:]:
        is_default = line.lstrip().startswith("*")
        clean = line.lstrip().lstrip("*").strip()
        parts = re.split(r"\s+", clean)
        if len(parts) >= 3:
            distros.append(DistroInfo(
                name=parts[0],
                state=parts[1],
                version=parts[2],
                is_default=is_default,
            ))
        elif parts:
            distros.append(DistroInfo(name=parts[0], is_default=is_default))
    return distros


def _probe_distro(distro: DistroInfo, wsl_exe: str) -> None:
    """Check whether `vibechek` and `essentia` are importable inside this distro.

    A single bash invocation does both probes so we only pay the distro-boot
    cost once. We check known fixed paths directly (not `which`) because
    `bash -lc` on Ubuntu is non-interactive and won't add `~/.local/bin` to
    PATH — Ubuntu's default `.bashrc` returns early for non-interactive shells.
    """
    # Disk-only check — fast and reliable. We deliberately DO NOT `import
    # essentia` because that triggers a ~10s TensorFlow load.
    #
    # *Critical:* we run the script via `bash -s` over stdin instead of
    # `bash -c "<script>"`. wsl.exe on Windows has a preprocessor quirk
    # that breaks variable assignment in multi-line `-c` scripts (the LHS
    # variable ends up empty). The install path uses `bash -s` for the
    # same reason — see install_vibechek_in_wsl.
    script = r"""
HOME_DIR="$(printenv HOME)"
for p in "$HOME_DIR/.vibechek/venv/bin/vibechek" "$HOME_DIR/.local/bin/vibechek"; do
  if [ -x "$p" ]; then
    printf 'vibechek=%s\n' "$p"
    break
  fi
done
for d in "$HOME_DIR/.vibechek/venv/lib/python3."*/site-packages/essentia*.dist-info; do
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

    # Decode like _wsl_run does — wsl can emit UTF-16 LE on some setups
    stdout = ""
    for enc in ("utf-8", "utf-16-le", "cp1252"):
        try:
            stdout = stdout_bytes.decode(enc).replace("\x00", "")
            break
        except UnicodeDecodeError:
            continue
    # Build a fake result object with .stdout so the rest of the function works
    class _R:
        pass
    result = _R()
    result.stdout = stdout
    result.returncode = 0

    if result.returncode != 0:
        return

    for line in result.stdout.splitlines():
        line = line.strip()
        if line.startswith("vibechek=") and len(line) > len("vibechek="):
            distro.vibechek_installed = True
            distro.vibechek_path = line[len("vibechek="):]
        elif line.startswith("essentia=") and len(line) > len("essentia="):
            distro.essentia_installed = True


# ---------------------------------------------------------------------------
# Engine-side GPU probe — what TensorFlow inside WSL actually sees
# ---------------------------------------------------------------------------


@dataclass
class EngineGpuDevice:
    """A single GPU as seen by the analyze engine."""
    name: str
    backend: str  # "cuda" | "rocm" | "metal" | "unknown"
    compute_capability: str | None = None
    memory_mb: int | None = None


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

print(json.dumps(out))
"""


def probe_engine_gpu(distro: str | None, *, force: bool = False) -> EngineGpuInfo:
    """Ask the *actual analyze engine* what GPUs it can use.

    If `distro` is None or we're not on Windows, falls back to a native probe
    using `vibechek.resources` so the API is unified across platforms.

    If `distro` is set (WSL path), runs a Python script inside that distro's
    venv that imports TensorFlow and enumerates GPUs. This is the ground truth
    for "will analyze actually use the GPU?".

    Results are cached for 5 minutes (per distro). Pass `force=True` to bypass.
    """
    cache_key = f"wsl:{distro}" if distro else "native"
    now = time.time()
    if not force:
        with _ENGINE_GPU_CACHE_LOCK:
            cached = _ENGINE_GPU_CACHE.get(cache_key)
        if cached is not None and (now - cached[1]) < _ENGINE_GPU_CACHE_TTL_SEC:
            return cached[0]

    if not distro or not IS_WINDOWS:
        info = _probe_native_engine_gpu()
    else:
        info = _probe_wsl_engine_gpu(distro)

    info.probed_at = now
    with _ENGINE_GPU_CACHE_LOCK:
        _ENGINE_GPU_CACHE[cache_key] = (info, now)
    return info


def _probe_native_engine_gpu() -> EngineGpuInfo:
    """Native probe: use the same engine vibechek.resources uses."""
    # Reuse resources.detect() since it already does the TF-aware enumeration
    # when essentia is installed locally.
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
        )
        for g in res.gpu_devices
    ]
    return EngineGpuInfo(
        engine="native",
        ok=True,
        gpu_available=res.gpu_available,
        gpu_count=len(devices),
        devices=devices,
        nvidia_driver=res.cuda_runtime,
        nvidia_smi_available=res.cuda_runtime is not None,
    )


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
echo "TF_JSON=$("$VENV_PY" - <<'PY' 2>/dev/null
__PROBE__
PY
)"
"""
    script = script.replace("__PROBE__", _ENGINE_GPU_PROBE_PY)

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
                ))
            info.gpu_count = int(tf_out.get("gpu_count") or len(info.devices))
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


def win_to_wsl_path(path: str) -> str:
    """Convert a Windows path to its WSL `/mnt/<drive>/...` form.

    Idempotent: returns the input unchanged if it doesn't look like a Win path.
    """
    if not path:
        return path
    m = _WIN_PATH_RE.match(path)
    if not m:
        return path  # Already WSL-style or unparseable
    drive = m.group(1).lower()
    rest = m.group(2).replace("\\", "/")
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
# Install: WSL itself
# ---------------------------------------------------------------------------


def install_wsl(
    distro: str = "Ubuntu-24.04",
    on_progress: ProgressCallback | None = None,
) -> dict:
    """Install WSL + a default distro via elevated PowerShell.

    Triggers a UAC prompt. Blocks until the install completes (or fails). The
    user may need to reboot afterward.

    Returns a dict suitable for direct RPC return.
    """
    if not IS_WINDOWS:
        return {"ok": False, "error": "Not running on Windows"}

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
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", ps_command],
            capture_output=True,
            text=True,
            timeout=60 * 30,  # WSL install can be slow on first run
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "WSL install timed out after 30 min"}
    except OSError as e:
        return {"ok": False, "error": f"Could not invoke PowerShell: {e}"}

    if result.returncode != 0:
        return {
            "ok": False,
            "error": f"WSL install exited with {result.returncode}",
            "stderr": result.stderr[-2000:] if result.stderr else "",
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
_USER_BOOTSTRAP = r"""
set -e

echo "[3/4] Creating ~/.vibechek venv..."
mkdir -p "$HOME/.vibechek"
if [ ! -d "$HOME/.vibechek/venv" ]; then
    python3 -m venv "$HOME/.vibechek/venv"
fi

echo "[4/4] Installing Python packages (this is the slow part)..."
"$HOME/.vibechek/venv/bin/pip" install --upgrade --quiet pip wheel
"$HOME/.vibechek/venv/bin/pip" install --quiet essentia-tensorflow
"$HOME/.vibechek/venv/bin/pip" install --quiet git+https://github.com/papapew/Vibechek.git

# Symlink the CLI into ~/.local/bin so it's on PATH (login shells get this dir
# automatically on most distros; we also tack it onto .bashrc just in case).
mkdir -p "$HOME/.local/bin"
ln -sf "$HOME/.vibechek/venv/bin/vibechek" "$HOME/.local/bin/vibechek"
case ":$PATH:" in
  *":$HOME/.local/bin:"*) ;;
  *) echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$HOME/.bashrc" ;;
esac

echo "DONE"
"$HOME/.vibechek/venv/bin/vibechek" --version
"$HOME/.vibechek/venv/bin/python" -c "import essentia; print('essentia OK', essentia.__version__)"
"""


def install_vibechek_in_wsl(
    distro: str,
    on_progress: ProgressCallback | None = None,
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

    if on_progress:
        on_progress(0, 100, f"Starting install inside {distro}...")

    # Step progress map across BOTH phases (same [N/4] markers).
    step_pct = {"[1/4]": 10, "[2/4]": 25, "[3/4]": 40, "[4/4]": 60, "DONE": 95}
    full_tail: list[str] = []

    def _run_phase(args: list[str], script: str, label: str) -> tuple[int, list[str]]:
        if on_progress:
            on_progress(step_pct.get("[1/4]" if "ROOT" in label else "[3/4]", 0), 100, label)
        try:
            # Use BINARY stdin and stdout. On Windows, text-mode Popen
            # silently converts \n → \r\n in writes, which makes bash on
            # the Linux side see a \r at the end of every line and choke
            # (`set: -\r: invalid option`, `case ... in\r: syntax error`).
            proc = subprocess.Popen(
                args,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                # No text=True — we manage encoding ourselves to keep \n pristine
            )
        except OSError as e:
            return -1, [f"Could not invoke wsl: {e}"]
        assert proc.stdin and proc.stdout

        # Belt-and-suspenders: strip any \r that snuck into the script source
        # (Drive sync sometimes converts files to CRLF) and write as raw bytes.
        clean = script.replace("\r\n", "\n").replace("\r", "\n")
        proc.stdin.write(clean.encode("utf-8"))
        proc.stdin.close()
        tail: list[str] = []
        for raw_line in proc.stdout:
            # stdout is bytes now (no text=True) — decode + strip both \r and \n
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
        rc = proc.wait(timeout=60 * 30)
        return rc, tail

    # ---- Phase 1: apt as root ----
    log.info("WSL install phase 1 (apt as root) in %s", distro)
    rc, tail = _run_phase(
        [wsl, "-d", distro, "-u", "root", "--", "bash", "-s"],
        _ROOT_BOOTSTRAP,
        "Phase 1: ROOT — installing system packages",
    )
    if rc != 0:
        return {
            "ok": False,
            "error": _explain_install_failure(rc, tail, phase="apt"),
            "tail": "\n".join(full_tail),
        }

    # ---- Phase 2: pip as default user ----
    log.info("WSL install phase 2 (pip as default user) in %s", distro)
    rc, tail = _run_phase(
        [wsl, "-d", distro, "--", "bash", "-s"],
        _USER_BOOTSTRAP,
        "Phase 2: USER — installing Vibechek + Essentia (slow)",
    )
    if rc != 0:
        return {
            "ok": False,
            "error": _explain_install_failure(rc, tail, phase="pip"),
            "tail": "\n".join(full_tail),
        }

    if on_progress:
        on_progress(100, 100, "Install complete")

    return {"ok": True, "distro": distro, "tail": "\n".join(full_tail)}


# ---------------------------------------------------------------------------
# CUDA library installer (optional GPU enablement inside WSL)
# ---------------------------------------------------------------------------


# Maps essentia's bundled TF 2.5 dlopen targets → Ubuntu 22.04/24.04 apt
# package names. We install the runtime-only variants (not the dev headers)
# to keep download size down (~600 MB instead of ~2 GB for the full toolkit).
_CUDA_APT_PACKAGES_BY_LIB = {
    "libcublas.so.11":   ["libcublas-11-8"],
    "libcublasLt.so.11": ["libcublas-11-8"],  # ships with libcublas
    "libcufft.so.10":    ["libcufft-11-8"],
    "libcurand.so.10":   ["libcurand-11-8"],
    "libcusolver.so.11": ["libcusolver-11-8"],
    "libcusparse.so.11": ["libcusparse-11-8"],
    "libcudnn.so.8":     ["libcudnn8"],
}

# The CUDA repo isn't enabled by default on most WSL Ubuntu installs. We add
# NVIDIA's keyring + repo before apt-get, then install the requested libs.
_CUDA_LIBS_BOOTSTRAP = r"""
set -e
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

echo "[1/3] Adding NVIDIA CUDA repository for Ubuntu $VERSION_ID..."
KEYRING_URL="https://developer.download.nvidia.com/compute/cuda/repos/ubuntu${UBUNTU_VER}/x86_64/cuda-keyring_1.1-1_all.deb"
TMPDEB="$(mktemp --suffix=.deb)"
if ! curl -fsSL "$KEYRING_URL" -o "$TMPDEB"; then
    echo "Failed to download CUDA keyring from $KEYRING_URL"
    exit 3
fi
dpkg -i "$TMPDEB"
rm -f "$TMPDEB"

echo "[2/3] Updating apt..."
apt-get update -y -q

echo "[3/3] Installing requested CUDA libraries: __PACKAGES__"
apt-get install -y -q --no-install-recommends __PACKAGES__

echo "DONE"
"""


def install_cuda_libs_in_wsl(
    distro: str,
    missing_libs: list[str],
    on_progress: ProgressCallback | None = None,
) -> dict:
    """Install the CUDA runtime libs essentia's bundled TF needs to use the GPU.

    `missing_libs` is the `missing_cuda_libs` list from `probe_engine_gpu`.
    We translate each lib name to its apt package, install them as root, then
    invalidate the engine GPU cache so the next probe sees the new state.

    This is the "Enable GPU" button in the UI.
    """
    if not IS_WINDOWS:
        return {"ok": False, "error": "Not running on Windows"}

    wsl = shutil.which("wsl") or shutil.which("wsl.exe")
    if not wsl:
        return {"ok": False, "error": "wsl.exe not found"}

    # Translate libs → unique apt packages
    packages: set[str] = set()
    unknown: list[str] = []
    for lib in missing_libs:
        if lib in _CUDA_APT_PACKAGES_BY_LIB:
            packages.update(_CUDA_APT_PACKAGES_BY_LIB[lib])
        else:
            unknown.append(lib)

    if not packages:
        return {
            "ok": False,
            "error": (
                f"No installable packages found for: {', '.join(missing_libs)}. "
                f"You may need to install them manually."
            ),
        }

    script = _CUDA_LIBS_BOOTSTRAP.replace("__PACKAGES__", " ".join(sorted(packages)))

    if on_progress:
        on_progress(0, 100, f"Installing CUDA libs ({len(packages)} packages) in {distro}...")

    step_pct = {"[1/3]": 10, "[2/3]": 30, "[3/3]": 50, "DONE": 95}
    tail: list[str] = []

    try:
        proc = subprocess.Popen(
            [wsl, "-d", distro, "-u", "root", "--", "bash", "-s"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
    except OSError as e:
        return {"ok": False, "error": f"Could not invoke wsl: {e}"}
    assert proc.stdin and proc.stdout

    clean = script.replace("\r\n", "\n").replace("\r", "\n")
    proc.stdin.write(clean.encode("utf-8"))
    proc.stdin.close()
    for raw_line in proc.stdout:
        line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
        tail.append(line)
        if len(tail) > 200:
            tail.pop(0)
        for marker, pct in step_pct.items():
            if line.startswith(marker):
                if on_progress:
                    on_progress(pct, 100, line)
                break

    try:
        rc = proc.wait(timeout=60 * 30)  # apt can be slow on first install
    except subprocess.TimeoutExpired:
        proc.kill()
        return {"ok": False, "error": "CUDA lib install timed out after 30 min"}

    if rc != 0:
        return {
            "ok": False,
            "error": f"CUDA lib install exited with {rc}",
            "tail": "\n".join(tail[-30:]),
            "unknown_libs": unknown,
            "packages_attempted": sorted(packages),
        }

    # Invalidate the engine GPU cache so the next probe reflects the new state
    with _ENGINE_GPU_CACHE_LOCK:
        _ENGINE_GPU_CACHE.clear()

    if on_progress:
        on_progress(100, 100, "CUDA libs installed; re-probe GPU to verify")

    return {
        "ok": True,
        "distro": distro,
        "packages_installed": sorted(packages),
        "unknown_libs": unknown,
        "tail": "\n".join(tail[-30:]),
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
    elif "ERROR: Could not install packages" in tail_text:
        hints.append("pip install failed — see the tail for the package and reason.")

    hint_str = ("\n\nHint: " + " ".join(hints)) if hints else ""
    return (
        f"Phase '{phase}' exited with {rc}.\n\nLast output:\n  {last_lines}{hint_str}"
    )


# ---------------------------------------------------------------------------
# Running vibechek inside WSL
# ---------------------------------------------------------------------------


def run_vibechek_in_wsl(
    distro: str,
    args: list[str],
    on_stderr_line: Callable[[str], None] | None = None,
    timeout: int | None = None,
) -> subprocess.CompletedProcess:
    """Run `vibechek <args>` inside `distro` and return the completed process.

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

    cmd_str = "vibechek " + " ".join(_shell_quote(a) for a in args)
    proc_cmd = [wsl, "-d", distro, "--", "bash", "-lc", cmd_str]

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

    # Watchdog: poll the cancellation flag every 500ms; terminate (then kill)
    # the child if a cancel comes through.
    cancel_event = _threading.Event()

    def _watch_cancel() -> None:
        while not cancel_event.is_set() and proc.poll() is None:
            if cancellation.is_cancelled():
                log.info("WSL subprocess cancellation requested — terminating PID %s", proc.pid)
                try:
                    proc.terminate()
                except OSError:
                    pass
                # Give it a grace period, then SIGKILL equivalent
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

    if on_stderr_line and proc.stderr is not None:
        def _reader() -> None:
            for line in proc.stderr:  # type: ignore[union-attr]
                on_stderr_line(line.rstrip())

        t = _threading.Thread(target=_reader, daemon=True)
        t.start()

    if proc.stdout is not None:
        for line in proc.stdout:
            stdout_chunks.append(line)

    rc = proc.wait(timeout=timeout)
    cancel_event.set()  # tell the watchdog we're done

    if cancellation.is_cancelled():
        raise cancellation.CancelledError("WSL analyze cancelled by user")

    return subprocess.CompletedProcess(
        args=proc_cmd,
        returncode=rc,
        stdout="".join(stdout_chunks),
        stderr="",  # already streamed via on_stderr_line
    )


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


def _wsl_run(cmd: list[str], timeout: int = 10) -> subprocess.CompletedProcess:
    """Run a wsl.exe command, decoding the UTF-16 output Windows uses for it."""
    result = subprocess.run(cmd, capture_output=True, timeout=timeout, check=False)
    # wsl.exe outputs UTF-16 LE by default on Windows. Try that first.
    for enc in ("utf-16-le", "utf-8", "cp1252"):
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
    "engine_gpu_info_to_dict",
    "win_to_wsl_path",
    "wsl_to_win_path",
    "to_dict",
]
