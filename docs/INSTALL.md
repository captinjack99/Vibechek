# Installing Vibechek

Two flavors:

- **Desktop app** (recommended for most users) — point-and-click. Handles its own dependencies via the in-app setup dialog. Download the installer for your OS from [Releases](https://github.com/papapew/Vibechek/releases).
- **CLI** (`vibechek` command) — for scripting, headless use, or running on a server.

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

### Linux & macOS (easy)

```
pip install essentia-tensorflow
```

That's it. Then run `vibechek download-models` once to fetch the model weights (~800 MB to a per-user data directory).

### Windows (now fully automated)

Essentia doesn't publish a Windows wheel, but the Vibechek desktop app handles
that for you. The first time you click **Analyze**, a setup dialog walks you
through the whole thing — no terminal required:

1. **Install WSL + Ubuntu** — one click, triggers a UAC prompt, ~5-15 min
   download (Windows handles the install itself).
2. **Install Vibechek + Essentia inside Ubuntu** — one click, ~3-5 min,
   runs entirely inside WSL with no extra prompts.
3. **Download ML models** — one click, ~200 MB.

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
| `<config_dir>/Vibechek/config.toml` | Your settings (auto-saved 500ms after change) | `%APPDATA%\Vibechek\Vibechek\config.toml` |
| `<config_dir>/Vibechek/library_state.json` | Recent libraries index | `%APPDATA%\Vibechek\Vibechek\library_state.json` |
| `<config_dir>/Vibechek/backup_history.json` | Past tag backups | `%APPDATA%\Vibechek\Vibechek\backup_history.json` |
| `<data_dir>/Vibechek/models/` | Downloaded ML model `.pb` files (~800 MB total) | `%LOCALAPPDATA%\Vibechek\Vibechek\models\` |
| `<data_dir>/Vibechek/analyses/` | Auto-saved per-library analysis JSONs | `%LOCALAPPDATA%\Vibechek\Vibechek\analyses\` |
| `<data_dir>/Vibechek/logs/vibechek.log` | Rotating log file (10 MB × 5 backups) | `%LOCALAPPDATA%\Vibechek\Vibechek\logs\vibechek.log` |

`<config_dir>` and `<data_dir>` come from [platformdirs](https://platformdirs.readthedocs.io/) and are correct per OS.

To start fresh: delete the `Vibechek` folder under your config and data dirs.
