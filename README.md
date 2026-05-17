# Vibechek

**ML-powered DJ library organizer.** Analyze, tag, and organize thousands of tracks automatically — without touching the cue points and beat grids your DJ software depends on.

> Status: 🚧 **Early development.** Python package + CLI work end-to-end; standalone binaries build on Windows/macOS/Linux. The underlying pipeline has already analyzed and organized a 12,000+ track personal library.

---

## What it does

Point Vibechek at a folder of music. It will:

- **Detect** genre, subgenre, BPM, key, energy, mood, timeslot (opener / warm-up / peak / afterhours), direction (up / steady / down), and vocal content using Essentia's pre-trained ML models.
- **Tag** your files with this info — with a confidence threshold you control — while preserving Rekordbox cue points, beat grids, and other binary metadata.
- **Find duplicates** via MD5 hash + Chromaprint audio fingerprinting (catches re-encoded copies, not just byte-identical files).
- **Organize** your library into genre / subgenre folders, with rules you control (e.g. "consolidate genres with fewer than N tracks into Other/").
- **Back up everything first.** No destructive operations without a restorable backup.

## Why

Existing tools (Mixed In Key, Platinum Notes, Lexicon DJ) are great but closed-source, paid, and opinionated about how your library should look. Vibechek is built around the belief that **you own your library**, and you should be able to tune every threshold, every folder rule, every tag-write decision.

## Project goals

1. **Just works.** One-click install on Windows, macOS, and Linux. No command line required.
2. **Granular controls.** Every flag in the underlying pipeline is exposed in the UI.
3. **Safe by default.** Backup before every write. Never touch Rekordbox binary frames.
4. **Free and open source.** AGPL-3.0. Donate via [GitHub Sponsors / Ko-fi — coming soon] if it saves you time.

## Status & roadmap

See [`docs/ROADMAP.md`](docs/ROADMAP.md) for the phased plan.

| Phase | What | Status |
|---|---|---|
| 1 | Refactor working Python scripts into a `vibechek` package + CLI | ✅ Done |
| 2 | Cross-platform installer (PyInstaller + platform installers) | ✅ Done |
| 3 | Desktop UI (Tauri + Python sidecar, reusing prototype design) | 🚧 In progress |
| 4 | Polish, docs, community launch | ⏳ Planned |

## What's in the repo

- [`vibechek/`](vibechek/) — the Python package: analyzer, tagger, duplicates, organizer, CLI, JSON-RPC sidecar.
- [`tests/`](tests/) — 67+ pytest cases covering pure logic and end-to-end flows.
- [`ui/`](ui/) — Tauri 2.x desktop shell + React frontend (see [ui/README.md](ui/README.md)).
- [`packaging/`](packaging/) — PyInstaller spec, build scripts for each OS, Inno Setup config for the Windows installer.
- [`.github/workflows/`](.github/workflows/) — CI (test on every push) + release (build artifacts on tag).
- [`legacy/`](legacy/) — the original v1 scripts that already processed a 12k-track library. Kept as the historical source of truth.
- [`docs/`](docs/) — project summary, prototype design notes, full roadmap.

## Install

### Download a release (recommended)

Grab the latest build for your platform from [Releases](https://github.com/papapew/Vibechek/releases) and extract:

- **Windows**: `vibechek-windows-x64.zip` → unzip, run `vibechek-setup.exe` (or use `vibechek.exe` directly).
- **macOS**: `vibechek-macos-arm64.tar.gz` → `tar -xzf` and add to your PATH.
- **Linux**: `vibechek-linux-x64.tar.gz` → `tar -xzf` and add to your PATH.

Then verify:

```
vibechek --help
```

For ML analysis (genre/mood/energy detection), install Essentia separately — it's too heavy to bundle:

```
pip install essentia-tensorflow
```

Linux & macOS only. Windows users: see `docs/INSTALL.md` for the workaround until Essentia ships a Windows wheel.

### From source (developers)

```bash
git clone https://github.com/papapew/Vibechek.git
cd Vibechek

python -m venv .venv
. .venv/Scripts/activate   # Windows
# source .venv/bin/activate  # macOS/Linux

pip install -e ".[dev]"
pytest
vibechek --help
```

### Build a binary yourself

```bash
# Windows
packaging\build-windows.bat

# macOS / Linux
./packaging/build-macos.sh
./packaging/build-linux.sh
```

Output lands in `dist/vibechek/`.

## Commands

```
vibechek analyze <path>            # Run ML analysis on every track
vibechek dedupe  <path>            # Find duplicates (MD5 + audio fingerprint)
vibechek organize <analysis.json>  # Move tracks into genre/subgenre folders
vibechek tag <analysis.json>       # Write ML tags to files (preserves Rekordbox data)
vibechek backup-tags <path>        # Snapshot all tags to a JSON file
vibechek restore-tags <backup>     # Restore from a snapshot
vibechek route <staging> <library> # Copy new tracks into matching genre folders
vibechek download-models           # Pre-download ML models (~800 MB)
```

Every command supports `--help` for its flags. Destructive commands (`organize`, `tag`) support `--dry-run`.

## Acknowledgements

- [Essentia](https://essentia.upf.edu/) — the ML magic that makes everything else possible.
- [Chromaprint](https://acoustid.org/chromaprint) — audio fingerprinting.
- [TagLib](https://taglib.org/) / [Mutagen](https://mutagen.readthedocs.io/) — audio metadata handling.

## License

AGPL-3.0-or-later. See [LICENSE](LICENSE).
