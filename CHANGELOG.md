# Changelog

All notable changes to Vibechek are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Pre-release tags use the form `vMAJOR.MINOR.PATCH-beta.N` (git tag) which maps to `MAJOR.MINOR.PATCHbN` in PEP 440 (`pyproject.toml`). See [docs/RELEASING.md](docs/RELEASING.md).

---

## [Unreleased]

Targeted for `v0.4.0`. Items move out of this section once they ship in a tagged release.

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

Beta cycle wrap-up. Eleventh beta exists because the public launch on Reddit surfaced real edge cases faster than any test matrix could.

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
