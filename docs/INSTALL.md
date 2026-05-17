# Installing Vibechek

## What you need depends on what you're doing

| Goal | What to install |
|---|---|
| Dedupe, organize, backup, restore, route | **Just Vibechek.** No ML needed. |
| Run `vibechek analyze` (ML genre/mood/energy detection) | Vibechek + Essentia (see below) |

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

### Windows (harder)

Essentia does not publish official Windows wheels. Two workarounds:

#### Option A: use WSL (recommended)

Run Vibechek inside WSL Ubuntu and point it at your music drive mounted under `/mnt/`. This is the same setup that processed the original 12k-track library — see [`docs/PROJECT_SUMMARY.md`](PROJECT_SUMMARY.md) for context.

```powershell
wsl --install -d Ubuntu-24.04
```

Then inside the WSL shell:

```bash
sudo apt update && sudo apt install -y python3-pip libchromaprint-tools
pip install essentia-tensorflow vibechek
vibechek analyze "/mnt/d/Music/Tracks"
```

#### Option B: skip analyze, use Vibechek for the rest

Everything except `analyze` works on native Windows: dedupe, organize, tag (from an existing analysis.json), backup/restore, route. If you have an analysis.json from a friend or from a previous run, you can use the rest of Vibechek normally.

#### Option C: build Essentia from source

Possible but painful — requires Visual Studio, vcpkg, and patience. Not officially supported; consider Option A first.

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
