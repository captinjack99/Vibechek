# Vibechek Roadmap

## North star

A DJ should be able to download Vibechek, double-click an installer, point it at a music folder, and get a clean, ML-tagged, deduplicated, organized library — with every default sensible and every knob adjustable.

## Guiding principles

1. **Safe by default.** Backup before every write. Never touch Rekordbox binary frames. Every destructive operation has a dry-run preview.
2. **Granular controls.** Every flag in the legacy scripts becomes a setting in the UI. Power users get knobs; new users get good defaults.
3. **Just works.** No command line required for end-users. One-click install per OS.
4. **OSS-friendly stack.** Python core so any DJ-with-some-coding can contribute. Heavy lifting (Essentia) is C++ already; we just need the Python wrapper to be clean.

## Phases

### Phase 1 — Package what works _(complete)_

Goal: a working `pip install -e .` package that mirrors the legacy CLI scripts via a single `vibechek` command.

- ✅ Git repository initialized + AGPL-3.0 license
- ✅ Python package (`vibechek/` with `cli`, `config`, `analyzer`, `tagger`, `duplicates`, `organizer`, `genres`, `keys`, `filename`, `utils`)
- ✅ `pyproject.toml` with Click-based CLI entry point
- ✅ Port `legacy/analyze_dj_tracks_v2.py` → `vibechek/analyzer.py`
- ✅ Port `legacy/backup_tags.py` → `vibechek/tagger.py` (backup + restore)
- ✅ Port `legacy/apply_tags_filtered.py` → `vibechek/tagger.py` (apply with confidence threshold + GEOB/PRIV preservation)
- ✅ Port `legacy/find_duplicates.py` + `move_safe_duplicates.py` → `vibechek/duplicates.py`
- ✅ Port `legacy/organize_by_genre.py` + `copy_to_genre_folders.py` → `vibechek/organizer.py`
- ✅ First round of pytest tests (67 passing, 1 skipped for missing audio fixtures)
- [ ] TOML config persistence in `vibechek/config.py` (deferred — GUI in Phase 3 will need this)
- [ ] Tag a `v0.1.0` release

### Phase 2 — Cross-platform installer _(in progress)_

Goal: a build any DJ can download, unzip, and run — without touching `pip`.

- ✅ PyInstaller spec ([`packaging/vibechek.spec`](../packaging/vibechek.spec)) — one-folder bundle, CLI-only, ~26 MB.
- ✅ Build scripts per OS:
  - [`packaging/build-windows.bat`](../packaging/build-windows.bat)
  - [`packaging/build-macos.sh`](../packaging/build-macos.sh)
  - [`packaging/build-linux.sh`](../packaging/build-linux.sh)
- ✅ Inno Setup installer config ([`packaging/installer.iss`](../packaging/installer.iss)) — optional PATH integration, per-user install (no admin).
- ✅ First-run model downloader command: `vibechek download-models`.
- ✅ Essentia kept out of the bundle (too heavy, no Windows wheel) — users install separately or skip if they only need dedup/organize/backup.
- ✅ GitHub Actions:
  - CI ([`.github/workflows/ci.yml`](../.github/workflows/ci.yml)) — tests + lint on Linux/macOS/Windows × Python 3.10/3.12
  - Release ([`.github/workflows/release.yml`](../.github/workflows/release.yml)) — on tag push, build all three platforms, draft GitHub Release
- [ ] `dmgbuild` config for macOS (currently ships `.tar.gz`).
- [ ] `appimagetool` config for Linux (currently ships `.tar.gz`).
- [ ] Code signing for Windows (deferred — needs a paid cert; will revisit if SmartScreen warnings hurt adoption).
- [ ] macOS notarization (deferred — same reason).
- [ ] Tag a `v0.2.0` release with downloadable artifacts.

### Phase 3 — Desktop UI _(in progress)_

Goal: full graphical workflow — open folder, analyze, preview, apply.

- ✅ JSON-RPC sidecar ([`vibechek/rpc.py`](../vibechek/rpc.py)): 14 methods (incl. `system_info`), progress notifications, error handling.
- ✅ System resources module ([`vibechek/resources.py`](../vibechek/resources.py)): CPU/RAM/GPU detection, CUDA_VISIBLE_DEVICES control.
- ✅ Tauri 2.x Rust shell ([`ui/src-tauri/`](../ui/src-tauri/)):
  - Spawns the Python sidecar at startup
  - Multiplexes JSON-RPC requests by id
  - Re-broadcasts progress notifications as `sidecar:*` Tauri events
  - Sidecar binary resolution: env var → externalBin sibling → PATH
- ✅ React frontend ([`ui/src/`](../ui/src/)): Vite, TypeScript, Tailwind, Zustand, react-virtuoso, framer-motion.
- ✅ Sidecar staging scripts ([`packaging/stage-sidecar.{bat,sh}`](../packaging/)) for dev mode.
- ✅ Components:
  - Library browser (virtualized) with search filter and ML tag/energy badges
  - Analysis progress overlay (live updates from sidecar progress notifications)
  - Duplicates view with **per-group resolver** — pick keeper, skip groups, move-to-folder or trash
  - Organize view — pick analysis source, tweak rules, preview plan grouped by destination folder, execute
  - Track details side panel with **before/after diff preview** and "apply to this track only" button
  - Settings page with **System resources detection** (CPU/RAM/GPU), workers slider, GPU auto/on/off
- ✅ Release workflow ([`.github/workflows/release.yml`](../.github/workflows/release.yml)):
  - Builds PyInstaller CLI bundle on each OS
  - Builds Tauri installers (`.msi`/`.exe`/`.dmg`/`.AppImage`/`.deb`) with sidecar bundled
  - Draft GitHub Release with all artifacts on tag push
- [ ] Settings persistence (TOML in user config dir) — currently in-memory only
- [ ] Cancellation support — long ops can't be interrupted from the UI yet
- [ ] App icons (`ui/src-tauri/icons/`) — currently using Tauri defaults
- [ ] Tag a `v0.3.0` release with the desktop app

### Notes on the desktop stack

- **Tauri vs Electron**: Tauri produces ~10× smaller installers (~30 MB shell + ~30 MB Python sidecar vs Electron's ~100+ MB Chromium).
- **Rust vs Python contributors**: The Rust shell is small (~300 lines) and rarely changes. Contributors who don't touch Rust never need to. The interesting code is in React (frontend) and Python (sidecar / core).
- **Sidecar protocol**: JSON-RPC 2.0 over stdin/stdout (Tauri's recommended sidecar pattern). No port management; sidecar lifetime tied to the Tauri process.

### Phase 4 — Polish & launch

- [ ] Docs site (GitHub Pages from `/docs`)
- [ ] User-facing screenshots and walkthroughs
- [ ] `r/Beatmatch`, `r/DJs`, `r/Rekordbox`, DJ TechTools, Pioneer DJ Forum — community announcement
- [ ] GitHub Sponsors + Ko-fi link
- [ ] `v1.0.0` release

## Non-goals (for now)

- **Other DJ software integration.** Rekordbox-first. Serato/Traktor/VirtualDJ XML export can come via community PR.
- **Cloud anything.** Vibechek is local-only. No telemetry, no account, no upload.
- **Music recommendation / curation.** This is an organizer, not a discovery tool.
- **The C++/Rust/Tauri rewrite described in `docs/PROTOTYPE_DESIGN.md`.** The Python core is good enough; the design doc is preserved for UI/UX reference only.

## Open questions

- **Models hosting.** Essentia hosts the models, but bandwidth is unclear. If downloads get rate-limited at scale, we may need to mirror them.
- **GPU support.** Essentia bundles its own TensorFlow build, which conflicts with CUDA 11/12 system installs. CPU-only is fine for v1 (~35 tracks/min on an i9). Investigate later.
- **Code signing budget.** Unsigned binaries get scary SmartScreen warnings on Windows. Sponsorship-funded?
