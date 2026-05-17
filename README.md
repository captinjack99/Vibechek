# Vibechek

**ML-powered DJ library organizer.** Analyze, tag, deduplicate, and organize thousands of tracks — automatically, with safety nets at every step. Never touches the cue points and beat grids your DJ software depends on.

> Status: **0.3-dev**, approaching `v1.0` readiness. The Python core has analyzed and organized a 12,000+ track personal library successfully. The desktop app is feature-complete; needs final polish + a tagged release.

---

## Why

Mixed In Key, Platinum Notes, and Lexicon DJ are closed-source, paid, and opinionated. Vibechek is built around the belief that **you own your library** and should be able to tune every threshold, every folder rule, every tag-write decision — without paying for it.

- 100% local — nothing uploaded, no account, no telemetry
- AGPL-3.0 forever
- Cross-platform: Windows / macOS / Linux

## What it does

| | |
|---|---|
| **Browse** | Open a folder of music, get an instant view of every track plus its tags |
| **Analyze** | ML detects genre, subgenre, BPM, key, energy (0-5), mood (Dark/Neutral/Bright), timeslot (Opener/Warm-Up/Peak/Afterhours), direction, and vocal type |
| **Deduplicate** | MD5 (byte-identical) + Chromaprint (acoustically identical) — with rule-based auto-keepers (codec > bitrate > size > newest > shortest path) |
| **Organize** | Move tracks into clean `Genre/Subgenre/` folders, with rules you control (rare genres → `Other/`, optional subfolders, target root override) |
| **Tag** | Write ML results back to files with a confidence threshold you control. **Never touches Rekordbox cue points or beat grids.** |
| **Back up tags** | One-click full snapshot of every ID3/Vorbis/MP4 tag (including GEOB/PRIV binary frames). Restore at any time. History view tracks past backups. |

## What's installed where

```
Desktop app (recommended)
├─ Tauri 2.x shell (Rust) ────────────────► spawns Python sidecar at startup
├─ React + Vite frontend (TypeScript) ────► UI you actually click
└─ Python sidecar = vibechek CLI ─────────► all real work happens here

CLI (headless / scripting)
└─ `vibechek` — every feature of the GUI is also a CLI subcommand
```

## Install

**End users:** download the installer for your OS from
[Releases](https://github.com/papapew/Vibechek/releases). The first time
you click **Analyze**, the app walks you through any missing prerequisites
(including WSL setup on Windows). See [docs/USER_GUIDE.md](docs/USER_GUIDE.md)
for the full walkthrough.

**Developers:**

```bash
git clone https://github.com/papapew/Vibechek.git
cd Vibechek

# Python core
python -m venv .venv
. .venv/Scripts/activate          # Windows
# source .venv/bin/activate         # macOS / Linux
pip install -e ".[dev]"

# Frontend (separate)
cd ui
npm install

# Run the desktop app in dev mode
# (First: ../packaging/build-windows.bat to stage the sidecar binary)
$env:VIBECHEK_SIDECAR = "$pwd\..\.venv\Scripts\vibechek.exe"   # Windows
npm run tauri:dev
```

For ML analysis on Windows, the GUI auto-installs Essentia inside WSL Ubuntu
the first time you click Analyze. On Linux/macOS: `pip install essentia-tensorflow`.

Full instructions: [docs/INSTALL.md](docs/INSTALL.md).

## Roadmap status

| Phase | Goal | Status |
|---|---|---|
| **1** | Package the proven Python pipeline into `vibechek` | ✅ Done |
| **2** | Cross-platform installer (PyInstaller + Tauri bundles + signed CI release pipeline) | ✅ Done |
| **3** | Desktop UI with onboarding, recent libraries, rules-based dedupe, settings persistence, full WSL automation | ✅ Done |
| **4** | Polish, docs, community launch | 🚧 In progress |

See [docs/ROADMAP.md](docs/ROADMAP.md) for the full breakdown + future ideas.

## CLI quick reference

```
vibechek system-info           # what CPU/RAM/GPU Vibechek sees
vibechek preflight             # is Essentia + models ready?
vibechek download-models       # grab the ~800 MB ML models

vibechek analyze <path>        # full ML pass
vibechek dedupe  <path>        # find duplicates (MD5 + Chromaprint)
vibechek organize <analysis>   # move tracks into genre folders
vibechek tag <analysis>        # write ML tags (preserves Rekordbox data)

vibechek backup-tags <path>    # snapshot all tags to JSON
vibechek restore-tags <file>   # restore from snapshot
vibechek route <staging> <lib> # copy tagged tracks into matching genre folders

vibechek rpc                   # JSON-RPC sidecar (used by the desktop app)
```

Every command supports `--help` and destructive commands support `--dry-run`.

## Architecture

```
React UI ──[Tauri invoke]──► Rust shell ──[JSON-RPC stdin/stdout]──► Python sidecar
                                                                          │
                              ┌───────────────────────────────────────────┴───────────┐
                              │ vibechek package (28 RPC methods)                     │
                              │  analyzer · tagger · duplicates · organizer · genres  │
                              │  config · cancellation · library_state · backup_history│
                              │  preflight · wsl · resources · logging_setup           │
                              └──────────────────────────────────────────────────────┘
```

The Python sidecar handles every long-running operation in a thread pool (8
workers) so the UI never freezes. Long ops are cancellable. On Windows
without native Essentia, analyze transparently routes through `vibechek` in
WSL Ubuntu (the GUI walks the user through installing it).

Full deep dive: [ui/README.md](ui/README.md).

## Project structure

```
vibechek/                  Python package + CLI + JSON-RPC sidecar
├── analyzer.py            ML analysis pipeline (Essentia wrapping)
├── tagger.py              Tag read/write (preserves Rekordbox binary frames)
├── duplicates.py          MD5 + Chromaprint dedup
├── organizer.py           Genre-folder reorganization
├── wsl.py                 WSL detection + auto-install + path translation
├── preflight.py           "Can we analyze?" check
├── cancellation.py        Cooperative cancellation tokens
├── library_state.py       Recent libraries + analysis auto-save
├── backup_history.py      Per-user backup history
├── logging_setup.py       Rotating file logs
├── config.py              TOML round-trip settings
├── resources.py           CPU/RAM/GPU detection
└── rpc.py                 JSON-RPC server (28 methods, threadpool dispatch)

tests/                     176 pytest tests (1 skipped: needs audio fixture)

ui/                        Desktop app
├── src/components/        React components (Library, Duplicates, Organize, Tags, Settings, …)
├── src/hooks/             useSidecar, useApplyTags, useConfigPersistence
├── src/lib/               keeperRules (auto-pick logic)
├── src/stores/            Zustand stores
├── src/types/             generated.ts (auto-mirrored from Python) + hand-written view types
└── src-tauri/             Rust shell (sidecar manager, IPC, capabilities)

packaging/                 PyInstaller spec + per-OS build scripts + Inno Setup + icons
scripts/                   generate_ts_types.py — Python dataclass → TS interface generator
docs/                      INSTALL, USER_GUIDE, ROADMAP, PROJECT_SUMMARY, PROTOTYPE_DESIGN
legacy/                    Original v1 scripts that processed the 12k-track library
.github/workflows/         ci.yml (tests on push), release.yml (builds on tag)
```

## Tests

```bash
# Python
./.venv/Scripts/python.exe -m pytest -q
# → 176 passed, 1 skipped

# Frontend (after npm install)
cd ui && npm test
# → 24 tests across keeperRules, LibraryFilters, ConfirmModal, Sidebar
```

## Contributing

The whole point of Vibechek being OSS is that DJs-who-code can shape it. Contributions of any size welcome:

1. Fork + branch
2. Make your change. Add tests where you can.
3. Run `./.venv/Scripts/python.exe -m pytest -q` (Python) and `cd ui && npm test` (frontend).
4. If you added a Python dataclass field, run `./.venv/Scripts/python.exe scripts/generate_ts_types.py` to refresh the TS mirror.
5. Open a PR.

## Acknowledgements

- [Essentia](https://essentia.upf.edu/) — the ML magic that makes everything else possible
- [Chromaprint](https://acoustid.org/chromaprint) — audio fingerprinting
- [Mutagen](https://mutagen.readthedocs.io/) — audio tag I/O
- [Tauri](https://v2.tauri.app/) — small, fast desktop shell

## License

AGPL-3.0-or-later. See [LICENSE](LICENSE).
