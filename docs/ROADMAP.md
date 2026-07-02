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
  - Release: on tag, builds PyInstaller CLI + Tauri installers and publishes a GitHub Release
- ✅ macOS `.dmg` (Apple Silicon) built by Tauri in CI
- ✅ Code-signing plumbing is opt-in (cert secrets → signed; none → unsigned). Beta ships **unsigned**; actual Developer ID / Authenticode signing + notarization deferred (paid certs).

### Phase 3 — Desktop UI _(complete)_

Goal: full graphical workflow — open folder, analyze, preview, apply. No CLI required.

- ✅ **JSON-RPC sidecar** ([`vibechek/rpc.py`](../vibechek/rpc.py)): 49 methods (28 at first ship, grown since), threadpool dispatch (8 workers), stdout lock, progress + per-track notifications, structured error codes.
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
  - **Library**: scan-only fast path, full analyze, virtualized track list, filter chips (genre/energy/mood/vocal/direction/key), bulk-select + bulk-apply with genre breakdown summary, an **"N to review" filter + per-row marker for genre source conflicts** (tag ≠ audio ≠ web) with one-click batch **Approve / Revert to tag** to resolve them, and an error-count badge with an "errors only" filter
  - **Track Details** side panel: file metadata, before/after tag diff, a **"Genre sources" panel** (your tag vs audio vs web + which won + a plain-English reason), compatible-key suggestions, waveform audio preview, per-file apply
  - **Duplicates**: rules-based auto-keeper picker (codec > bitrate > size > newest > shortest-path), manual override, plain-English language
  - **Organize**: source picker, preview, polished confirm with backup-first option, post-op result panel with folder breakdown
  - **Tags**: backup/restore + history of past backups with stale warnings
  - **Settings**: System resources card, Analysis (workers slider, GPU mode), Tagging/Duplicates/Organization (behind Advanced disclosure), restore-defaults, View logs
- ✅ **Onboarding overlay**: three-slide tour shown once on first launch, persisted in the JSON config (`seen_onboarding`)
- ✅ **Recent libraries**: empty Library tab shows clickable cards for past libraries with relative timestamps
- ✅ **Error UX**: global ErrorToast with Copy details + View logs + Report on GitHub (prefilled issue)
- ✅ **Frontend tests** (vitest + RTL + jsdom + Tauri mocks) — 24 at Phase 3, 93 now
- ✅ **109 backend tests** added in Phase 3 (modules: wsl, preflight, cancellation, library_state, logging_setup, RPC dispatch with concurrency check); 929 total now
- ✅ **Generated TS types**: `scripts/generate_ts_types.py` walks 10 Python modules and emits 30 TS interfaces. `__ts_overrides__` mechanism handles wire-form ≠ storage-form cases.
- ✅ Tagged `v0.3.0` then iterated to `v0.4.0-beta.10` (see CHANGELOG)

### Phase 4 — Polish, docs, community launch _(in progress)_

- ✅ README ([README.md](../README.md))
- ✅ End-user guide ([USER_GUIDE.md](USER_GUIDE.md))
- ✅ Install instructions for every platform ([INSTALL.md](INSTALL.md))
- ✅ Architecture docs ([ui/README.md](../ui/README.md))
- ✅ Mutagen-read opt-out in `duplicates._file_info` (skip the per-file probe when rules don't need bitrate/duration)
- ✅ Shared `useApplyTags` hook (eliminates dup between LibraryBrowser and TrackDetails)
- ✅ Polished confirm modal across the app (no `window.confirm` / `window.alert` remain)
- ✅ Real audio waveform via WaveSurfer.js
- ✅ Toast notifications for success / info
- ✅ `.gitignore` hygiene (test artifacts, IDE settings, OS junk)

**Shipped during the beta.3 → beta.8 cycle:**

- ✅ **Full codebase audit** — fixed every HIGH + MED finding (atomic writes, path-traversal guard, organize overwrite-prevention, cancellation coverage, RPC sync guardrail, …). See CHANGELOG beta.4.
- ✅ **End-to-end audit closeout** — the beta.8 remediation cleared the remaining findings: all 4 HIGH (lossy tag backup now format-complete incl. AIFF/WAV cue frames, dead Direction classifier, opaque UNC/network-share error, racing concurrent index writes) plus the MED/LOW sweep (RPC input validation/clamping, `sanitize_folder_name` reserved-name rejection, multi-GPU device-0 pin, atomic WSL shim rewrites, …). See CHANGELOG beta.8.
- ✅ **FLAC → CDJ export** ([`vibechek/cdj_export.py`](../vibechek/cdj_export.py)) — `vibechek cdj-export <rekordbox.xml> --out <dir>` transcodes a FLAC library to sample-identical 16-bit AIFF and rewrites the Rekordbox XML so beat grids (`TEMPO`) + cues (`POSITION_MARK`) copy across with zero offset math, letting older Pioneer CDJs play a FLAC collection. Strictly additive; never MP3 (its encoder delay shifts the grid). Optional `[cdj]` extra (soundfile) with an ffmpeg fallback.
- ✅ **In-app auto-update wiring (opt-in)** — `tauri-plugin-updater`; Settings → "Software updates" → check / download / install / relaunch. CI signs artifacts + publishes `latest.json` when a signing key is configured; ships inert (unsigned) until one is enrolled.
- ✅ **ONNX inference backend (opt-in, GPU-accelerated, validated end-to-end)** ([`vibechek/onnx_backend.py`](../vibechek/onnx_backend.py)) — selectable in Settings via `AnalysisConfig.inference_engine = "onnx"` (default stays `essentia_tf`, byte-unchanged). Runs every neural forward pass on ONNX Runtime (MTG's official EffNet ONNX backbone + tf2onnx-converted heads) with a cross-vendor GPU EP chain (CUDA → ROCm → CoreML → CPU); essentia stays only for DSP. **NVIDIA CUDA is hardware-validated** (RTX 4070, TF-free, onnxruntime-gpu + nvidia-cu12 + DLL preload); ROCm + CoreML wired but hardware-unverified. Provisioned into a separate `~/.vibechek/venv-onnx` via "Set up ONNX engine" (installer auto-picks the GPU runtime); extras `[onnx]` / `[onnx-gpu]`. Validated to match the TF path on a real track (embedding cosine 0.99942, sub-0.005 deltas) via `scripts/onnx_parity.py`.
- ✅ **Genre: three classifiers + smart tag reconciliation** — [`vibechek/genres.py`](../vibechek/genres.py) `reconcile_genre` (tier order tag › grounded-web › audio, via `genre_source_policy`); the opt-in **CLAP** pure-audio classifier ([`vibechek/clap_genre.py`](../vibechek/clap_genre.py): a CLAP embedding → distance-weighted kNN over a bundled 2 MB reference, ~2× the Discogs head — ~54% exact / ~69% family on a 74-track web-verified gold corpus); and the opt-in **online web-synthesis resolver** ([`vibechek/genre_web.py`](../vibechek/genre_web.py): a fully-local LLM reads keyless `ddgs` results for artist+title, distrusts commercial chart buckets, evidence-gated — ~60%). One-click `setup_clap_engine` / `setup_genre_resolver`; extras `[clap]` / `[resolver]`; default stays Discogs + `prefer_tag` (zero regression). Verified end-to-end in a real WSL analyze + a live WebDriver GUI crawl.
- ✅ **Trust UX — genre source conflict surfacing** ([`ui/src/lib/review.ts`](../ui/src/lib/review.ts)) — the reconciler already records provenance (`ml_genre_source`, `ml_genre_conflict`, the pre-reconcile audio/web reads); these are now declared on the `MLResult` dataclass so the generated TS types carry them, and surfaced in the UI: a per-row review marker, an **"N to review"** toolbar filter (composes with the chip filters; mutually exclusive with "errors only"), and a **"Genre sources"** panel in Track Details (your tag vs audio vs web + which won + a plain-English reason). Read-only — it never overwrites a hand-curated tag, it flags the disagreement for a one-click look. First of the post-accuracy **trust-UX** line; #2 (inline correct/override) and #3 (import MIK/Rekordbox/Beatport tags as priors) follow.
- ✅ **Trust UX #2 — inline genre correction (batch approve/revert)** (`resolve_genre_conflicts` RPC) — the review queue is now actionable. Select flagged tracks (or select-all within the "N to review" filter) and **Approve** to accept Vibechek's reconciled genre, or **Revert to tag** to keep the genre already in the file. Either clears the conflict (the track leaves the queue; the queue shows an "all caught up" state when drained) and persists the decision to the saved analysis so it survives a reload — an approved track records `ml_genre_source="approved"` (a human vouched for it). Augment-not-overwrite holds: it **never writes file tags** (that stays the backup-first **Apply ML tags** flow); it only resolves the in-library review state. #3 (tag import as priors) is the remaining trust-UX effort.
- ✅ **Variant-aware de-duplication** — keeps the versions a DJ wants side by side (Extended / Radio / Remix edits, FLAC *Original Mix* + MP3 *Extended*), collapsing only true duplicates *within* a version. Configurable (`keep_distinct_versions`, `keep_all_formats`, duration tolerance for mislabeled lengths).
- ✅ **Accuracy improvements** — Shaath key profile + a 3-segment majority vote (gold-corpus shoot-out winner; corrects the single-read major→parallel-minor bias — ~71% exact-Camelot / ~78% mixable); BPM octave-error guard (folds 70↔140 / 87↔174 and cross-checks the filename BPM); de-dup recall via Chromaprint sliding-offset alignment + multi-probe bucketing.
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
- **Variable / dynamic beatgrid (local tempo).** Today Vibechek emits one static BPM per track (essentia RhythmExtractor2013, octave-folded + filename-reconciled → ~97% exact on the electronic gold corpus, already near the practical ceiling for a single value). The genuine *capability* gap vs Rekordbox's "dynamic" analysis is a VARIABLE beatgrid — local tempo over the track — for tempo-drifting / live / non-quantized / older recordings. A TempoCNN-style model on <12 s windows (Schreiber-Müller) can estimate local tempo and visualize drift. This is a feature gap, not an accuracy gap: single-value BPM accuracy has little headroom (pure-audio cross-genre exact tempo plateaus ~74% Acc1 because of octave errors — Vibechek's octave-folding already targets that). Worth it only if the desktop UX grows a beat/grid view. (NB: Mixed In Key itself does NOT do variable beatgrids — fixed chunks → one static BPM, same class as Vibechek. See docs/COMPETITORS.md.)
- **Trust-UX line (post-accuracy direction).** Analysis accuracy is at the published ceiling for electronic music (see [docs/COMPETITORS.md](COMPETITORS.md)), so the higher-leverage work for adoption by skeptical pros is *trust*, not more accuracy %. Three staged efforts: **#1 conflict/confidence surfacing — shipped** (see Phase 4); **#2 inline correct/override — shipped** (batch approve/revert from the review queue via `resolve_genre_conflicts`, persisted and never lost; see Phase 4); **#3 tag import as priors** — below.
- **MixedInKey/Lexicon/Beatport tag import (trust-UX #3, augment-not-overwrite).** "I already have data from $TOOL, use that as ground truth instead of/alongside ML." Read existing MIK key/energy, Rekordbox XML (key/energy/colour/my-tags), and Beatport genre tags and feed them into `genres.reconcile_genre` as strong priors so hand-curation is preserved.
- **Native-Windows (WSL-free) analyze path — SHIPPED (experimental, opt-in) in 0.6.0-beta.** Windows routed ML through WSL only because essentia has no Windows wheel. Now there's `inference_engine="native"`: ONNX inference + a pure-NumPy mel frontend (`vibechek/numpy_frontend.py`, reproduces essentia's `TensorflowInputMusiCNN` bit-close — log-mel L1 0.0000, embedding cosine 1.00000) + a **DSP-only native essentia wheel** (decode/BPM/key) built on Windows from the wo80 MSVC fork (reproducible via `scripts/build_native_essentia_wheel.ps1`, delvewheel-packaged self-contained). Validated end-to-end: a real analyze ran fully in-process, no WSL, no TensorFlow; the wheel's `RhythmExtractor2013`/`KeyExtractor` match WSL-essentia exactly (KEY 12/12, BPM 11/12 on a check). Selectable in Settings; **default stays `essentia_tf`**. Remaining before it can be the default: bundle the cp312 wheel (matching the release Python) into the Windows installer + a one-click "set up native engine" flow + the full gold-corpus gate. macOS/Linux are already native via the `onnx` engine (essentia ships wheels there). See `scripts/native_frontend_parity.py`, `tests/test_native_engine.py`, CHANGELOG.
- **Native GPU on Windows.** ~~Currently auto-routes through WSL~~ — done: GPU works through WSL CUDA wheels today. A native path is only worth it if/when Essentia ships Windows wheels with CUDA.
- **ONNX inference backend.** ~~Retire the EOL bundled TensorFlow 2.5 from the inference path~~ — **essentially done.** The engine shipped (selectable in Settings behind `AnalysisConfig.inference_engine = "onnx"`, default still `essentia_tf`), is GPU-accelerated (NVIDIA CUDA hardware-validated; ROCm + CoreML wired), and is validated end-to-end against the TF path. Remaining before flipping the default: host the converted head `.onnx` files on the model mirror (so no local `tf2onnx` conversion is needed), run a full-library smoke test, and validate the cross-vendor GPU paths (ROCm / CoreML) on real hardware.
- **Cancel mid-step in WSL install.** Largely addressed in beta.3 (setsid process-group kill); further per-phase granularity could still help.

## Architectural notes

- **Sidecar concurrency.** 8 workers in a ThreadPoolExecutor. Quick ops (config, system_info, preflight, library_state) interleave freely. Long ops (analyze, dedupe, organize, install) can run concurrently with quick ops; only the cancellation token is a singleton, so only ONE long op can be cancellable at a time.
- **Path translation lives only in `wsl.py`.** The frontend never sees `/mnt/c/...`. The analyzer's WSL detour translates on the way in and out.
- **JSON config drops unknown keys.** Adding fields is safe — old configs just inherit the defaults for new fields. (Config is JSON since 0.3.0; a pre-0.3.0 `config.toml` is read once as a migration.)
- **`__ts_overrides__` pattern.** A class attribute that the TS generator reads — used when the JSON wire form is narrower than the Python storage form (e.g., `TrackAnalysis.existing_tags: dict[str, Any]` is typed as `ExistingTags` in TypeScript).
