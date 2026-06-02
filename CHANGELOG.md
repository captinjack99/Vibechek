# Changelog

All notable changes to Vibechek are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Pre-release tags use the form `vMAJOR.MINOR.PATCH-beta.N` (git tag) which maps to `MAJOR.MINOR.PATCHbN` in PEP 440 (`pyproject.toml`). See [docs/RELEASING.md](docs/RELEASING.md).

---

## [Unreleased]

Targeted for `v0.4.0`. Items move out of this section once they ship in a tagged release.

---

## [0.4.0-beta.9] — 2026-06-01

Completes the ONNX migration into a shippable, TensorFlow-free analysis engine.

### Added
- **ONNX inference engine is now user-selectable and TensorFlow-free.** Builds on
  beta.8's validated backend into a shippable feature: a **Settings → Analysis →
  Inference engine** toggle + a **Set up ONNX engine** button provision a separate
  managed environment (`~/.vibechek/venv-onnx`, a second WSL venv on Windows) with
  **plain Essentia + ONNX Runtime and zero TensorFlow**. Confirmed end-to-end by
  running the real analyzer in a plain-essentia venv on a real track — genre/vocal
  match the TF path and `tensorflow` is never imported. The melspec linchpin is
  settled: plain Essentia ships `TensorflowInputMusiCNN` with **bit-identical**
  output to the TF build, so no NumPy reimplementation is needed. New:
  `download_models(engine="onnx")` fetches SHA256-pinned converted heads from the
  `models-onnx-v1` mirror; `vibechek analyze --engine {essentia_tf,onnx}`;
  engine-aware install/routing across `wsl.py` + `native_install.py`; `vibechek[onnx]`
  extra; `scripts/build_onnx_model_bundle.py`. Default stays `essentia_tf` until the
  head bundle is hosted + cross-platform GPU smoke tests land. See `docs/ONNX_MIGRATION.md`.
- **ONNX preflight matches the TensorFlow path.** Selecting ONNX and starting an
  analyze now runs the same readiness check + one-click setup flow as TF instead of
  failing mid-analyze: `preflight` inspects the `venv-onnx` environment and the
  `.onnx` models for the selected engine, so a missing ONNX engine drives the same
  "Set up" / "Download models" prompts. `preflight` / `detect_wsl` / `check_models`
  are engine-aware (defaults preserve the TF path byte-for-byte); the PreflightDialog
  copy and install routing follow the selected engine.

---

## [0.4.0-beta.8] — 2026-06-01

End-to-end engineering audit + remediation, two new flagship features (FLAC→CDJ
export, opt-in ONNX inference), and the auto-update pipeline. Full suite: 664
Python tests, ruff (now enforcing) clean, 32→38 frontend tests, cargo check clean.

### Added
- **FLAC → CDJ export** (`vibechek cdj-export <rekordbox.xml> --out <dir>`). Lets DJs play a FLAC library on older Pioneer CDJs (CDJ-2000nexus and earlier) that don't support FLAC, **without losing cues or beat grids**: each FLAC is transcoded to a sample-identical 16-bit **AIFF**, and the Rekordbox XML is rewritten so the `TEMPO` (grid) + `POSITION_MARK` (cues) copy across with zero offset math (never MP3 — its ~26 ms encoder delay shifts the grid). Strictly additive; source files never modified. Optional `[cdj]` extra (`soundfile`) with an `ffmpeg` fallback. New `vibechek/cdj_export.py` + 20 tests.
- **ONNX inference backend** (`AnalysisConfig.inference_engine = "onnx"`, opt-in; default stays `essentia_tf`). A path off the end-of-life bundled TensorFlow 2.5 onto ONNX Runtime (`vibechek/onnx_backend.py`), using MTG's official EffNet ONNX backbone (CUDA→ROCm→DirectML→CoreML→CPU execution-provider chain; cross-vendor GPU) + `tf2onnx`-converted heads, with essentia kept only for DSP (melspec/BPM/key). **Validated end-to-end**: on a real track every categorical field (genre, subgenre, vocal, mood, energy, BPM, key, direction, timeslot) matches the TF path, with sub-0.005 float deltas (embedding cosine 0.99942) — see `scripts/onnx_parity.py`. The essentia-tensorflow path is byte-unchanged. (Release follow-up: host the converted head `.onnx` on the model mirror so the engine needs no local conversion.)
- **In-app auto-updater** (`tauri-plugin-updater`, opt-in). Settings → "Software updates" → check / download / install / relaunch. CI signs update artifacts + publishes `latest.json` when a signing key is configured; ships inert (unsigned) until you enroll one — see `docs/RELEASING.md`. Public-key-verified payloads.
- **Configurable key-detection profile** + **BPM octave-error guard** (folds 70↔140 / 87↔174 and cross-checks the filename BPM).

### Fixed (audit — 4 HIGH + 18 MED + 18 LOW)
- **Tag backup was lossy** — captured only ~7 fixed fields for FLAC/M4A and **nothing** for AIFF/WAV/OGG/AAC, while advertised as "no loss." Now format-complete: FLAC reads every Vorbis comment, M4A every atom (binary atoms base64'd), and **AIFF/WAV now capture their ID3 GEOB/PRIV cue frames** (previously a silent cue-loss risk on restore). Restore reports unsupported entries instead of skipping silently.
- **Direction classifier was silently dead** — it averaged both softmax columns → "Steady" for ~every track. Now indexes the aggressive column.
- **UNC / network-share library paths** raised an opaque failure inside WSL → now a clear, actionable error.
- **Concurrent index writes** (`rename_library`/`tag_library`/`forget_*`) raced a fixed `.partial` temp file → unique per-write temp name + module locks.
- **Key accuracy**: switched to Essentia's EDM-tuned `edma` key profile (meaningful accuracy lift on electronic music).
- **De-dup recall**: Chromaprint matching now uses sliding-offset alignment + multi-probe bucketing (catches transcodes index-0-only matching missed); the similarity-threshold setting is now actually forwarded.
- RPC write-path inputs are validated/clamped (`id3_text_encoding`, confidence thresholds, worker counts, inverted vocal bands); `sanitize_folder_name` rejects `..`/reserved device names; multi-GPU VRAM probe pins device 0; voice/genre head class order resolved by label; WSL shim rewrites made atomic; install-distro PowerShell-injection allowlist; filename-BPM false-positive guard; ConfirmModal a11y (focus Cancel on destructive, dialog semantics, Escape); analyze-run streaming guard; asset-protocol + shell capability scoping; and many more — see `internal/AUDIT_2026-06-01.md`.

### Changed
- **CI**: `ruff check` is now enforcing (was advisory); third-party GitHub Actions pinned to commit SHAs; removed the stale `installer.iss` (NSIS-via-Tauri is the supported path).
- **Docs**: competitor comparison claims independently verified against 2026 sources (`docs/COMPETITORS.md`); the ONNX migration plan reframed — official MTG ONNX models already exist, so it's "retire EOL TensorFlow 2.5" (security-driven) + a MAEST backbone, not self-conversion (effort ~3 weeks → ~1 week).

---

## [0.4.0-beta.7] — 2026-05-29

Three user-reported accuracy/UX bugs.

### Fixed
- **Vocal detection mislabelled instrumental dance as "Vocal".** Instrumental tracks with prominent melodic leads (e.g. Robert Miles "Children", Eric Prydz "Pjanoo") score ~0.64–0.71 on essentia's voice/instrumental model — above the old 0.6 cutoff, so they were wrongly tagged "Vocal". Recalibrated the cutoffs against measured scores: voice probability `< 0.72` → **Instrumental**, `< 0.88` → **Light Vocal**, else **Vocal**. Verified: Children (0.703) and Pjanoo (0.642) now classify Instrumental; Adele "Chasing Pavements" (0.972) stays Vocal.
- **Audio preview started a new track at the previous track's elapsed time.** Loading track B while track A was 35s in began B at 0:35 (and could seek past B's end if B was shorter). The global player now `stop()`s before loading and seeks to 0 on `ready`, so every preview starts at 0:00.
- **macOS release build hard-failed at the codesign step.** `release.yml` passed `APPLE_CERTIFICATE: ${{ secrets.* }}` directly into the build step's `env:`; an unset secret arrives as an empty-but-defined variable, and Tauri 2's Rust bundler (`std::env::var` → `Ok("")`) treated that as "a cert is present", ran `security import` on empty data, and died with `SecKeychainItemImport: One or more parameters passed to a function were not valid`. Signing is now genuinely opt-in: a new "Configure code signing (opt-in)" step exports each cert var to `$GITHUB_ENV` only when its secret is non-empty, so an unconfigured repo builds an **unsigned** `.dmg`/`.app` instead of failing. Beta macOS builds are unsigned for now — the release notes + README document the one-time `xattr -dr com.apple.quarantine` / right-click-Open Gatekeeper bypass.

### Added
- **Raw vocal score is now stored** (`MLResult.ml_vocal_score`, 0–1), so the Instrumental/Light Vocal/Vocal label can be **retuned and re-applied at tag time without re-analyzing**. (Tracks analyzed before beta.7 lack the raw score and need one re-analysis to benefit.)
- **Configurable vocal sensitivity.** New `TaggingConfig.vocal_instrumental_max` (0.72) and `vocal_full_min` (0.88), surfaced in Settings as a "Vocal detection sensitivity" dual slider (Instrumental ≤ / Vocal ≥). Plumbed through the `apply_ml_tags` RPC.
- **Per-field write toggles.** Replaced the single `skip_bpm_and_key` flag with independent `write_genre / write_bpm / write_key / write_energy / write_mood / write_timeslot / write_direction / write_vocal` toggles (BPM & Key default **off** — Rekordbox's own detection is usually better). Each ML field can now be written independently; genre remains additionally gated by its confidence thresholds. Surfaced as a "Write these fields" grid in Settings. This is what makes non-genre tags writable independent of genre confidence — they were always computed independently, and the granular toggles make that explicit and controllable.

### Changed
- **BREAKING (config):** `TaggingConfig.skip_bpm_and_key` removed in favor of the `write_bpm` / `write_key` toggles. The RPC still accepts the legacy `skip_bpm_and_key` param for back-compat (maps to `write_bpm = not skip`); the CLI `tag` command maps `--skip-bpm-key` the same way.

---

## [0.4.0-beta.6] — 2026-05-18

### Added
- **Hybrid CPU + GPU analysis (work-stealing).** Previously analyze ran ONE device — either ~3 GPU workers (VRAM-capped) OR N CPU workers — so a modest GPU's low worker cap throttled throughput while most cores sat idle. Now, when a GPU is available and GPU mode isn't "off", the analyzer runs GPU workers (`CUDA_VISIBLE_DEVICES=0`) AND CPU workers (`=-1`) concurrently against a single shared work queue. The queue *is* the load balancer: whichever device finishes a track grabs the next, so fast and slow devices self-balance with no predictive scheduling. Total workers are bounded by RAM; the GPU subset by VRAM; CPU fills the rest. Per-device throughput (count + avg latency) is measured and reported. Verified on an RTX 4070 Laptop: a 50-track run split GPU 9 (17.5s/track) + CPU 41 (23.5s/track), using all resources. New `AnalysisConfig.hybrid_cpu_gpu` (default on), `--hybrid/--no-hybrid` CLI flag, and a **Settings toggle**. Worker recycling (process-exit every 200 tracks) and the stall watchdog / cancellation are preserved. Linux-CI hybrid-pool tests added.
- **Single global audio player.** Replaced the per-track embedded WaveSurfer (which kept playing after you navigated away, and let multiple previews sound at once) with one persistent player bar mounted at the app root. It survives tab/menu changes, always shows a stop control, and loading a new track stops the previous one (a single WaveSurfer instance — two previews can never overlap). `usePlayerStore` is the single source of truth; TrackDetails just calls `play(path, title)`. Removed the dead `AudioPreview` component.

### Fixed
- Audio playback: navigating away no longer leaves a track playing with no way to stop it; clicking a new track no longer stacks a second simultaneous preview.

---

## [0.4.0-beta.5] — 2026-05-18

Undo journal + the remaining audit LOW/informational fixes.

### Added
- **Operation undo journal** (`vibechek/journal.py`). `organize` and `dedupe` (move-to-review) now write an append-only JSONL journal — each completed move is recorded + flushed BEFORE the next, so a partial run (disk full, crash, power loss) is recoverable AND a finished operation can be reverted. New `revert_journal` moves files back to their origins (newest-first, never clobbering an occupied origin); `list_journals` powers an undo list. Trash entries are journaled for transparency but flagged non-revertible (send2trash → OS recycle bin has no reliable restore). New RPCs `list_journals` + `revert_journal` (+ typed TS wrappers), CLI `vibechek journals` / `vibechek revert <file>`, and `OrganizeStats.journal_path` / dedupe summary `journal_path`. 9 new journal tests.
- **Undo UI**: a "Recent operations" modal (sidebar entry) lists every organize / dedupe and offers one-click Undo (reverts via `revert_journal`); the Organize result screen gained an inline "Undo this organize" button; dedupe completion toasts now point to the undo surface (or the recycle bin for trash).
- **Wired the rest of the backend into the UI** — these shipped RPCs previously had no UI path:
  - **DJ profiles** picker in Settings (`list_profiles` / `load_profile`) — one-click presets.
  - **Copy diagnostic** (`doctor`), **Verify model integrity** (`verify_models`), and **Update WSL install** (`upgrade_vibechek_in_wsl`, the fast version-drift repair) buttons in a new Settings "Diagnostics & maintenance" section.
  - **Rename / tag** a recent library (`rename_library` / `tag_library`) via an inline editor on the recent-library cards.
- **Sidebar version** now reflects the real sidecar version (`version` RPC) instead of a hardcoded string that had drifted to "v0.3.0-dev".

### Fixed (audit LOW + informational)
- **Double `aggressive` model inference per track** — the Direction calc re-ran the most expensive ML head on the full embedding; now reuses the array already computed in the mood loop (measurable speedup on large libraries).
- **`find_audio_files` aborted the whole scan** on one unreadable entry (broken symlink, MAX_PATH, permission denied) → now skips the bad entry and continues.
- **`sanitize`/install fragility**: `_run_phase` reverse-parsed the staged inner-script path out of the launcher text (broke on paths with spaces, leaked tempfiles) → now captures the `Path` up front. `_resolve_cuda_packages` had an operator-precedence smell in cu12 detection → parenthesized. `repair_wsl_shim` decoded stdout as utf-8 only → now multi-encoding like the other WSL probes.
- **JSON-RPC**: failed *notifications* (no `id`) wrongly emitted an error response → now silent per spec. Sidecar shutdown sets the cancellation flag before pool teardown so an in-flight analyze/install can unwind instead of orphaning subprocesses.
- **CLI `export`** crashed on a malformed `analysis.json` with non-dict track entries → skips them.
- **M4A BPM restore** raised `ValueError` on a non-numeric backup value (`"128 BPM"`) and failed the whole file → coerces defensively.
- **keys.py**: an explicit-but-unknown mode (`"C dim"`) no longer silently resolves to major; `is_compatible_with` is now genuinely symmetric for the directional `energy-boost` mode (checks both directions).
- **genres.py**: removed dead "promote more specific subgenre" branch (the guard could never fire given descending sort).
- **config int coercion** accepts string/float forms uniformly (`int(round(float(v)))`) instead of truncating/raising inconsistently.
- **resources.py** `nvidia-smi` device probe now checks the exit code before parsing (NVML driver-mismatch errors).
- **Frontend**: Settings engine-GPU error unwraps `RpcError.message` instead of `String(e)` → no more `[object Object]`; `operation.fail` dropped the fragile `includes("cancelled by user")` substring heuristic (relies on the reliable `RpcError.cancelled`); Rust shell logs post-timeout late responses as expected-noise, not errors.
- Added a real (CI-runnable, synthetic-MP3-backed) **Rekordbox GEOB/PRIV preservation regression test** — guards the product's #1 feature against a tag write ever stripping cue points / beat grids, including across a double re-apply.

---

## [0.4.0-beta.4] — 2026-05-18

End-to-end codebase audit. Fixed all HIGH + MED findings.

### Fixed — data safety (HIGH)
- **Non-atomic JSON writes in the analyzer.** The final report write, the every-50-tracks checkpoint (`_write_partial`), and the WSL path-rewrite all used `Path.write_text(json.dumps(...))` — a kill/power-loss/disk-full mid-write truncated the report (up to 32 MB / 30+ min of GPU time). All now use `vibechek.io.atomic_write_json`. Checkpoint writes are also wrapped so a transient write error logs-and-continues instead of aborting the whole run.
- **Path traversal via genre tags** (`utils.sanitize_folder_name`). A track's existing genre tag (attacker-controlled on a downloaded file) flows into `organizer.route_new_tracks` as a destination folder; a genre of `..` or `../../Windows` escaped the library root. `sanitize_folder_name` now strips leading/trailing dots + separators and rejects `.`/`..` outright.
- **organize move could overwrite an existing file** (data loss). `shutil.move` overwrites silently. Two source files with the same basename routing to one genre folder both planned the same destination (neither existed on disk at plan time), and the second move clobbered the first. Fixed with an intra-batch `claimed` destination set in `plan_organization` plus an execute-time `_unique_destination` re-check that never overwrites.
- **`duplicates.save_report` non-atomic** → switched to `atomic_write_json` (the report drives destructive delete/move decisions).

### Fixed — correctness (HIGH/MED)
- **WSL installs used `setsid` without `-w`** (the documented fork-and-exit landmine) in `install_vibechek_in_wsl` and `install_cuda_libs_in_wsl` — the parent saw instant exit 0 while apt/pip ran orphaned, reporting "Install complete" before anything installed. Both now use `setsid -w` like the analyze path.
- **stderr pipe deadlock**: `run_vibechek_in_wsl` / `run_vibechek_in_native_venv` only drained stderr when an `on_stderr_line` callback was supplied; a verbose child filling the ~64KB stderr pipe buffer while the parent blocked on stdout would deadlock. stderr is now always drained on a background thread.
- **Two-stage tagger over-tagged legacy reports.** Re-applying a pre-`ml_genre_raw_confidence` analysis tagged ~30% more files with parent genres than the user saw when that report was the live behaviour. Stage 2 (parent fallback) is now disabled when `ml_genre_raw_confidence` is absent, matching the documented "legacy behaviour exactly."
- **Duplicate keeper selection** could keep a 0-byte corrupt file over real audio (format priority alone won) and was non-deterministic on ties. Now deprioritizes empty files and adds a path tiebreaker.
- **Cancellation ignored** in the duplicate trash/move loops and `organizer.route_new_tracks` — a Cancel mid-batch kept moving/copying files. All now check `cancellation.check()`.
- **`restore_tags_with_remap`** leaked raw `JSONDecodeError`/`KeyError` on a corrupt backup (the non-remap path was already hardened). Both now share `_load_backup_files` validation.
- **Truncated WSL/venv output** raised an opaque `UnicodeDecodeError` instead of the friendly "doesn't parse as JSON" message. Both analyze paths now read bytes once + decode with `errors="replace"`.
- **`nvidia-smi` device probe** parsed stdout without checking the exit code (NVML mismatch errors). Now bails on non-zero return.
- **CLI `analyze`** accepted negative `--workers`/`--skip`/`--limit` (e.g. `--skip -5` analyzed only the last 5 tracks). Now `click.IntRange(min=0)`.

### Fixed — frontend (HIGH/MED)
- **RPC sync guardrail was self-referential** — 7 Python methods (`rename_library`, `tag_library`, `count_new_tracks`, `doctor`, `verify_models`, `list_profiles`, `load_profile`) were missing from `RPC_METHODS` AND its hand-maintained test mirror, so the drift test stayed green while the TS wrappers didn't exist. Added all 7 (typed wrappers + param types), and added an authoritative cross-language check (`tests/test_rpc_method_sync.py`) that reads both `vibechek/rpc.py` and `ui/src/api/methods.ts` directly.
- **`track_analyzed` stale-event corruption**: a cancelled/superseded analyze kept streaming events that got merged as phantom tracks into a freshly-opened library. App.tsx now drops events whose path isn't under the current `libraryPath`.
- **`fail(String(e))` reintroduced** in LibraryBrowser's preflight catch — discarded the RpcError `cancelled` flag (user-cancel surfaced as an error toast). Reverted to `fail(e)`.
- **OrganizeView executed from live state, not the confirmed plan**: `currentParamsKey` only fingerprinted track *count*, so a content change (same count) left a stale plan looking valid before a destructive, no-undo move. Now includes a path+genre content fingerprint.
- **"Select all" ignored active filters** → selected (and could bulk-tag) hidden tracks. Now selects the filtered set via a new `selectPaths` store action.
- **DuplicatesView "space to free"** went stale after a rule reorder (used the backend's precomputed `recoverable_mb` instead of the rule-picked keeper). Now computes from `currentKeeper` with `rulesSig` in deps.
- **`scan_directory` mis-classed as a 60s QUICK op** in the Rust shell — timed out on large network shares. Moved to MEDIUM.
- Removed a `dangerouslySetInnerHTML` footgun in the Settings `Toggle` (rendered static labels as raw HTML).

---

## [0.4.0-beta.3] — 2026-05-18

### Fixed
- **WSL multi-track analyze silently exited 1.** Root cause: a WSL `vibechek` install older than the sidecar (worst case: v0.1.0-dev from the first setup) is missing the worker cap and stall watchdog, so the CLI dispatched `--workers 19` straight to essentia and the resulting 19 TF processes OOM-crashed in seconds. The bounded 80-line stderr tail filled with per-worker essentia `MusicExtractorSVM: no classifier models were configured by default` noise so the real error never made it back to the GUI.
  - `_analyze_via_wsl` now refuses to dispatch when the WSL vibechek version differs from the sidecar (`__version__`), surfacing a clear "WSL vibechek is out of date — re-run setup" error instead.
  - Stderr noise filter strips per-worker essentia INFO and TF GPU init chatter from the bounded tail buffer so genuine tracebacks and `VIBECHEK_WORKER_INIT_FAIL` markers survive.
  - `install_vibechek_in_wsl` now uses `pip install --upgrade` so re-running Set up WSL is an idempotent way to fix drift.
- **Tauri sidecar failed to load Python DLL at startup.** Root cause: PyInstaller's old `--onedir` mode produced an EXE that loaded a sibling `_internal/` folder containing `python3*.dll`, but Tauri 2's `externalBin` config is a single-file contract — it only copies the EXE to `target/<profile>/`, never `_internal/`. The dev sidecar died with `[PYI-xxxx:ERROR] Failed to load Python DLL`. The same gap silently broke macOS code-signing (Apple notarytool rejects bundles whose `_internal/.dylib`s are unsigned) and the Linux AppImage layout.
  - Switched `packaging/vibechek.spec` to `--onefile` mode. One self-contained binary per platform, one signing op per platform, no `_internal/` staging.
  - Rewrote `packaging/stage-sidecar.bat` and `packaging/stage-sidecar.sh` to copy that single file into both `ui/src-tauri/binaries/` (for Tauri's externalBin) and `ui/src-tauri/target/{debug,release}/` (for `cargo run` / `npm run tauri dev`).
  - Build scripts (`build-{windows.bat,macos.sh,linux.sh}`) updated for the new `dist/vibechek(.exe)` path (no `dist/vibechek/vibechek` subfolder).
  - `.github/workflows/release.yml` `sidecar_source` values updated to match.
  - Trade-off: cold sidecar startup is ~500ms slower (700ms vs 200ms — measured on Windows). Acceptable because the sidecar is a long-lived RPC server (one startup per app session, not per RPC call).

### Added
- New `upgrade_vibechek_in_wsl` RPC (Python) and `upgradeVibechekInWSL` TS wrapper. Fast-path re-install of just the vibechek package inside WSL (skips apt + essentia) — the one-click repair for the version-drift case.
- `DistroInfo.vibechek_version` populated by the WSL probe from `site-packages/vibechek-*.dist-info` so the GUI can show what's actually installed.
- `ui/src-tauri/entitlements.plist` with the standard PyInstaller-on-macOS hardened-runtime entitlements (`disable-library-validation`, `allow-jit`, `allow-unsigned-executable-memory`, `allow-dyld-environment-variables`). Wired into `tauri.conf.json` `bundle.macOS.entitlements` so `tauri-action`'s codesign picks them up automatically. Documented in `docs/RELEASING.md`.
- **Streaming progress + per-track results during analyze.** The GUI used to sit at "starting…" for 30-60 s while preflight + WSL boot + worker spawn ran with zero feedback. Now there's a structured event channel:
  - `vibechek/analyzer.py:_emit_event(type, **payload)` writes `VIBECHEK_EVENT\t<type>\t<json>` lines to stderr when `VIBECHEK_STREAM_PROGRESS=1` is set. Activated automatically inside the WSL launcher (`vibechek/wsl.py:run_vibechek_in_wsl`) and the managed-venv launcher (`vibechek/native_install.py:run_vibechek_in_native_venv`).
  - Events fired at every stage: `scanning`, `preflight`, `wsl_dispatch`, `venv_dispatch`, `analyzing`, `loading_models`, `spawning_workers`, plus one `track` event per completed file carrying the full ML record.
  - `_make_event_aware_line_handler` parses these out of subprocess stderr and routes them to the existing `on_progress` callback (stage events update the status message) AND a new `on_track` callback (per-track records).
  - New `track_analyzed` JSON-RPC notification (`vibechek/rpc.py:_emit_track_analyzed`) → Rust shell re-emits as `sidecar:track_analyzed` Tauri event → frontend's `App.tsx` merges each record into `useLibraryStore.tracks` in real time via the new `mergeAnalyzedTrack(record)` action.
  - 9 new regression tests in `tests/test_analyzer_event_stream.py` cover the emitter, the parser, the noise filter, and exception silencing.
- **Audio preview fixes:**
  - WSL-streamed `track_analyzed` events carried POSIX paths (`/mnt/c/...`) so Tauri's `convertFileSrc()` produced broken `asset://` URLs and the AudioPreview waveform refused to load. `_analyze_via_wsl` now wraps `on_track` to translate paths back to Windows form before forwarding (mirrors the existing post-analyze translation on the report's `tracks` array).
  - `TrackDetails` was rendered outside the viewMode switch in `App.tsx`, so AudioPreview (and its WaveSurfer instance) stayed mounted across every tab — playback continued after navigating to Duplicates / Organize / etc. with no visible control. Gated `<TrackDetails />` on `viewMode === "library"` so the panel unmounts on tab change and WaveSurfer's destroy() stops playback.
  - AudioPreview's error display used `truncate` (single-line ellipsis), hiding the actually-useful part of long error messages like "Could not decode audio: …". Switched to wrap + `whitespace-pre-wrap` + scrollable max-height so the full error is readable inline.
- **GPU worker cap re-tuned to prevent mid-analyze stalls.** Bumped `_GPU_WORKER_MB` from 1500 to 2500 MB after the user hit a 5-min stall watchdog with workers=5 (the old cap on an RTX 4070 Laptop 8 GB). Empirical testing: 5 workers stalled after ~12 tracks (TF growth-allocator fragmentation pushed each worker's footprint past 1500 MB), 4 workers stalled at startup under contention, 3 workers ran cleanly to completion. The new 2500-MB budget reflects steady-state per-worker memory including CUDA context + model graphs + activation buffers + fragmentation overhead. Trade-off: a 24-GB card now gets 9 workers (was 13), but the previous cap was producing stall-watchdog errors with no useful diagnostic — the worst possible failure mode. We prefer "always finishes" over "sometimes faster".
- **Cap message now actionable.** Previously: "Capped workers from 19 to 5 due to 7 GB free VRAM". Now: `"Capped workers from 19 to 3 (7 GB free VRAM (~2500 MB per worker)). Set 'GPU mode' to 'off' in Settings to use your full 17-worker CPU budget instead."` — surfaces the tradeoff and the override path. Emitted on both the legacy progress channel AND the structured `worker_cap` stage event for the GUI overlay.

### Added
- `vibechek doctor` CLI command — prints a complete environment report for bug reports (Python version, OS, sidecar location, venv / WSL status, GPU detection, recent log tail).
- Typed RPC wrapper `ui/src/api/rpc.ts` — every UI call now goes through a single typed surface backed by `ui/src/types/generated.ts`.
- CI: CodeQL workflow for Python and TypeScript, weekly + on PR.
- CI: enforcing lint on the main matrix once cleanup is done; advisory `lint-strict` job in the meantime.
- CI: `tsc --noEmit` and `npm run build` checks in the frontend job.
- CI: Python coverage report uploaded as a per-run artifact.
- CI: README stats freshness check (fails the build if `README.md` is out of date relative to the code).
- Dependabot: weekly grouped PRs for pip, npm, cargo, github-actions.
- Issue templates, PR template, contributing guide, code of conduct, security policy, funding manifest.
- Intel Mac (`macos-13`) build in the release matrix alongside the existing Apple Silicon job.
- Public bus-factor doc ([docs/MAINTAINERS.md](docs/MAINTAINERS.md)) and contracts walkthrough ([docs/CONTRACTS.md](docs/CONTRACTS.md)).
- Competitor citations ([docs/COMPETITORS.md](docs/COMPETITORS.md)) for every claim in the README comparison table.

### Changed
- README: headline now leads with Rekordbox cue preservation (was buried six paragraphs in). Stats line is auto-regenerated by `scripts/update_readme_stats.py`.

---

## [0.3.0-beta.11] — 2026-05-17

Beta cycle wrap-up. Eleventh beta exists because early user feedback surfaced real edge cases faster than any test matrix could.

### Fixed
- Long-tail stability fixes from public beta feedback.
- Improved error surfacing when the sidecar dies mid-operation.

### Changed
- Final docs polish ahead of v0.3.0 stable.

---

## [0.3.0-beta.10] — 2026-05-12

### Fixed
- Race condition between cancellation and progress reporting on very fast operations.
- Tag backup history corruption when two libraries shared a backup directory.

---

## [0.3.0-beta.9] — 2026-05-06

### Added
- Per-library auto-save of the last analysis so re-opening the app restores state immediately.

### Fixed
- Library state index growing unbounded across sessions.

---

## [0.3.0-beta.8] — 2026-04-29

### Fixed
- Windows-only: WSL shim repair when the user upgraded WSL outside the app.
- Subgenre fallback when Discogs-EffNet returns a low-confidence top label.

---

## [0.3.0-beta.7] — 2026-04-21

### Added
- `native_venv_status` RPC method so the UI can show macOS / Linux venv state without spawning a subprocess on every poll.

### Changed
- Tightened error codes returned by the JSON-RPC layer so the UI can branch on `INVALID_REQUEST` vs `APP_ERROR` cleanly.

---

## [0.3.0-beta.6] — 2026-04-14

### Fixed
- Organizer dry-run plan now correctly previews path collisions before the user commits.
- Chromaprint dedupe no longer panics on zero-length files.

---

## [0.3.0-beta.5] — 2026-04-07

### Added
- Backup history view: every snapshot is timestamped and restorable.
- `forget_backup` RPC method to prune stale entries from the history.

---

## [0.3.0-beta.4] — 2026-03-31

### Fixed
- `__TRY_CHAIN__` self-substitution bug in the WSL bootstrap shell wrapper.
- NVIDIA repo sanity check before attempting CUDA install (prevents adding a broken apt source).

---

## [0.3.0-beta.3] — 2026-03-24

### Fixed
- CUDA install regression: keyring placement on Ubuntu 22.04 vs 24.04.

---

## [0.3.0-beta.2] — 2026-03-17

### Fixed
- Cross-platform GUI install resilience — the Preflight dialog now correctly recovers from a half-installed venv.
- Improved CUDA install logging so the failure mode is visible without trawling WSL logs.

---

## [0.3.0-beta.1] — 2026-03-10

First public beta. Feature-complete, headed for stable.

### Added
- End-to-end ML pipeline: genre + subgenre across ~400 Discogs categories, BPM, key, energy 0-5, mood, timeslot, direction, vocal type, danceability.
- Acoustic duplicate detection via Chromaprint (MD5 fallback for byte-identical files).
- One-click organize into `Genre/Subgenre/` tree with dry-run preview.
- Full tag backup / restore including binary GEOB and PRIV frames (Rekordbox-safe).
- Cross-platform GPU detection that probes the actual analysis engine, not just the host.
- Tauri desktop app with five tabs: Library, Duplicates, Organize, Tags, Settings.
- CLI parity: every GUI action is a `vibechek` subcommand.
- Windows auto-install of WSL Ubuntu + Essentia inside it, with transparent path translation.
- macOS / Linux hermetic venv at `~/.vibechek/venv/` so the system Python is never touched.
- JSON-RPC stdin/stdout bridge between the Tauri shell and the Python sidecar.
- Auto-generated TypeScript types mirroring Python dataclasses.
- Cross-platform CI release pipeline producing signed (when secrets are configured) installers.

---

[Unreleased]: https://github.com/papapew/Vibechek/compare/v0.3.0-beta.11...HEAD
[0.3.0-beta.11]: https://github.com/papapew/Vibechek/compare/v0.3.0-beta.10...v0.3.0-beta.11
[0.3.0-beta.10]: https://github.com/papapew/Vibechek/compare/v0.3.0-beta.9...v0.3.0-beta.10
[0.3.0-beta.9]: https://github.com/papapew/Vibechek/compare/v0.3.0-beta.8...v0.3.0-beta.9
[0.3.0-beta.8]: https://github.com/papapew/Vibechek/compare/v0.3.0-beta.7...v0.3.0-beta.8
[0.3.0-beta.7]: https://github.com/papapew/Vibechek/compare/v0.3.0-beta.6...v0.3.0-beta.7
[0.3.0-beta.6]: https://github.com/papapew/Vibechek/compare/v0.3.0-beta.5...v0.3.0-beta.6
[0.3.0-beta.5]: https://github.com/papapew/Vibechek/compare/v0.3.0-beta.4...v0.3.0-beta.5
[0.3.0-beta.4]: https://github.com/papapew/Vibechek/compare/v0.3.0-beta.3...v0.3.0-beta.4
[0.3.0-beta.3]: https://github.com/papapew/Vibechek/compare/v0.3.0-beta.2...v0.3.0-beta.3
[0.3.0-beta.2]: https://github.com/papapew/Vibechek/compare/v0.3.0-beta.1...v0.3.0-beta.2
[0.3.0-beta.1]: https://github.com/papapew/Vibechek/releases/tag/v0.3.0-beta.1
