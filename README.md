# Vibechek

**ML-powered DJ library organizer.** Analyze, tag, and organize thousands of tracks automatically — without touching the cue points and beat grids your DJ software depends on.

> Status: 🚧 **Early development.** The underlying Python pipeline has analyzed and organized a 12,000+ track personal library successfully. We are now turning it into a packaged, installable application for everyone.

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

See [`docs/ROADMAP.md`](docs/ROADMAP.md) for the phased plan. We are currently at the start of **Phase 1: Package what works**.

| Phase | What | Status |
|---|---|---|
| 1 | Refactor working Python scripts into a `vibechek` package + CLI | 🚧 In progress |
| 2 | Cross-platform installer (PyInstaller + platform installers) | ⏳ Planned |
| 3 | Desktop UI (Tauri + Python sidecar, reusing prototype design) | ⏳ Planned |
| 4 | Polish, docs, community launch | ⏳ Planned |

## What's already in the repo

- [`vibechek/`](vibechek/) — Python package skeleton (stubs being filled in from the legacy scripts).
- [`legacy/`](legacy/) — The original scripts that have already processed a 12k-track library. Working, but not packaged. Source of truth being ported into the package.
- [`docs/`](docs/) — Project summary, design docs for the planned desktop app, roadmap.

## Quick start (developers)

```bash
git clone <repo-url>
cd vibechek

# Create venv
python -m venv .venv
. .venv/Scripts/activate   # Windows
# source .venv/bin/activate  # macOS/Linux

# Install in editable mode
pip install -e .

# Run the CLI (stubs only for now)
vibechek --help
```

End-user install instructions will come with Phase 2.

## Acknowledgements

- [Essentia](https://essentia.upf.edu/) — the ML magic that makes everything else possible.
- [Chromaprint](https://acoustid.org/chromaprint) — audio fingerprinting.
- [TagLib](https://taglib.org/) / [Mutagen](https://mutagen.readthedocs.io/) — audio metadata handling.

## License

AGPL-3.0-or-later. See [LICENSE](LICENSE).
