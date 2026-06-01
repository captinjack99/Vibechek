# Vibechek Roadmap

## North star

A DJ should be able to download Vibechek, double-click an installer, point it at a music folder, and get a clean, ML-tagged, deduplicated, organized library — with every default sensible and every knob adjustable.

## Guiding principles

1. **Safe by default.** Backup before every write. Never touch Rekordbox binary frames. Every destructive operation has a confirm modal with a clear preview.
2. **Granular controls.** Every flag in the legacy scripts becomes a setting in the UI. Power users get knobs; new users get good defaults.
3. **Just works.** No command line required for end-users. One-click install per OS. Auto-setup of WSL on Windows.
4. **OSS-friendly stack.** Python core so any DJ-with-some-coding can contribute. Heavy lifting (Essentia) is C++ already; we just need the Python wrapper to be clean.
5. **No lost work.** Analysis auto-saves. Settings persist. Recent libraries surface on startup.

## Phases

### Phase 1 — Package what works _(complete)_

Goal: a working `pip install -e .` package that mirrors the legacy CLI scripts via a single `vibechek` command.

- ✅ Git repository initialized + AGPL-3.0 license
- ✅ Python package (`vibechek/` with `cli`, `config`, `analyzer`, `tagger`, `duplicates`, `organizer`, `genres`, `keys`, `filename`, `utils`)
- ✅ `pyproject.toml` with Click-based CLI entry point
- ✅ Port `legacy/analyze_dj_tracks_v2.py` → `vibechek/analyzer.py`
- ✅ Port `legacy/backup_tags.py` → `vibechek/tagger.py`
- ✅ Port `legacy/apply_tags_filtered.py` → `vibechek/tagger.py`
- ✅ Port `legacy/find_duplicates.py` + `move_safe_duplicates.py` → `vibechek/duplicates.py`
- ✅ Port `legacy/organize_by_genre.py` + `copy_to_genre_folders.py` → `vibechek/organizer.py`
- ✅ JSON config persistence in `vibechek/config.py` (delivered Phase 3; migrated from TOML in 0.3.0)
- ✅ 67 pytest tests
- [ ] Tag a `v0.1.0` release

### Phase 2 — Cross-platform installer _(complete)_

Goal: a build any DJ can download, unzip, and run — without touching `pip`.

- ✅ PyInstaller spec ([`packaging/vibechek.spec`](../packaging/vibechek.spec))
- ✅ Build scripts per OS: `build-windows.bat` / `build-macos.sh` / `build-linux.sh`
- ✅ Windows desktop installer — Tauri-built NSIS `.exe` (per-user); CLI distributed as `vibechek-windows-x64.zip` (manual PATH)
- ✅ Branded app icons in every format Tauri needs (`packaging/generate-icons.py`)
- ✅ First-run model downloader command + GUI button
- ✅ GitHub Actions:
  - CI: tests on Linux/macOS/Windows × Python 3.10/3.12
  - Release: on tag, builds PyInstaller CLI + Tauri installers, drafts a GitHub Release
- ✅ macOS `.dmg` (Apple Silicon) built by Tauri in CI
- ✅ Code-signing plumbing is opt-in (cert secrets → signed; none → unsigned). Beta ships **unsigned**; actual Developer ID / Authenticode signing + notarization deferred (paid certs).

### Phase 3 — Desktop UI _(complete)_

Goal: full graphical workflow — open folder, analyze, preview, apply. No CLI required.

- ✅ **JSON-RPC sidecar** ([`vibechek/rpc.py`](../vibechek/rpc.py)): 44 methods (28 at first ship, grown since), threadpool dispatch (8 workers), stdout lock, progress + per-track notifications, structured error codes.
- ✅ **Async sidecar**: long ops don't block fast ones. Fast endpoints (system_info, preflight) interleave with running analyze/dedupe/organize.
- ✅ **Cancellation** ([`vibechek/cancellation.py`](../vibechek/cancellation.py)): cooperative token; multiprocessing pool terminates cleanly on cancel; WSL subprocess gets SIGTERM+SIGKILL.
- ✅ **Auto-saved analysis state** ([`vibechek/library_state.py`](../vibechek/library_state.py)): analysis result writes to `<data_dir>/analyses/...` automatically; recent libraries index surfaces them on the Library tab's empty state.
- ✅ **Structured logging** ([`vibechek/logging_setup.py`](../vibechek/logging_setup.py)): rotating file logs; `get_log_tail` RPC + LogsViewer modal in the GUI.
- ✅ **System resource detection** ([`vibechek/resources.py`](../vibechek/resources.py)): CPU/RAM/GPU detection; workers slider snaps to recommended; GPU auto/on/off control.
- ✅ **JSON settings persistence** ([`vibechek/config.py`](../vibechek/config.py)): debounced auto-save 500ms after any UI change; restore-defaults; Simple/Advanced split. (Reads pre-0.3.0 `config.toml` once as a migration.)
- ✅ **Tauri 2.x Rust shell** ([`ui/src-tauri/`](../ui/src-tauri/)):
  - Spawns Python sidecar
  - Sidecar-death detection (atomic flag, drains pending oneshots on EOF)
  - Re-broadcasts progress notifications as Tauri events
- ✅ **React frontend** ([`ui/src/`](../ui/src/)): Vite, TypeScript, Tailwind, Zustand, react-virtuoso, framer-motion, WaveSurfer.js
- ✅ **Auto-installed prerequisites on Windows** ([`vibechek/wsl.py`](../vibechek/wsl.py)):
  - Detects WSL state + installed distros
  - One-click install of WSL itself (elevated PowerShell, UAC)
  - One-click install of vibechek + essentia + chromaprint inside the user's distro
  - Path translation at the WSL boundary (`C:\foo` ↔ `/mnt/c/foo`)
  - Live progress log in the install dialog, with Cancel
- ✅ **All major views** with consistent error handling, polished confirm modals, and Toast notifications:
  - **Library**: scan-only fast path, full analyze, virtualized track list, filter chips (genre/energy/mood/vocal), bulk-select + bulk-apply with genre breakdown summary, error count badge with "errors only" filter
  - **Track Details** side panel: file metadata, before/after tag diff, waveform audio preview, per-file apply
  - **Duplicates**: rules-based auto-keeper picker (codec > bitrate > size > newest > shortest-path), manual override, plain-English language
  - **Organize**: source picker, preview, polished confirm with backup-first option, post-op result panel with folder breakdown
  - **Tags**: backup/restore + history of past backups with stale warnings
  - **Settings**: System resources card, Analysis (workers slider, GPU mode), Tagging/Duplicates/Organization (behind Advanced disclosure), restore-defaults, View logs
- ✅ **Onboarding overlay**: three-slide tour shown once on first launch, persisted in TOML
- ✅ **Recent libraries**: empty Library tab shows clickable cards for past libraries with relative timestamps
- ✅ **Error UX**: global ErrorToast with Copy details + View logs + Report on GitHub (prefilled issue)
- ✅ **Frontend tests** (vitest + RTL + jsdom + Tauri mocks) — 24 at Phase 3, 32 now
- ✅ **109 backend tests** added in Phase 3 (modules: wsl, preflight, cancellation, library_state, logging_setup, RPC dispatch with concurrency check); 487 total now
- ✅ **Generated TS types**: `scripts/generate_ts_types.py` walks 9 Python modules and emits 27 TS interfaces. `__ts_overrides__` mechanism handles wire-form ≠ storage-form cases.
- ✅ Tagged `v0.3.0` then iterated to `v0.4.0-beta.7` (see CHANGELOG)

### Phase 4 — Polish, docs, community launch _(in progress)_

- ✅ Comprehensive README ([README.md](../README.md))
- ✅ End-user guide ([USER_GUIDE.md](USER_GUIDE.md))
- ✅ Install instructions for every platform ([INSTALL.md](INSTALL.md))
- ✅ Architecture docs ([ui/README.md](../ui/README.md))
- ✅ Mutagen-read opt-out in `duplicates._file_info` (skip the per-file probe when rules don't need bitrate/duration)
- ✅ Shared `useApplyTags` hook (eliminates dup between LibraryBrowser and TrackDetails)
- ✅ Polished confirm modal across the app (no `window.confirm` / `window.alert` remain)
- ✅ Real audio waveform via WaveSurfer.js
- ✅ Toast notifications for success / info
- ✅ `.gitignore` hygiene (test artifacts, IDE settings, OS junk)

**Shipped during the beta.3 → beta.7 cycle:**

- ✅ **Full codebase audit** — fixed every HIGH + MED finding (atomic writes, path-traversal guard, organize overwrite-prevention, cancellation coverage, RPC sync guardrail, …). See CHANGELOG beta.4.
- ✅ **Hybrid CPU+GPU analysis** — GPU + CPU workers share one work-stealing queue; per-device throughput measured. `--hybrid/--no-hybrid` + Settings toggle.
- ✅ **GPU on Windows via WSL CUDA wheels** — one-click "Enable GPU" installs NVIDIA pip wheels into the managed venv (works on any WSL distro, no apt/keyring/root).
- ✅ **Operation undo journal** — append-only JSONL for organize + dedupe-move; one-click revert from a "Recent operations" panel; crash-recoverable.
- ✅ **Global persistent audio player** — one player bar at the app root; survives navigation; previews never overlap; always start at 0:00.
- ✅ **Per-field tag write toggles** + configurable vocal sensitivity (raw score stored, re-label without re-analyze); recalibrated vocal cutoffs (instrumental dance no longer mislabelled Vocal).
- ✅ **Wired the rest of the backend into the UI** — DJ profiles picker, doctor (copy diagnostic), verify-models, update-WSL-install, rename/tag a recent library.
- ✅ **CI release pipeline working** — Windows `.exe` (NSIS) + Linux `.deb`/`.AppImage` + macOS `.dmg` build on tag push. Code signing made opt-in; beta ships unsigned (Gatekeeper bypass documented).
- ✅ **PyInstaller `--onefile` sidecar** (single signed-able binary per platform) + version-drift guard for the WSL install.

**Remaining for the launch:**

- [ ] Hand-test the full Windows flow end-to-end (install WSL → install essentia → analyze 12k tracks → organize → restore)
- [ ] Signed + notarized macOS builds (and Authenticode for Windows)
- [ ] Tag `v1.0.0`
- [ ] Community launch (DJ communities, forums, and DJ-software outlets)
- ✅ GitHub Sponsors link live (README) — [ ] Ko-fi optional
- [ ] Screenshots + animated GIFs in README
- [ ] Demo video (90 sec)

## Non-goals (for now)

- **Other DJ software integration.** Rekordbox-first. Serato/Traktor/VirtualDJ XML export can come via community PR.
- **Cloud anything.** Vibechek is local-only. No telemetry, no account, no upload.
- **Music recommendation / curation.** This is an organizer, not a discovery tool.
- **Re-encoding / format conversion.** Not Vibechek's job.

## Future ideas (v0.5+)

Logged here so they don't get forgotten:

- **Per-genre confidence thresholds.** "Trust ML for House at 70%, but require 90% for Trance because the model gets it wrong."
- **Smart playlist export.** Generate `.m3u8` for "Peak House, 125-128 BPM, Camelot 8A-9A".
- **Multi-library support.** Some DJs split by genre — let them work on multiple library roots in one Vibechek session.
- **A/B compare two tracks.** Side-by-side waveform + tag diff for the "are these actually duplicates?" question.
- **Per-file tag undo / history.** Operation-level undo (organize/dedupe) shipped in beta.5; per-file tag-write history (roll back a single field write, not just a full restore) is still open.
- **MixedInKey/Lexicon/Beatport tag import.** "I already have data from $TOOL, use that as ground truth instead of/alongside ML."
- **Native GPU on Windows.** ~~Currently auto-routes through WSL~~ — done: GPU works through WSL CUDA wheels today. A native path is only worth it if/when Essentia ships Windows wheels with CUDA.
- **Cancel mid-step in WSL install.** Largely addressed in beta.3 (setsid process-group kill); further per-phase granularity could still help.

## Architectural notes

- **Sidecar concurrency.** 8 workers in a ThreadPoolExecutor. Quick ops (config, system_info, preflight, library_state) interleave freely. Long ops (analyze, dedupe, organize, install) can run concurrently with quick ops; only the cancellation token is a singleton, so only ONE long op can be cancellable at a time.
- **Path translation lives only in `wsl.py`.** The frontend never sees `/mnt/c/...`. The analyzer's WSL detour translates on the way in and out.
- **JSON config drops unknown keys.** Adding fields is safe — old configs just inherit the defaults for new fields. (Config is JSON since 0.3.0; a pre-0.3.0 `config.toml` is read once as a migration.)
- **`__ts_overrides__` pattern.** A class attribute that the TS generator reads — used when the JSON wire form is narrower than the Python storage form (e.g., `TrackAnalysis.existing_tags: dict[str, Any]` is typed as `ExistingTags` in TypeScript).
