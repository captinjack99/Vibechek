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
import logging
import re
import shutil
import subprocess
import sys
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
    cost once. The script writes plain `vibechek=...` / `essentia=...` lines
    for unambiguous parsing.
    """
    script = (
        "echo vibechek=$(which vibechek 2>/dev/null);"
        "echo essentia=$(python3 -c 'import essentia; print(essentia.__version__)' 2>/dev/null)"
    )
    try:
        result = _wsl_run(
            [wsl_exe, "-d", distro.name, "--", "bash", "-lc", script],
            timeout=20,  # distro boot + two quick probes
        )
    except Exception as e:  # noqa: BLE001
        log.debug("probe %s failed: %s", distro.name, e)
        return

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


# Script that runs inside the distro. Idempotent; safe to re-run.
_BOOTSTRAP_SCRIPT = r"""
set -e
echo "[1/4] Updating apt..."
sudo -n apt-get update -y || { echo "Need passwordless sudo for apt. Edit /etc/sudoers." ; exit 2; }

echo "[2/4] Installing system deps..."
sudo -n apt-get install -y --no-install-recommends \
    python3 python3-pip python3-venv \
    libchromaprint-tools \
    git ca-certificates

echo "[3/4] Creating ~/.vibechek venv..."
mkdir -p "$HOME/.vibechek"
if [ ! -d "$HOME/.vibechek/venv" ]; then
    python3 -m venv "$HOME/.vibechek/venv"
fi

echo "[4/4] Installing Python packages (this is the slow part)..."
"$HOME/.vibechek/venv/bin/pip" install --upgrade --quiet pip wheel
"$HOME/.vibechek/venv/bin/pip" install --quiet essentia-tensorflow
"$HOME/.vibechek/venv/bin/pip" install --quiet git+https://github.com/papapew/Vibechek.git

# Symlink the CLI into ~/.local/bin so it's on PATH
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

    Streams the bootstrap script's stdout to `on_progress` line-by-line so
    the GUI can show what step is running.
    """
    if not IS_WINDOWS:
        return {"ok": False, "error": "Not running on Windows"}

    wsl = shutil.which("wsl") or shutil.which("wsl.exe")
    if not wsl:
        return {"ok": False, "error": "wsl.exe not found"}

    if on_progress:
        on_progress(0, 100, f"Starting install inside {distro}...")

    # Pipe the bootstrap script in via stdin so we don't have to deal with
    # quoting it on the command line.
    try:
        proc = subprocess.Popen(
            [wsl, "-d", distro, "--", "bash", "-s"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,  # merge so we see everything in order
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as e:
        return {"ok": False, "error": f"Could not invoke wsl: {e}"}

    assert proc.stdin and proc.stdout
    proc.stdin.write(_BOOTSTRAP_SCRIPT)
    proc.stdin.close()

    # Approximate progress: there are 4 numbered steps in the script.
    step_pct = {"[1/4]": 10, "[2/4]": 25, "[3/4]": 40, "[4/4]": 60, "DONE": 95}
    tail: list[str] = []
    for line in proc.stdout:
        tail.append(line.rstrip())
        if len(tail) > 30:
            tail.pop(0)
        for marker, pct in step_pct.items():
            if line.startswith(marker):
                if on_progress:
                    on_progress(pct, 100, line.strip())
                break

    rc = proc.wait(timeout=60 * 30)
    if rc != 0:
        return {
            "ok": False,
            "error": f"Install script exited with {rc}",
            "tail": "\n".join(tail),
        }

    if on_progress:
        on_progress(100, 100, "Install complete")

    return {"ok": True, "distro": distro, "tail": "\n".join(tail)}


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
    """
    wsl = shutil.which("wsl") or shutil.which("wsl.exe")
    if not wsl:
        raise FileNotFoundError("wsl.exe not on PATH")

    # Use the bash login shell so the user's PATH (incl. ~/.local/bin) is set up
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

    stdout_chunks: list[str] = []

    if on_stderr_line and proc.stderr is not None:
        import threading

        def _reader() -> None:
            for line in proc.stderr:  # type: ignore[union-attr]
                on_stderr_line(line.rstrip())

        t = threading.Thread(target=_reader, daemon=True)
        t.start()

    if proc.stdout is not None:
        for line in proc.stdout:
            stdout_chunks.append(line)

    rc = proc.wait(timeout=timeout)
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
    "detect_wsl",
    "install_wsl",
    "install_vibechek_in_wsl",
    "run_vibechek_in_wsl",
    "win_to_wsl_path",
    "wsl_to_win_path",
    "to_dict",
]
