# Installing Vibechek

Two flavors:

- **Desktop app** (recommended for most users) — point-and-click. Handles its own dependencies via the in-app setup dialog. Download the installer for your OS from [Releases](https://github.com/papapew/Vibechek/releases). Windows ships a `.exe` (NSIS) installer, Linux a `.deb`/`.AppImage`, macOS a `.dmg` (Apple Silicon).
- **CLI** (`vibechek` command) — for scripting, headless use, or running on a server.

> **macOS first launch (beta builds are unsigned).** The `.dmg` isn't notarized yet,
> so macOS shows *"Vibechek is damaged / can't be verified."* It isn't — just unsigned.
> Right-click **Vibechek.app → Open → Open**, or run
> `xattr -dr com.apple.quarantine /Applications/Vibechek.app` once. Signed + notarized
> builds land with the stable release.

See [USER_GUIDE.md](USER_GUIDE.md) for an end-to-end walkthrough of the desktop app.

## What you need depends on what you're doing

| Goal | What to install |
|---|---|
| Dedupe, organize, backup, restore, route | **Just Vibechek.** No ML needed. |
| Run `vibechek analyze` (ML genre/mood/energy detection) | Vibechek + Essentia (see below). On Windows the desktop app installs Essentia for you. |

## Installing Vibechek

### Recommended: pre-built release

1. Go to [Releases](https://github.com/papapew/Vibechek/releases).
2. Download the archive for your OS.
3. Extract somewhere stable (e.g. `C:\Program Files\Vibechek\` or `~/.local/bin/vibechek/`).
4. Add the extracted folder to your `PATH` — or, on Windows, run the included `vibechek-setup.exe` and check the "Add to PATH" box.
5. Verify: `vibechek --version`.

### Alternative: pip install from source

```bash
git clone https://github.com/papapew/Vibechek.git
cd Vibechek
pip install -e .
```

## Installing Essentia (for `analyze`)

Essentia is the ML library that detects genre, mood, energy, etc. It's not bundled with the Vibechek installer because the TensorFlow runtime it depends on adds 500+ MB and behaves differently on each OS.

### Linux & macOS (handled by the desktop app)

The desktop app handles Essentia install for you, the same way it does on
Windows — no terminal needed. The first time you click **Analyze**, a setup
dialog appears with a one-click **Install Essentia** button. It creates a
managed venv at `~/.vibechek/venv/`, installs `essentia-tensorflow` +
`vibechek` into it, and routes analysis through that venv transparently.

The venv is hermetic — Vibechek does *not* touch your system Python or
whatever venv you might be developing in. Delete `~/.vibechek/` to wipe the
managed install entirely.

If you'd rather do it by hand (CLI use, server setup, custom Python
environment), the equivalents are:

```bash
pip install essentia-tensorflow
vibechek download-models     # one-time, ~800 MB
```

### Windows (now fully automated)

Essentia doesn't publish a Windows wheel, but the Vibechek desktop app handles
that for you. The first time you click **Analyze**, a setup dialog walks you
through the whole thing — no terminal required:

1. **Install WSL + Ubuntu** — one click, triggers a UAC prompt, ~5-15 min
   download (Windows handles the install itself).
2. **Install Vibechek + Essentia inside Ubuntu** — one click, ~3-5 min,
   runs entirely inside WSL with no extra prompts.
3. **Download ML models** — one click, ~800 MB.
4. **(Optional) Enable GPU acceleration** — one click in **Settings → System**.
   See the GPU section below. Vibechek can also run **hybrid CPU+GPU**, using your
   GPU and spare CPU cores at the same time.

After setup, when you analyze a library on `C:\Music\Tracks` the app
automatically routes that analyze through WSL, translates the path to
`/mnt/c/Music/Tracks`, runs essentia inside the Linux environment, and
returns Windows paths in the resulting `analysis.json`. You never see WSL.

If you'd rather do it by hand (e.g. running on a server), the manual
equivalents are:

```powershell
wsl --install -d Ubuntu-24.04
# After Windows finishes the install + you've launched Ubuntu once:
wsl -d Ubuntu-24.04 -- bash -lc '
  sudo apt update &&
  sudo apt install -y python3-pip libchromaprint-tools &&
  pip install essentia-tensorflow vibechek &&
  vibechek --version'
```

### Enabling GPU acceleration (optional, NVIDIA only)

Essentia's bundled TensorFlow (2.5) can use an NVIDIA GPU to speed up
analysis by ~3-10× — useful if you have a 5 000+ track library. The runtime
libraries TF needs (`libcublas`, `libcufft`, `libcudnn`, `libcusparse`) are
**not** installed by default on WSL Ubuntu. Vibechek detects this exact
state and exposes a one-click fix:

1. Open **Settings → System**.
2. If you have an NVIDIA GPU but the engine probe shows:
   *"…is visible to WSL, but TensorFlow can&apos;t use it — required CUDA
   libraries are missing"*, click **Enable GPU (install CUDA wheels)**.
3. The installer downloads NVIDIA's CUDA runtime wheels from PyPI
   (`nvidia-cublas-cu11`, `nvidia-cudnn-cu11`, `nvidia-cufft-cu11`,
   `nvidia-cusparse-cu11`) into the managed venv — about 200 MB total,
   ~30 seconds on a normal connection.
4. The probe automatically re-runs; the row turns green and shows your card
   name (e.g. "NVIDIA GeForce RTX 4070 Laptop GPU").

The pip-wheel approach works on **every Linux distribution**: Ubuntu 20.04,
22.04, 24.04, Debian, anything WSL can run. No apt repo configuration, no
NVIDIA keyring, no root required. The wheels ship the `.so` files directly,
Vibechek generates `~/.vibechek/cuda-env.sh` to expose them to TF, and the
venv's `vibechek` shim is patched to source it on launch.

Vibechek will not lie to you about the GPU. If your card is visible to the
host but Essentia can't actually use it (driver / lib version mismatch), the
UI says so plainly and tells you what's wrong. CPU mode is fast enough on a
modern multi-core system — typical throughput is ~25-40 tracks/min with all
cores in use.

**Doing it by hand:**

```bash
wsl -d Ubuntu -- bash -lc '
  ~/.vibechek/venv/bin/pip install \
    nvidia-cublas-cu11 nvidia-cudnn-cu11 \
    nvidia-cufft-cu11 nvidia-cusparse-cu11'
```

Then set `LD_LIBRARY_PATH` to include the resulting `~/.vibechek/venv/lib/python3.*/site-packages/nvidia/*/lib` directories, or just re-run the GUI "Enable GPU" button and it'll regenerate `cuda-env.sh` for you.

### Skipping analyze entirely

Everything else works on native Windows without Essentia: dedupe, organize,
tag (from an existing `analysis.json`), backup, restore, route. If you have
an analysis from a friend or a previous run, the rest of Vibechek runs
straight from the standalone installer.

## Installing fpcalc (for `dedupe`'s audio fingerprinting)

`fpcalc` is the [Chromaprint](https://acoustid.org/chromaprint) command-line tool. Vibechek's `dedupe` works without it (MD5-only mode catches exact-byte duplicates), but with `fpcalc` it also catches re-encoded duplicates.

| OS | Install |
|---|---|
| Linux | `sudo apt install libchromaprint-tools` |
| macOS | `brew install chromaprint` |
| Windows | Download from <https://acoustid.org/chromaprint> and put `fpcalc.exe` on your PATH |

## Verifying your install

```bash
vibechek --version
vibechek dedupe . --no-chromaprint -o /tmp/test.json   # no-op test
```

If `analyze` is your goal, also:

```bash
python -c "import essentia; print('essentia', essentia.__version__)"
vibechek download-models   # one-time, ~800 MB
```

## Troubleshooting

**"vibechek: command not found"** — the install folder isn't on your PATH. Re-run the Windows installer with "Add to PATH" checked, or add the folder manually:
- Windows: System Properties → Environment Variables → User Path
- macOS / Linux: add `export PATH="$PATH:/path/to/vibechek"` to your shell rc

**"essentia-tensorflow not installed"** when running `vibechek analyze` — that's expected on Windows. See "Installing Essentia" above.

**Models download is slow / fails** — `essentia.upf.edu` occasionally rate-limits. Retry, or download the `.pb` files manually from [the model index](https://essentia.upf.edu/models.html) into the directory `vibechek download-models` prints on startup.

**"fpcalc not found"** — install Chromaprint (see above) or use `--no-chromaprint` to fall back to MD5-only duplicate detection.

## Where Vibechek stores things

| File / dir | Purpose | Example (Windows) |
|---|---|---|
| `<config_dir>/Vibechek/config.json` | Your settings (auto-saved 500ms after change) | `%APPDATA%\Vibechek\Vibechek\config.json` |
| `<config_dir>/Vibechek/library_state.json` | Recent libraries index | `%APPDATA%\Vibechek\Vibechek\library_state.json` |
| `<config_dir>/Vibechek/backup_history.json` | Past tag backups | `%APPDATA%\Vibechek\Vibechek\backup_history.json` |
| `<data_dir>/Vibechek/models/` | Downloaded ML model `.pb` files (~800 MB total) | `%LOCALAPPDATA%\Vibechek\Vibechek\models\` |
| `<data_dir>/Vibechek/analyses/` | Auto-saved per-library analysis JSONs | `%LOCALAPPDATA%\Vibechek\Vibechek\analyses\` |
| `<data_dir>/Vibechek/logs/vibechek.log` | Rotating log file (10 MB × 5 backups) | `%LOCALAPPDATA%\Vibechek\Vibechek\logs\vibechek.log` |

`<config_dir>` and `<data_dir>` come from [platformdirs](https://platformdirs.readthedocs.io/) and are correct per OS.

To start fresh: delete the `Vibechek` folder under your config and data dirs.
