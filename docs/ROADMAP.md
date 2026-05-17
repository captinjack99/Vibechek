# Vibechek Roadmap

## North star

A DJ should be able to download Vibechek, double-click an installer, point it at a music folder, and get a clean, ML-tagged, deduplicated, organized library — with every default sensible and every knob adjustable.

## Guiding principles

1. **Safe by default.** Backup before every write. Never touch Rekordbox binary frames. Every destructive operation has a dry-run preview.
2. **Granular controls.** Every flag in the legacy scripts becomes a setting in the UI. Power users get knobs; new users get good defaults.
3. **Just works.** No command line required for end-users. One-click install per OS.
4. **OSS-friendly stack.** Python core so any DJ-with-some-coding can contribute. Heavy lifting (Essentia) is C++ already; we just need the Python wrapper to be clean.

## Phases

### Phase 1 — Package what works _(in progress)_

Goal: a working `pip install -e .` package that mirrors the legacy CLI scripts via a single `vibechek` command.

**Done**
- ✅ Git repository initialized
- ✅ AGPL-3.0 license
- ✅ Package skeleton (`vibechek/` with `cli`, `config`, `analyzer`, `tagger`, `duplicates`, `organizer`)
- ✅ `pyproject.toml` with Click-based CLI entry point
- ✅ Legacy scripts preserved in `legacy/` for reference

**To do**
- [ ] Port `legacy/analyze_dj_tracks_v2.py` → `vibechek/analyzer.py`
- [ ] Port `legacy/backup_tags.py` → `vibechek/tagger.py` (backup + restore)
- [ ] Port `legacy/apply_tags_filtered.py` → `vibechek/tagger.py` (apply)
- [ ] Port `legacy/find_duplicates.py` + `move_safe_duplicates.py` → `vibechek/duplicates.py`
- [ ] Port `legacy/organize_by_genre.py` + `copy_to_genre_folders.py` → `vibechek/organizer.py`
- [ ] TOML config persistence in `vibechek/config.py`
- [ ] First round of pytest tests against a small fixture library
- [ ] Tag a `v0.1.0` release

### Phase 2 — Cross-platform installer

Goal: `vibechek-0.2.0-win64.msi` (and `.dmg`, `.AppImage`) that any DJ can double-click.

- [ ] PyInstaller build per OS
- [ ] First-run model downloader (so the installer stays small; Essentia models add ~800MB)
- [ ] Inno Setup (Windows), `dmgbuild` (macOS), `appimagetool` (Linux)
- [ ] GitHub Actions CI building all three platforms on tag push
- [ ] Code signing for Windows (eventually) and macOS notarization (eventually)
- [ ] `v0.2.0` release with downloadable installers

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
