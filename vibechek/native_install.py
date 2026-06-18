"""Native Essentia install for Linux & macOS.

On Linux and macOS, Essentia *does* have a PyPI wheel (`essentia-tensorflow`),
but the desktop app's sidecar runs as a PyInstaller bundle — there's no pip
inside that bundle. So we mirror the Windows-via-WSL approach: create a
managed venv at `~/.vibechek/venv/`, install `essentia-tensorflow + vibechek`
into it, and route the analyze step through *that* venv's `vibechek` binary.

This module is the Linux/macOS analog of `vibechek.wsl` — same idea, no WSL.

The flow:
  1. `probe_native_venv()`  — does ~/.vibechek/venv/ exist + have essentia?
  2. `install_essentia_native(on_progress)` — create venv + pip install.
     Streams pip output line-by-line as progress notifications.
  3. `run_vibechek_in_native_venv(args)` — analog to run_vibechek_in_wsl.

Why a separate venv instead of `pip install --user`?
- User-site installs leak into whatever Python the user happens to have, can
  conflict with system packages, and break when they upgrade Python.
- A managed venv at a known path is hermetic and easy to delete cleanly.
- Mirrors the WSL flow so the analyze routing code stays simple.

Skip on Windows entirely: Windows doesn't have a working essentia wheel,
which is the whole reason we route through WSL there.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
import threading as _threading
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

from vibechek.platform import (  # noqa: F401  (IS_WINDOWS re-exported from the single platform source; guarded by test_platform)
    IS_LINUX,
    IS_MAC,
    IS_WINDOWS,
)

log = logging.getLogger(__name__)

ProgressCallback = Callable[[int, int, str], None]

# Skip the whole module on Windows — WSL is the path there.
IS_SUPPORTED = IS_MAC or IS_LINUX

# Where the managed venv lives. Keeping it under the user's home (not
# user_data_dir) makes it visible in `ls ~/.vibechek/` and matches the WSL
# install layout.
VENV_DIR = Path.home() / ".vibechek" / "venv"


def _venv_dir(engine: str = "essentia_tf") -> Path:
    """Managed-venv path for an inference engine.

    "essentia_tf" → ``~/.vibechek/venv`` (essentia-tensorflow). "onnx" →
    ``~/.vibechek/venv-onnx`` (plain essentia + onnxruntime). The two essentia
    builds can't coexist in one venv (both ship the ``essentia`` module), so
    the ONNX engine gets its own.
    """
    return VENV_DIR.parent / "venv-onnx" if engine == "onnx" else VENV_DIR


@dataclass
class NativeVenvStatus:
    """What we know about the managed venv on this machine."""

    supported: bool                    # False on Windows
    venv_dir: str                      # ~/.vibechek/venv as a string
    venv_python: str | None = None     # Absolute path to python in the venv
    venv_vibechek: str | None = None   # Absolute path to vibechek CLI in venv
    essentia_installed: bool = False
    essentia_version: str | None = None
    vibechek_installed: bool = False
    vibechek_version: str | None = None
    error: str | None = None


def to_dict(s: NativeVenvStatus) -> dict:
    return asdict(s)


# ---------------------------------------------------------------------------
# Probe — does the venv exist and what's in it?
# ---------------------------------------------------------------------------


def probe_native_venv(engine: str = "essentia_tf") -> NativeVenvStatus:
    """Snapshot the managed venv state for the given inference engine.

    Fast: no subprocess calls, just disk inspection. The result is what the
    Settings UI shows in the "Engine" row on Linux/macOS. `engine="onnx"`
    inspects ``~/.vibechek/venv-onnx`` instead of the default ``venv``.
    """
    vd = _venv_dir(engine)
    status = NativeVenvStatus(
        supported=IS_SUPPORTED,
        venv_dir=str(vd),
    )

    if not IS_SUPPORTED:
        return status

    # Find the venv python (bin/python on Unix, Scripts/python.exe on Windows
    # — even though we don't support Windows here, leave the path lookup
    # symmetric for tests and future-proofing).
    candidate_pythons = [
        vd / "bin" / "python3",
        vd / "bin" / "python",
        vd / "Scripts" / "python.exe",
    ]
    py = next((p for p in candidate_pythons if p.exists()), None)
    if py is None:
        return status
    status.venv_python = str(py)

    # vibechek CLI binary inside the venv
    candidate_clis = [
        vd / "bin" / "vibechek",
        vd / "Scripts" / "vibechek.exe",
    ]
    cli = next((p for p in candidate_clis if p.exists()), None)
    if cli is not None:
        status.venv_vibechek = str(cli)
        status.vibechek_installed = True
        # Best-effort version probe — small subprocess, ~50ms
        try:
            result = subprocess.run(
                [str(cli), "--version"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                m = re.search(r"version\s+(\S+)", result.stdout)
                if m:
                    status.vibechek_version = m.group(1)
        except (OSError, subprocess.TimeoutExpired):
            pass

    # Disk-only check for essentia (avoids the ~10s TF load that
    # `import essentia` would trigger). NB: the wildcard segment must be
    # globbed from the venv ROOT — `Path.glob()` only expands wildcards in
    # the pattern argument, so the old `(vd/"lib"/"python3.*").glob(
    # "site-packages")` treated `python3.*` as a literal directory name and
    # the Unix venv layout never matched: Linux/macOS always reported
    # "essentia not installed" even right after a successful install.
    site_packages_globs = [
        "lib/python3.*/site-packages",  # Unix venv layout
        "Lib/site-packages",  # Windows venv layout
    ]
    for pattern in site_packages_globs:
        for sp in vd.glob(pattern):
            # `essentia-tensorflow` installs both `essentia/` and a dist-info
            for d in sp.glob("essentia*.dist-info"):
                status.essentia_installed = True
                # Parse version from dir name. Handle both the essentia_tf venv
                # ("essentia_tensorflow-2.1b6.dev1110.dist-info") and the ONNX
                # venv, which installs plain essentia ("essentia-2.1b6....").
                m = re.match(r"^essentia(?:[_-][^-]+)?-([^-]+)\.dist-info$", d.name)
                if m:
                    status.essentia_version = m.group(1)
                break
            if status.essentia_installed:
                break
        if status.essentia_installed:
            break

    return status


# ---------------------------------------------------------------------------
# Install
# ---------------------------------------------------------------------------


def _find_host_python() -> str | None:
    """Locate a Python 3.10+ interpreter on the host to bootstrap the venv with.

    We don't trust `sys.executable` because the desktop app's sidecar is a
    PyInstaller bundle — `sys.executable` would point at the frozen binary,
    not a real python. Instead, look for system python in order of preference.
    """
    # Common names — pick the highest version we can find
    candidates = ["python3.13", "python3.12", "python3.11", "python3.10", "python3", "python"]
    for name in candidates:
        path = shutil.which(name)
        if not path:
            continue
        # Sanity check: is it 3.10+? Run --version and parse.
        try:
            result = subprocess.run(
                [path, "--version"],
                capture_output=True, text=True, timeout=5,
            )
            m = re.search(r"Python (\d+)\.(\d+)", result.stdout + result.stderr)
            if m:
                major, minor = int(m.group(1)), int(m.group(2))
                if major == 3 and minor >= 10:
                    return path
        except (OSError, subprocess.TimeoutExpired):
            continue
    return None


def install_essentia_native(
    on_progress: ProgressCallback | None = None,
    *,
    vibechek_source: str | None = None,
    engine: str = "essentia_tf",
) -> dict:
    """Create the managed venv and install the ML stack + vibechek.

    `engine="essentia_tf"` installs **essentia-tensorflow** into
    ``~/.vibechek/venv`` (default). `engine="onnx"` installs **plain essentia +
    onnxruntime** into ``~/.vibechek/venv-onnx`` (the TF-free engine). The two
    essentia builds can't coexist in one venv, so the ONNX engine gets its own.

    Streams pip output line-by-line. Returns a dict the GUI can render.

    `vibechek_source` defaults to the GitHub master branch — same as the WSL
    install path. Override for local testing (e.g. an editable install
    against the dev checkout).
    """
    if not IS_SUPPORTED:
        return {"ok": False, "error": f"Native install not supported on {sys.platform}"}

    vd = _venv_dir(engine)

    host_python = _find_host_python()
    if not host_python:
        # Build OS-specific guidance so the user sees the right command.
        if IS_MAC:
            hint = (
                "On macOS, install Homebrew first (https://brew.sh) then:\n"
                "    brew install python@3.12\n"
                "Or use the official installer at https://www.python.org/downloads/macos/\n"
                "(Vibechek needs Python 3.10 or newer; macOS doesn't ship "
                "Python by default anymore.)"
            )
        elif IS_LINUX:
            hint = (
                "On Debian/Ubuntu: sudo apt install python3 python3-pip python3-venv\n"
                "On Fedora/RHEL:   sudo dnf install python3 python3-pip\n"
                "On Arch:          sudo pacman -S python python-pip\n"
                "Vibechek needs Python 3.10 or newer."
            )
        else:
            hint = "Install Python 3.10 or newer for your OS."
        return {
            "ok": False,
            "error": f"No Python 3.10+ found on PATH.\n\n{hint}",
        }

    # Lazy import — same dependency-direction reasoning as _run_with_progress.
    from vibechek import cancellation

    if on_progress:
        on_progress(0, 100, f"Using host Python: {host_python}")

    vd.parent.mkdir(parents=True, exist_ok=True)

    # ---- Step 1: create venv (or skip if it exists) ----
    if not (vd / "bin" / "python3").exists() and not (vd / "bin" / "python").exists():
        if on_progress:
            on_progress(5, 100, f"Creating venv at {vd}...")
        rc, _stdout, stderr, cancelled = _run_subprocess_cancellable(
            [host_python, "-m", "venv", str(vd)],
            timeout=120,
        )
        if cancelled or cancellation.is_cancelled():
            return {"ok": False, "error": "Cancelled by user", "cancelled": True}
        if rc != 0:
            return {
                "ok": False,
                "error": f"venv creation exited with {rc}\n{stderr[-1000:]}",
            }
    elif on_progress:
        on_progress(10, 100, f"Venv already exists at {vd}, reusing")

    venv_python = next(
        p for p in [vd / "bin" / "python3", vd / "bin" / "python"]
        if p.exists()
    )
    venv_pip = [str(venv_python), "-m", "pip"]

    # ---- Step 2: upgrade pip + wheel ----
    if on_progress:
        on_progress(15, 100, "Upgrading pip + wheel...")
    rc, tail = _run_with_progress(
        [*venv_pip, "install", "--upgrade", "--quiet", "pip", "wheel"],
        on_progress=lambda line: on_progress and on_progress(15, 100, line[:120]),
        timeout=120,
    )
    if cancellation.is_cancelled():
        return {"ok": False, "error": "Cancelled by user", "cancelled": True}
    if rc != 0:
        return _fail("pip/wheel upgrade", rc, tail)

    # Clean swap: onnxruntime / -gpu / -rocm all ship the same `onnxruntime`
    # module, so a CPU→GPU re-setup must drop the old distribution first.
    if engine == "onnx":
        _run_subprocess_cancellable(
            [*venv_pip, "uninstall", "-y", "onnxruntime", "onnxruntime-gpu", "onnxruntime-rocm"],
            timeout=60,
        )

    # ---- Step 3: the ML stack (the slow ~3-5 min step) ----
    # onnx → plain essentia + onnxruntime (TF-free). GPU is the point of ONNX,
    # picked per platform/vendor (build_providers/onnx_backend handle the EP):
    #   macOS        → onnxruntime (CoreML EP ships in the wheel — Apple GPU/ANE)
    #   NVIDIA Linux → onnxruntime-gpu + CUDA 12 wheels (preload_dlls() loads them)
    #   AMD Linux    → onnxruntime-rocm (best-effort; ROCm not viable in WSL)
    #   else         → CPU onnxruntime
    # essentia_tf → essentia-tensorflow (CUDA via the separate Enable-GPU step).
    if engine == "onnx":
        if IS_MAC:
            ml_packages = ["essentia", "onnxruntime"]
        elif shutil.which("nvidia-smi"):
            ml_packages = [
                "essentia", "onnxruntime-gpu",
                "nvidia-cuda-runtime-cu12", "nvidia-cudnn-cu12", "nvidia-cublas-cu12",
                "nvidia-cufft-cu12", "nvidia-curand-cu12", "nvidia-cusparse-cu12",
                "nvidia-cuda-nvrtc-cu12",
            ]
        elif shutil.which("rocminfo") or shutil.which("rocm-smi"):
            ml_packages = ["essentia", "onnxruntime-rocm"]
        else:
            ml_packages = ["essentia", "onnxruntime"]
    else:
        ml_packages = ["essentia-tensorflow"]
    ml_label = (
        "essentia + GPU onnxruntime"
        if any(p.startswith("onnxruntime-") for p in ml_packages)
        else " + ".join(ml_packages)
    )
    # The GPU stacks are multi-GB (the CUDA 12 wheel set alone is ~2 GB) — on an
    # ordinary home connection that blows any quarter-hour ceiling (live-verified:
    # cudnn alone took 10+ min at ~1.2 MB/s), so align with the 2 h wall-clock the
    # WSL genre setups use for arbitrary-connection downloads. The CPU sets are
    # ≤ ~0.5 GB → the WSL essentia install's 30 min precedent. Either way the
    # step streams progress and stays cancellable; the ceiling only catches a
    # true hang.
    gpu_stack = any(p.startswith(("onnxruntime-", "nvidia-")) for p in ml_packages)
    ml_timeout = 60 * 120 if gpu_stack else 60 * 30
    if on_progress:
        eta = "multi-GB GPU stack — can take tens of minutes" if gpu_stack else "~3-5 min"
        on_progress(25, 100, f"Installing {ml_label} (this is the slow step, {eta})...")
    rc, tail = _run_with_progress(
        [*venv_pip, "install", *ml_packages],
        on_progress=lambda line: on_progress and on_progress(
            _parse_pip_pct(line, base=25, span=55), 100, line[:120],
        ),
        timeout=ml_timeout,
    )
    if cancellation.is_cancelled():
        return {"ok": False, "error": "Cancelled by user", "cancelled": True}
    if rc != 0:
        return _fail(f"{ml_label} install", rc, tail)

    # ---- Step 4: vibechek itself ----
    if on_progress:
        on_progress(85, 100, "Installing vibechek...")
    source = vibechek_source or "git+https://github.com/captinjack99/Vibechek.git"
    rc, tail = _run_with_progress(
        [*venv_pip, "install", "--upgrade", source],
        on_progress=lambda line: on_progress and on_progress(90, 100, line[:120]),
        timeout=60 * 10,
    )
    if cancellation.is_cancelled():
        return {"ok": False, "error": "Cancelled by user", "cancelled": True}
    if rc != 0:
        return _fail("vibechek install", rc, tail)

    # ---- Step 5: verify ----
    if on_progress:
        on_progress(95, 100, "Verifying install...")
    venv_vibechek = vd / "bin" / "vibechek"
    v_rc, v_stdout, v_stderr, v_cancel = _run_subprocess_cancellable(
        [str(venv_vibechek), "--version"], timeout=10,
    )
    if v_cancel or cancellation.is_cancelled():
        return {"ok": False, "error": "Cancelled by user", "cancelled": True}
    e_rc, e_stdout, e_stderr, e_cancel = _run_subprocess_cancellable(
        [str(venv_python), "-c", "import essentia; print(essentia.__version__)"],
        timeout=30,
    )
    if e_cancel or cancellation.is_cancelled():
        return {"ok": False, "error": "Cancelled by user", "cancelled": True}
    # Build a CompletedProcess-shaped result so the existing code path below
    # doesn't need to change shape.
    version_result = subprocess.CompletedProcess(
        [str(venv_vibechek), "--version"], v_rc, v_stdout, v_stderr,
    )
    essentia_result = subprocess.CompletedProcess(
        [str(venv_python), "-c", "import essentia"], e_rc, e_stdout, e_stderr,
    )

    if version_result.returncode != 0 or essentia_result.returncode != 0:
        return {
            "ok": False,
            "error": (
                f"Install completed but verification failed.\n"
                f"vibechek --version → rc={version_result.returncode}: {version_result.stderr[:300]}\n"
                f"import essentia → rc={essentia_result.returncode}: {essentia_result.stderr[:300]}"
            ),
        }

    if on_progress:
        on_progress(100, 100, "Install complete")

    return {
        "ok": True,
        "venv_dir": str(vd),
        "venv_python": str(venv_python),
        "venv_vibechek": str(venv_vibechek),
        "vibechek_version": version_result.stdout.strip(),
        "essentia_version": essentia_result.stdout.strip(),
    }


def _fail(stage: str, rc: int, tail: list[str]) -> dict:
    """Build a structured failure dict with the last-N lines of output."""
    last = "\n".join(tail[-15:])
    return {
        "ok": False,
        "error": f"{stage} exited with {rc}.\n\nLast output:\n{last}",
        "tail": "\n".join(tail[-60:]),
    }


_PIP_DOWNLOAD_RE = re.compile(r"^\s*Downloading\s+(\S+)")
_PIP_INSTALLING_RE = re.compile(r"^Installing collected packages:")


def _parse_pip_pct(line: str, base: int, span: int) -> int:
    """Best-effort progress estimate from pip stdout lines.

    pip doesn't emit percentages; we use markers ("Downloading X", "Installing
    collected packages") as rough waypoints inside [base, base+span].
    """
    if _PIP_DOWNLOAD_RE.match(line):
        return base + span // 3
    if _PIP_INSTALLING_RE.match(line):
        return base + (span * 2) // 3
    if line.startswith("Successfully installed"):
        return base + span
    return base


def _run_with_progress(
    args: list[str],
    on_progress: Callable[[str], None],
    timeout: int,
    env: dict[str, str] | None = None,
) -> tuple[int, list[str]]:
    """Run `args`, stream stdout (+stderr merged) to `on_progress` line-by-line.

    Returns (returncode, last-N-lines). On timeout, kills the process and
    returns -1 plus whatever we collected. `env` overrides the child
    environment (None = inherit).

    Cooperatively cancellable: a watchdog thread polls
    `vibechek.cancellation.is_cancelled()` every 500ms and terminates the
    child process if the user requests cancel. Without this, the GUI's Cancel
    button would flip the flag but the (potentially multi-minute) pip install
    would keep running, holding the long-op lock. Returns rc=-1 with a
    `cancelled by user` marker line so callers can surface it cleanly.
    """
    # Local import — keeps this module importable without pulling the
    # cancellation module in (it's tiny but the dependency direction matters
    # for the wsl module too).
    from vibechek import cancellation

    try:
        proc = subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=env,
        )
    except OSError as e:
        return -1, [f"Could not invoke: {e}"]

    tail: list[str] = []
    assert proc.stdout

    # Use a thread to drain stdout so .wait(timeout=) actually enforces the limit
    def _drain() -> None:
        for line in proc.stdout:  # type: ignore[union-attr]
            stripped = line.rstrip()
            tail.append(stripped)
            if len(tail) > 400:
                tail.pop(0)
            try:
                on_progress(stripped)
            except Exception:  # noqa: BLE001
                pass

    drainer = _threading.Thread(target=_drain, daemon=True)
    drainer.start()

    # Watchdog: tear the child down on cancel. Same pattern as
    # run_vibechek_in_native_venv / run_vibechek_in_wsl.
    cancel_done = _threading.Event()
    cancelled_flag: dict[str, bool] = {"v": False}

    def _watch_cancel() -> None:
        while not cancel_done.is_set() and proc.poll() is None:
            if cancellation.is_cancelled():
                cancelled_flag["v"] = True
                log.info(
                    "Install subprocess cancellation requested — terminating PID %s",
                    proc.pid,
                )
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

    watchdog = _threading.Thread(target=_watch_cancel, daemon=True)
    watchdog.start()

    try:
        rc = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        cancel_done.set()
        return -1, tail + [f"Killed after {timeout}s timeout"]
    cancel_done.set()
    drainer.join(timeout=5)
    if cancelled_flag["v"]:
        return -1, tail + ["Cancelled by user"]
    return rc, tail


def _run_subprocess_cancellable(
    args: list[str],
    timeout: int,
) -> tuple[int, str, str, bool]:
    """Run a non-streaming subprocess with cancellation polling.

    For short, capture_output-style calls (venv create, --version checks).
    Same watchdog pattern as `_run_with_progress`. Returns
    `(returncode, stdout, stderr, cancelled)`.
    """
    from vibechek import cancellation

    try:
        proc = subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as e:
        return -1, "", str(e), False

    cancel_done = _threading.Event()
    cancelled_flag: dict[str, bool] = {"v": False}

    def _watch_cancel() -> None:
        while not cancel_done.is_set() and proc.poll() is None:
            if cancellation.is_cancelled():
                cancelled_flag["v"] = True
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

    watchdog = _threading.Thread(target=_watch_cancel, daemon=True)
    watchdog.start()

    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        rc = proc.returncode
    except subprocess.TimeoutExpired:
        proc.kill()
        cancel_done.set()
        return -1, "", "timeout", False
    cancel_done.set()
    return rc, stdout or "", stderr or "", cancelled_flag["v"]


# ---------------------------------------------------------------------------
# Routing — run vibechek inside the managed venv
# ---------------------------------------------------------------------------


def run_vibechek_in_native_venv(
    args: list[str],
    on_stderr_line: Callable[[str], None] | None = None,
    timeout: int | None = None,
    engine: str = "essentia_tf",
) -> subprocess.CompletedProcess:
    """Run `~/.vibechek/<venv>/bin/vibechek <args>` and return the completed process.

    The Linux/macOS analog of `run_vibechek_in_wsl`. `engine` selects the venv:
    "essentia_tf" → ``venv``, "onnx" → ``venv-onnx``. Same cooperative
    cancellation pattern: a watchdog thread polls
    `vibechek.cancellation.is_cancelled()` and terminates the child if a
    cancel comes in.
    """
    from vibechek import cancellation

    status = probe_native_venv(engine)
    if not status.vibechek_installed or not status.venv_vibechek:
        raise FileNotFoundError(
            f"vibechek is not installed in the managed venv at {_venv_dir(engine)}. "
            f"Run install_essentia_native(engine={engine!r}) first."
        )

    cmd = [status.venv_vibechek, *args]
    log.info("Native venv exec: %s", cmd)

    # Activate the structured-progress event channel inside the managed-venv
    # `vibechek analyze` process. The parent (this sidecar) parses
    # VIBECHEK_EVENT lines out of stderr via `_make_event_aware_line_handler`
    # so the GUI gets per-stage and per-track feedback instead of sitting at
    # "starting…" through preflight + worker spawn. See
    # vibechek/analyzer.py:_emit_event for the line schema.
    venv_env = os.environ.copy()
    venv_env["VIBECHEK_STREAM_PROGRESS"] = "1"

    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env=venv_env,
    )

    cancel_event = _threading.Event()

    def _watch_cancel() -> None:
        while not cancel_event.is_set() and proc.poll() is None:
            if cancellation.is_cancelled():
                log.info("Native venv cancellation requested — terminating PID %s", proc.pid)
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

    # ALWAYS drain stderr (see run_vibechek_in_wsl): stderr is a PIPE, and
    # leaving it unread while we block on stdout deadlocks a verbose child
    # once the stderr pipe buffer fills. The callback is optional; draining
    # is mandatory.
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
    cancel_event.set()

    if cancellation.is_cancelled():
        raise cancellation.CancelledError("Native venv analyze cancelled by user")

    return subprocess.CompletedProcess(
        args=cmd,
        returncode=rc,
        stdout="".join(stdout_chunks),
        stderr="",
    )



# ---------------------------------------------------------------------------
# Opt-in genre engines (CLAP student / online resolver) — native analogs of
# wsl.setup_clap_in_wsl / setup_resolver_in_wsl. Same venv layout, same
# artifact paths (~/.vibechek/clap/music_clap.pt, ~/ollama/bin/ollama), so the
# analyze-time consumers (analyzer._maybe_load_clap, genre_web.ensure_backend)
# work unchanged on native Linux/macOS.
# ---------------------------------------------------------------------------

# The 2.2 GB CLAP checkpoint; anything below this is a truncated/error download
# (mirrors the WSL script's `stat -c%s` floor).
_CLAP_MIN_CKPT_BYTES = 1_500_000_000


def _ollama_tarball() -> tuple[str, str]:
    """(url, kind) of the pinned no-sudo Ollama tarball for this OS/arch.

    Single-sources the release pin from `vibechek.wsl` (the WSL setup installs
    the same build). Linux ships `.tar.zst` (CUDA/ROCm libs bundled; extracts
    `bin/ollama` + `lib/` into ~/ollama); macOS ships a plain `.tgz` holding
    the bare universal `ollama` binary at the tar root (verified against the
    v0.30.4 asset), which we land at ~/ollama/bin/ollama ourselves.
    """
    import platform as _platform  # noqa: PLC0415

    from vibechek.wsl import _OLLAMA_RELEASE  # noqa: PLC0415

    base = f"https://github.com/ollama/ollama/releases/download/{_OLLAMA_RELEASE}"
    if IS_MAC:
        return f"{base}/ollama-darwin.tgz", "tgz"
    machine = _platform.machine().lower()
    arch = "arm64" if machine in ("arm64", "aarch64") else "amd64"
    return f"{base}/ollama-linux-{arch}.tar.zst", "tar.zst"


def _genre_venv_python(engine: str) -> tuple[Path | None, str | None]:
    """The analysis venv's python for `engine`, or (None, error) if absent.

    The genre extras install INTO the engine's venv (one worker runs them
    alongside essentia/onnx), so the engine setup must have run first — same
    precondition as the WSL scripts' `[ -x $VENV/bin/pip ]` guard.
    """
    vd = _venv_dir(engine)
    py = next((p for p in (vd / "bin" / "python3", vd / "bin" / "python") if p.exists()), None)
    if py is None:
        return None, (
            f"The analysis venv at {vd} is missing — run the engine setup "
            "(Install analysis engine / Set up ONNX engine) first."
        )
    return py, None


def setup_clap_native(
    on_progress: ProgressCallback | None = None,
    engine: str = "essentia_tf",
) -> dict:
    """Install the CLAP genre student into the native managed venv (Linux/macOS).

    Mirror of `wsl.setup_clap_in_wsl`, in Python instead of a WSL bash script:
      1. torch + torchvision (CPU wheel index first — CLAP is pinned to CPU by
         design; plain-index fallback) + laion-clap + soundfile into the
         engine's venv;
      2. the ~2.2 GB checkpoint → ~/.vibechek/clap/music_clap.pt (idempotent,
         .partial-staged, size-validated, cancellable mid-stream);
      3. import-verify inside the venv.
    Cancellable; streams progress. Returns the WSL helpers' dict shape.
    """
    if not IS_SUPPORTED:
        return {"ok": False, "error": f"Native genre-engine setup not supported on {sys.platform}"}

    from vibechek import cancellation  # noqa: PLC0415
    from vibechek.clap_genre import _CHECKPOINT_NAME, _CHECKPOINT_URL  # noqa: PLC0415

    venv_python, err = _genre_venv_python(engine)
    if venv_python is None:
        return {"ok": False, "error": err}
    venv_pip = [str(venv_python), "-m", "pip"]

    def _step(pct: int, msg: str) -> None:
        if on_progress:
            on_progress(pct, 100, msg)

    # ---- 1. deps. The CPU torch index keeps it ~200 MB; the plain-index
    # fallback (some platforms lack cpu-index wheels) can pull the multi-GB
    # CUDA build, so the step gets the GPU-stack 2 h ceiling.
    _step(2, "[1/3] Installing CLAP deps (torch, torchvision, laion-clap)...")
    rc, tail = _run_with_progress(
        [*venv_pip, "install", "--quiet", "torch", "torchvision",
         "--index-url", "https://download.pytorch.org/whl/cpu"],
        on_progress=lambda line: _step(10, line[:120]),
        timeout=60 * 120,
    )
    if cancellation.is_cancelled():
        return {"ok": False, "error": "Cancelled by user", "cancelled": True}
    if rc != 0:
        _step(10, "CPU wheel index failed — retrying from the default index...")
        rc, tail = _run_with_progress(
            [*venv_pip, "install", "--quiet", "torch", "torchvision"],
            on_progress=lambda line: _step(15, line[:120]),
            timeout=60 * 120,
        )
        if cancellation.is_cancelled():
            return {"ok": False, "error": "Cancelled by user", "cancelled": True}
        if rc != 0:
            return _fail("torch install", rc, tail)
    rc, tail = _run_with_progress(
        [*venv_pip, "install", "--quiet", "laion-clap", "soundfile"],
        on_progress=lambda line: _step(35, line[:120]),
        timeout=60 * 30,
    )
    if cancellation.is_cancelled():
        return {"ok": False, "error": "Cancelled by user", "cancelled": True}
    if rc != 0:
        return _fail("laion-clap install", rc, tail)

    # ---- 2. checkpoint (idempotent; the downloader stages to .partial,
    # validates Content-Length, and aborts mid-stream on cancel).
    ckpt = Path.home() / ".vibechek" / "clap" / _CHECKPOINT_NAME
    if ckpt.exists() and ckpt.stat().st_size >= _CLAP_MIN_CKPT_BYTES:
        _step(90, "CLAP checkpoint already present, reusing")
    else:
        _step(50, "[2/3] Downloading CLAP checkpoint (~2.2 GB, one-time)...")
        ckpt.parent.mkdir(parents=True, exist_ok=True)
        from vibechek.analyzer import _download_from_mirrors  # noqa: PLC0415

        def _dl_progress(done: int, total: int) -> None:
            pct = 50 + int(40 * done / total) if total > 0 else 60
            _step(pct, f"CLAP checkpoint ({done // 2**20} MB/{total // 2**20} MB)")

        try:
            _download_from_mirrors(
                [_CHECKPOINT_URL], ckpt, label=_CHECKPOINT_NAME, on_progress=_dl_progress,
            )
        except cancellation.CancelledError:
            return {"ok": False, "error": "Cancelled by user", "cancelled": True}
        except RuntimeError as e:
            return {"ok": False, "error": f"CLAP checkpoint download failed: {e}"}
        if ckpt.stat().st_size < _CLAP_MIN_CKPT_BYTES:
            size = ckpt.stat().st_size
            ckpt.unlink(missing_ok=True)
            return {"ok": False,
                    "error": f"CLAP checkpoint download incomplete ({size} bytes)"}

    # ---- 3. verify
    _step(95, "[3/3] Verifying...")
    rc, out, errout, cancelled = _run_subprocess_cancellable(
        [str(venv_python), "-c", "import laion_clap, soundfile; print('clap import ok')"],
        timeout=180,
    )
    if cancelled or cancellation.is_cancelled():
        return {"ok": False, "error": "Cancelled by user", "cancelled": True}
    if rc != 0:
        return {"ok": False,
                "error": f"CLAP deps installed but import-verify failed:\n{errout[-800:]}"}
    _step(100, "CLAP genre engine ready")
    return {"ok": True, "tail": out.strip()}


def setup_resolver_native(
    on_progress: ProgressCallback | None = None,
    engine: str = "essentia_tf",
    model: str = "qwen2.5:7b",
) -> dict:
    """Install the online genre resolver natively (Linux/macOS).

    Mirror of `wsl.setup_resolver_in_wsl`: ddgs + zstandard into the engine's
    venv, a no-sudo Ollama under ~/ollama (platform tarball via
    `_ollama_tarball`), start the server (reuses `genre_web.ensure_backend` —
    the same code that self-heals it at analyze time), pull `model`, verify.
    Cancellable; streams progress. Returns the WSL helpers' dict shape.
    """
    if not IS_SUPPORTED:
        return {"ok": False, "error": f"Native genre-engine setup not supported on {sys.platform}"}

    import tempfile  # noqa: PLC0415

    from vibechek import cancellation  # noqa: PLC0415

    venv_python, err = _genre_venv_python(engine)
    if venv_python is None:
        return {"ok": False, "error": err}
    venv_pip = [str(venv_python), "-m", "pip"]

    def _step(pct: int, msg: str) -> None:
        if on_progress:
            on_progress(pct, 100, msg)

    # ---- 1. python deps (zstandard also decompresses the linux tarball below)
    _step(2, "[1/4] Installing ddgs + zstandard...")
    rc, tail = _run_with_progress(
        [*venv_pip, "install", "--quiet", "ddgs", "zstandard"],
        on_progress=lambda line: _step(8, line[:120]),
        timeout=60 * 10,
    )
    if cancellation.is_cancelled():
        return {"ok": False, "error": "Cancelled by user", "cancelled": True}
    if rc != 0:
        return _fail("ddgs install", rc, tail)

    # ---- 2. the no-sudo Ollama under ~/ollama (idempotent)
    ollama_bin = Path.home() / "ollama" / "bin" / "ollama"
    if not ollama_bin.is_file():
        url, kind = _ollama_tarball()
        _step(15, f"[2/4] Installing Ollama (no-sudo, {kind})...")
        from vibechek.analyzer import _download_from_mirrors  # noqa: PLC0415

        with tempfile.TemporaryDirectory(prefix="vibechek-ollama-") as td:
            archive = Path(td) / f"ollama.{kind}"

            def _dl_progress(done: int, total: int) -> None:
                pct = 15 + int(25 * done / total) if total > 0 else 25
                _step(pct, f"Ollama download ({done // 2**20} MB/{total // 2**20} MB)")

            try:
                _download_from_mirrors([url], archive, label="ollama", on_progress=_dl_progress)
            except cancellation.CancelledError:
                return {"ok": False, "error": "Cancelled by user", "cancelled": True}
            except RuntimeError as e:
                return {"ok": False, "error": f"Ollama download failed: {e}"}

            _step(42, "Unpacking Ollama...")
            target = Path.home() / "ollama"
            if kind == "tar.zst":
                # Decompress with the venv's zstandard (installed in step 1) —
                # no system zstd dependency; then plain tar (linux layout has
                # bin/ + lib/ at the archive root).
                tar_path = Path(td) / "ollama.tar"
                rc, out, errout, cancelled = _run_subprocess_cancellable(
                    [str(venv_python), "-c",
                     "import zstandard,sys;"
                     "fi=open(sys.argv[1],'rb');fo=open(sys.argv[2],'wb');"
                     "zstandard.ZstdDecompressor().copy_stream(fi,fo)",
                     str(archive), str(tar_path)],
                    timeout=60 * 10,
                )
                if cancelled or cancellation.is_cancelled():
                    return {"ok": False, "error": "Cancelled by user", "cancelled": True}
                if rc != 0:
                    return {"ok": False, "error": f"Ollama unpack (zstd) failed:\n{errout[-500:]}"}
                target.mkdir(parents=True, exist_ok=True)
                rc, out, errout, cancelled = _run_subprocess_cancellable(
                    ["tar", "-xf", str(tar_path), "-C", str(target)], timeout=60 * 10,
                )
            else:
                # darwin .tgz: the bare `ollama` binary at the tar root → land
                # it in bin/ ourselves so ensure_backend's path works.
                (target / "bin").mkdir(parents=True, exist_ok=True)
                rc, out, errout, cancelled = _run_subprocess_cancellable(
                    ["tar", "-xzf", str(archive), "-C", str(target / "bin")], timeout=60 * 10,
                )
            if cancelled or cancellation.is_cancelled():
                return {"ok": False, "error": "Cancelled by user", "cancelled": True}
            if rc != 0:
                return {"ok": False, "error": f"Ollama unpack (tar) failed:\n{errout[-500:]}"}
        if not ollama_bin.is_file():
            return {"ok": False,
                    "error": f"Ollama unpacked but {ollama_bin} is missing (layout change?)"}
        ollama_bin.chmod(0o755)
    else:
        _step(40, "Ollama already installed, reusing")

    # ---- 3. server + model pull
    _step(50, f"[3/4] Starting Ollama + pulling {model} (one-time)...")
    from vibechek.genre_web import ensure_backend  # noqa: PLC0415

    if not ensure_backend():
        return {"ok": False,
                "error": "Ollama installed but the server did not come up "
                         "(see ~/.vibechek/ollama.log)"}
    rc, tail = _run_with_progress(
        [str(ollama_bin), "pull", model],
        on_progress=lambda line: _step(70, line[:120]),
        timeout=60 * 120,  # multi-GB model on arbitrary connections
        env={**os.environ, "OLLAMA_HOST": "127.0.0.1:11434"},
    )
    if cancellation.is_cancelled():
        return {"ok": False, "error": "Cancelled by user", "cancelled": True}
    if rc != 0:
        return _fail(f"ollama pull {model}", rc, tail)

    # ---- 4. verify
    _step(95, "[4/4] Verifying...")
    if not ensure_backend():
        return {"ok": False, "error": "Ollama server unreachable after setup"}
    _step(100, "Online genre resolver ready")
    return {"ok": True, "tail": "\n".join(tail[-10:])}


__all__ = [
    "IS_SUPPORTED",
    "VENV_DIR",
    "NativeVenvStatus",
    "to_dict",
    "probe_native_venv",
    "install_essentia_native",
    "run_vibechek_in_native_venv",
    "setup_clap_native",
    "setup_resolver_native",
]
