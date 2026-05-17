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

### Phase 3 — Desktop UI

Goal: full graphical workflow — open folder, analyze, preview tag changes, apply selectively.

- [ ] Tauri shell with Python sidecar (or PyWebView fallback)
- [ ] Port the React prototype design in `docs/prototype/` and `docs/PROTOTYPE_DESIGN.md`
  - Library browser (virtualized for 10k+ tracks)
  - Analysis progress overlay
  - Track detail panel with diff preview before write
  - Settings page exposing **every** field in `vibechek/config.py`
  - Duplicates view (group → pick keeper → resolve)
- [ ] Wire IPC to the Python core
- [ ] `v0.3.0` release with full GUI

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
