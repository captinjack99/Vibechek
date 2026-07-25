# Installing Vibechek

Two flavors:

- **Desktop app** (recommended for most users) — point-and-click. Handles its own dependencies via the in-app setup dialog. Download the installer for your OS from [Releases](https://github.com/captinjack99/Vibechek/releases). Windows ships a `.exe` (NSIS) installer, Linux a `.deb`/`.AppImage`, macOS a `.dmg` (Apple Silicon).
- **CLI** (`vibechek` command) — for scripting, headless use, or running on a server.

> **macOS first launch (beta builds are unsigned).** The `.dmg` isn't notarized yet,
> so macOS shows *"Vibechek is damaged / can't be verified."* It isn't — just unsigned.
> Right-click **Vibechek.app → Open → Open**, or run
> `xattr -dr com.apple.quarantine /Applications/Vibechek.app` once. Signed + notarized
> builds land with the stable release.

> **In-app updates.** The desktop app can check for, download, and install updates from
> **Settings → Software updates** once update signing is configured. Beta builds ship
> unsigned, so the feature is inert on them — grab new betas from the
> [Releases](https://github.com/captinjack99/Vibechek/releases) page until signing is enabled
> for the stable release.

See [USER_GUIDE.md](USER_GUIDE.md) for an end-to-end walkthrough of the desktop app.

## What you need depends on what you're doing

| Goal | What to install |
|---|---|
| Dedupe, organize, backup, restore, route | **Just Vibechek.** No ML needed. |
| Run `vibechek analyze` (ML genre/mood/energy detection) | Vibechek + Essentia (see below). On Windows the desktop app bundles a native, WSL-free engine and needs no setup; the CLI / fallback engines use WSL Essentia. |

## Installing Vibechek

### Recommended: pre-built release

1. Go to [Releases](https://github.com/captinjack99/Vibechek/releases).
2. Download the archive for your OS.
3. Extract somewhere stable (e.g. `C:\Program Files\Vibechek\` or `~/.local/bin/vibechek/`).
4. Add the extracted folder to your `PATH`. The Windows CLI ships as `vibechek-windows-x64.zip` — there's no separate setup program, so add the extracted folder to your user `PATH` by hand (System Properties → Environment Variables). *(The point-and-click desktop app is a separate NSIS `.exe` — see the two flavors above.)*
5. Verify: `vibechek --version`.

### Alternative: pip install from source

```bash
git clone https://github.com/captinjack99/Vibechek.git
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

The venv is hermetic. Vibechek does *not* touch your system Python or
whatever venv you might be developing in. Delete `~/.vibechek/` to wipe the
managed install entirely.

If you'd rather do it by hand (CLI use, server setup, custom Python
environment), the equivalents are:

```bash
pip install essentia-tensorflow
vibechek download-models     # one-time, ~800 MB
```

### Windows

**Since v0.6.3-beta the desktop installer bundles a native, WSL-free analysis
engine and defaults to it.** A fresh install of the desktop app analyzes fully
in-process — no WSL, no separate Python, nothing to set up. Click **Analyze** and
it runs immediately. The native engine is a DSP-only native essentia wheel
(decode/BPM/key) + a pure-NumPy mel frontend + ONNX Runtime inference, all
in-process; it ships CPU-only (see the GPU section for the ONNX-engine GPU path).

Essentia itself has no Windows wheel, so the fallback path below is used when the
native bundle isn't present — the lean CLI `.zip`, a `pip install`, or when you
switch to the **essentia-tensorflow** or **ONNX** engines (or the opt-in CLAP /
online-lookup genre engines, which still run through WSL on Windows). On a
fallback path the desktop app sets everything up for you; the first time you click
**Analyze** there, a setup dialog walks you through it — no terminal required:

1. **Install WSL + Ubuntu** — one click, triggers a UAC prompt, ~5-15 min
   download (Windows handles the install itself).
2. **Install Vibechek + Essentia inside Ubuntu** — one click, ~3-5 min,
   runs entirely inside WSL with no extra prompts.
3. **Download ML models** — one click, ~800 MB.
4. **(Optional) Enable GPU acceleration** — one click in **Settings → System**.
   See the GPU section below. Vibechek can also run **hybrid CPU+GPU**, using your
   GPU and spare CPU cores at the same time.

On a WSL fallback, when you analyze a library on `C:\Music\Tracks` the app
automatically routes that analyze through WSL, translates the path to
`/mnt/c/Music/Tracks`, runs essentia inside the Linux environment, and
returns Windows paths in the resulting `analysis.json`. You never see WSL.

If you'd rather set up the WSL fallback by hand (e.g. running on a server), the
manual equivalents are:

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
analysis by ~3-10×. Useful if you have a 5,000+ track library. The runtime
libraries TF needs (`libcublas`, `libcufft`, `libcudnn`, `libcusparse`) are
**not** installed by default on WSL Ubuntu — and a WSL reinstall or Ubuntu
upgrade wipes them again even if they were present before.

**Since v0.8.0-beta this self-heals.** Before dispatching an analyze on the
essentia-tensorflow engine, Vibechek verifies the WSL venv can actually
import its ML stack and checks whether an NVIDIA GPU is visible but the CUDA
libs are missing; if so it restores them automatically — a "Restoring GPU
libraries…" progress line appears (a one-time multi-GB download, since the
cuDNN/cuBLAS wheels are large) and analyze continues once it's done. You don't
need to click anything. If the restore
fails for some reason, analyze still proceeds on CPU with the reason
recorded. Set `VIBECHEK_NO_AUTOHEAL=1` to disable automatic repairs (Vibechek
still detects and reports the problem honestly; repairing then requires the
manual button below).

The **Enable GPU (install CUDA wheels)** button in **Settings → System** is
now an accelerator rather than the only path — use it to trigger the same
repair on demand instead of waiting for the next analyze:

1. Open **Settings → System**.
2. If you have an NVIDIA GPU but the engine probe shows:
   *"…is visible to WSL, but TensorFlow can't use it — required CUDA
   libraries are missing"*, click **Enable GPU (install CUDA wheels)**.
3. The installer downloads NVIDIA's CUDA runtime wheels from PyPI
   (`nvidia-cublas-cu11`, `nvidia-cudnn-cu11`, `nvidia-cufft-cu11`,
   `nvidia-cusparse-cu11`, and related cu11 packages) into the managed venv —
   roughly 1-2 GB total (cuDNN alone is large), a few minutes on a normal
   connection.
4. The probe automatically re-runs; the row turns green and shows your card
   name (e.g. "NVIDIA GeForce RTX 4070 Laptop GPU").

The pip-wheel approach works on **every Linux distribution**: Ubuntu 20.04,
22.04, 24.04, Debian, anything WSL can run. No apt repo configuration, no
NVIDIA keyring, no root required. The wheels ship the `.so` files directly,
Vibechek generates `~/.vibechek/cuda-env.sh` to expose them to TF, and the
venv's `vibechek` shim is patched to source it on launch.

Vibechek will not lie to you about the GPU. If your card is visible to the
host but Essentia can't actually use it (driver / lib version mismatch), the
UI says so plainly and tells you what's wrong. Readiness checks actually run
the venv's Python interpreter rather than just checking that the venv folder
exists, so a venv left behind by a removed/upgraded host Python is reported
as broken (and repaired) instead of falsely showing READY. CPU mode is fast
enough on a modern multi-core system — typical throughput is ~25-40
tracks/min with all cores in use.

**Doing it by hand:**

```bash
wsl -d Ubuntu -- bash -lc '
  ~/.vibechek/venv/bin/pip install \
    nvidia-cublas-cu11 nvidia-cudnn-cu11 \
    nvidia-cufft-cu11 nvidia-cusparse-cu11'
```

Then set `LD_LIBRARY_PATH` to include the resulting `~/.vibechek/venv/lib/python3.*/site-packages/nvidia/*/lib` directories, or just re-run the GUI "Enable GPU" button and it'll regenerate `cuda-env.sh` for you.

> **Non-NVIDIA GPUs via ONNX.** The default analysis engine
> (essentia-tensorflow) only accelerates on NVIDIA/CUDA. The opt-in
> **ONNX Runtime** engine (Settings → Analysis → Inference engine) runs the same
> models with cross-vendor GPU support — NVIDIA **CUDA**, AMD **ROCm**, Apple
> **CoreML** — on **plain Essentia with no TensorFlow**. Validated to match the
> default engine (CUDA verified on an RTX 4070; ROCm/CoreML wired but
> hardware-unverified). Turn it on in Settings → **Set up ONNX engine**: it
> provisions a separate `~/.vibechek/venv-onnx` (a second WSL venv on Windows) and
> auto-picks the GPU runtime for your hardware — the in-app installer pins
> `onnxruntime-gpu` to the CUDA-12 line (`<1.27`; an unpinned install used to
> resolve to a CUDA-13-only build that crashed the ONNX engine on import with
> a missing `libcudart.so.13`) alongside the matching `nvidia-*-cu12` runtime
> wheels. For a manual CLI install: `pip install vibechek[onnx]` (CPU) or
> `pip install vibechek[onnx-gpu]` (NVIDIA/CUDA — this extra is unpinned, so
> pin `onnxruntime-gpu<1.27` yourself if you hit the CUDA-13 crash); on AMD
> Linux the setup uses `onnxruntime-rocm` and on macOS the CoreML provider
> ships in the base `onnxruntime` wheel. Then
> `vibechek download-models --engine onnx`.
> See [docs/ONNX_MIGRATION.md](ONNX_MIGRATION.md).
>
> *(Note: on Windows there is no DirectML/Intel path. The default native engine
> runs in-process but CPU-only; the ONNX engine runs in WSL — Essentia has no
> Windows wheel — where the GPU providers are CUDA / ROCm / CoreML.)*

### Skipping analyze entirely

Everything else works on native Windows without Essentia: dedupe, organize,
tag (from an existing `analysis.json`), backup, restore, route. If you have
an analysis from a friend or a previous run, the rest of Vibechek runs
straight from the standalone installer.

## Installing soundfile (for `cdj-export`'s FLAC→AIFF transcode)

`vibechek cdj-export` transcodes a FLAC library to AIFF so it plays on old CDJs (see
the [user guide](USER_GUIDE.md#workflow-6-play-your-flac-library-on-old-cdjs)). It needs
one of two things to read/write audio:

- The optional `[cdj]` extra, which pulls in [`soundfile`](https://pypi.org/project/soundfile/):

  ```bash
  pip install "vibechek[cdj]"
  ```

- **or** [`ffmpeg`](https://ffmpeg.org/) on your `PATH` (used as a fallback).

You only need this for `cdj-export`; nothing else in Vibechek depends on it.

## The opt-in genre engines (CLAP audio model / online lookup)

Genre has two optional accuracy upgrades over the bundled Discogs-EffNet head. Both are
**opt-in** (the default install ships neither) and both have a one-click **Set up …** button
in **Settings → Analysis** that does everything below for you — only reach for the CLI if
you're installing into your own environment.

- **CLAP audio classifier** (`genre_classifier = "clap"`) — a pure-audio model, ~2× the
  Discogs head's accuracy, that also works on untagged tracks. The in-app setup installs
  `vibechek[clap]` (`laion-clap` + `torch`) into the analysis venv and downloads the
  ~2.2 GB CLAP checkpoint once (into `~/.vibechek/clap/`). Manual: `pip install
  "vibechek[clap]"` then click **Set up CLAP genre engine** (or run the analyze with
  `--genre-classifier clap`). The small kNN reference library is bundled in the app.
- **Online genre lookup** (`genre_web_lookup`) — searches for the track's artist + title,
  fetches the catalog pages the search returned, and reads the genre straight off their
  structured genre field, keeping it only when that page names this exact track. No model
  and no API key: the in-app setup installs `vibechek[resolver]` (`ddgs` for the keyless
  search, `beautifulsoup4` for HTML → text) and nothing else. Manual: `pip install
  "vibechek[resolver]"` then click **Set up online lookup** (or analyze with
  `--genre-web-lookup`). `--genre-llm-backend` is accepted but has no effect — the earlier
  local-LLM version of this tier was measured worse than the direct read and was removed.

Both run inside the same analysis environment as Essentia (WSL on Windows, the managed
`~/.vibechek` venv on Linux/macOS), so one worker does BPM/key/mood *and* the new genre
source. They are **not** supported with the Windows-default native engine — the backend
refuses the setup with a clear error; switch to the ONNX or essentia-tensorflow engine
(both run in WSL on Windows) to enable them. See the
[user guide](USER_GUIDE.md#genre-classifier--online-lookup).

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

**"vibechek: command not found"** — the install folder isn't on your PATH. Add the folder manually:
- Windows: System Properties → Environment Variables → User Path
- macOS / Linux: add `export PATH="$PATH:/path/to/vibechek"` to your shell rc

**"essentia-tensorflow not installed"** when running `vibechek analyze` — expected on the Windows CLI zip or a fallback (essentia-tensorflow / ONNX) engine; the desktop app's bundled native engine needs no Essentia. See "Installing Essentia" above.

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
