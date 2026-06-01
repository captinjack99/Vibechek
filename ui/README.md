# Vibechek Desktop UI

Tauri 2.x shell + React frontend that drives the Python core through a JSON-RPC sidecar.

```
ui/
├── src/                  React frontend (Vite, TypeScript, Tailwind, Zustand)
│   ├── components/       UI components (Sidebar, LibraryBrowser, TagsView, Settings, …)
│   ├── stores/           Zustand global state (library, operation, UI, config, notification)
│   ├── hooks/            useSidecar, useApplyTags, useConfigPersistence, useUpdater
│   ├── lib/              keeperRules — auto-pick logic for duplicate keepers
│   ├── types/            generated.ts (auto-mirrored from Python) + index.ts (view-only types)
│   └── test/             vitest setup with Tauri mocks
├── src-tauri/            Rust shell
│   ├── src/
│   │   ├── lib.rs        Tauri setup
│   │   ├── sidecar.rs    Spawns vibechek rpc, multiplexes IPC, detects sidecar death
│   │   └── commands.rs   Tauri commands exposed to the frontend
│   ├── capabilities/     Tauri 2.x permissions
│   ├── icons/            App icons (generated from packaging/generate-icons.py)
│   ├── binaries/         (gitignored) PyInstaller-built sidecar staged here for dev/release
│   └── tauri.conf.json
├── package.json
├── vite.config.ts
├── vitest.config.ts      Jsdom env, Tauri API mocks
└── tsconfig.json
```

## Architecture

```
┌────────────────────────────────────────────────────────┐
│  React frontend (Vite)                                 │
│  - Zustand stores, virtualized track list, settings UI │
│  - 38 vitest tests (Tauri APIs mocked in setup)        │
└──────────────────────────┬─────────────────────────────┘
                           │ Tauri invoke("rpc_call", method, params)
                           ▼
┌────────────────────────────────────────────────────────┐
│  Rust shell (Tauri 2.x)                                │
│  - Spawns the Python sidecar                           │
│  - Multiplexes JSON-RPC requests by id                 │
│  - Re-emits `progress` notifications as Tauri events   │
│  - Detects sidecar death (EOF on stdout) and fails    │
│    pending requests immediately instead of timing out  │
└──────────────────────────┬─────────────────────────────┘
                           │ JSON-RPC over stdin/stdout
                           ▼
┌────────────────────────────────────────────────────────┐
│  `vibechek rpc` (Python sidecar)                       │
│  - 44 RPC methods, threadpool dispatch (8 workers)     │
│  - Stdout lock keeps concurrent JSON frames atomic     │
│  - Cooperative cancellation token                      │
│  - All real work: analyzer, tagger, dedup, organize    │
└────────────────────────────────────────────────────────┘
```

The Python sidecar is the same package the CLI uses (`vibechek rpc`
subcommand). Threadpool dispatch means fast endpoints — `get_config`,
`system_info`, `preflight` — interleave with long-running ones, so the GUI
never freezes waiting for a 2-hour analyze.

## Components

| Component | Purpose |
|---|---|
| `App.tsx` | Shell + view router + global mounts (ErrorToast, Toast, AnalysisProgress, TrackDetails, Onboarding) |
| `Sidebar.tsx` | Library / Duplicates / Organize / Tags / Settings nav with consistent badges |
| `LibraryBrowser.tsx` | Virtualized track list, search filter, bulk select, "Apply ML tags to N", error badge, recent-libraries empty state |
| `LibraryFilters.tsx` | Genre / energy / mood / vocal chip filters with controlled popovers (proper outside-click handling) |
| `TrackDetails.tsx` | Side panel — file metadata, before/after tag diff; triggers the global player |
| `GlobalAudioPlayer.tsx` | Single persistent WaveSurfer.js player bar at the app root (via Tauri asset protocol). Survives navigation; only one preview ever plays; every track starts at 0:00 |
| `DuplicatesView.tsx` | Per-group rules editor, auto-keeper picks, manual override, action bar |
| `OrganizeView.tsx` | Source picker, rules, plan preview, polished confirm, post-op result panel with folder breakdown + inline Undo |
| `OperationsHistory.tsx` | "Recent operations" modal — lists past organize/dedupe runs (from the journal) with one-click revert |
| `TagsView.tsx` | Backup, restore, history of past backups with stale warnings |
| `Settings.tsx` | Preflight banner, System resources, Analysis (workers/GPU/hybrid), Tagging (per-field write grid + vocal sensitivity), DJ profiles, diagnostics (doctor/verify-models/update-WSL), **Software updates** (check → download → install → relaunch via `useUpdater`), Advanced disclosure, Restore defaults, View logs |
| `ConfirmModal.tsx` | Reusable confirm dialog — replaces every `window.confirm` |
| `Toast.tsx` | Bottom-right success/info notification stack |
| `ErrorToast.tsx` | Top-center error banner with Copy details / View logs / Report on GitHub |
| `LogsViewer.tsx` | Full log tail viewer with level filter, copy-all, refresh |
| `Onboarding.tsx` | First-launch three-slide tour, persisted in JSON config |
| `PreflightDialog.tsx` | Setup walkthrough with one-click WSL install / Essentia install / model download |
| `AnalysisProgress.tsx` | Floating progress overlay with Cancel button |
| `TagBadges.tsx` | Reusable energy bar + tag pill |

## Hooks

| Hook | Purpose |
|---|---|
| `useSidecar.ts` | `rpc(method, params)` Promise wrapper + `useSidecarProgress` event subscription |
| `useApplyTags.ts` | Shared "apply ML tags to N tracks" logic used by LibraryBrowser bulk + TrackDetails per-file |
| `useConfigPersistence.ts` | One-shot load on mount, debounced auto-save (500ms) on every config change |
| `useUpdater.ts` | In-app auto-updater state machine (idle → checking → available → downloading → installing → relaunch). Wraps `@tauri-apps/plugin-updater` + `@tauri-apps/plugin-process`, lazy-imported and guarded by `isTauri()` so it no-ops cleanly in the dev browser / tests |

## Stores

Small Zustand stores, no cross-dependencies (split into one file each under `stores/`,
re-exported from `stores/index.ts`):

- `useLibraryStore` — tracks, libraryPath, selectedIds, searchFilter
- `useOperationStore` — active op, progress, error, duplicateReport, organizePlan, clearError
- `useUIStore` — viewMode, sidebarCollapsed, selectedTrackPath
- `useConfigStore` — config (mirrors `VibechekConfig` from Python), loaded flag. New backend fields flow through automatically: e.g. `AnalysisConfig.inference_engine` (`"essentia_tf"` | `"onnx"`, opt-in ONNX) is mirrored into the generated TS types (`types/generated.ts`)
- `useNotificationStore` — toast queue with notify/dismiss
- `usePlayerStore` — single source of truth for the global audio player (current track, play/stop/position)

## Prerequisites for development

- **Node.js 20+** with npm
- **Rust 1.77+** via [rustup.rs](https://rustup.rs/)
- **Tauri 2.x system deps**: [v2.tauri.app/start/prerequisites](https://v2.tauri.app/start/prerequisites/)
- **Vibechek Python package** installed in editable mode (`pip install -e .` from repo root)

## Dev workflow

```bash
# From repo root, set up Python first
python -m venv .venv
. .venv/Scripts/activate     # Windows
pip install -e ".[dev]"

# Build the PyInstaller sidecar binary (one-time, or when Python changes a lot)
packaging\build-windows.bat
packaging\stage-sidecar.bat  # Copies the binary where Tauri expects it

# Install UI deps
cd ui
npm install

# Set sidecar path for dev (lets you edit Python live, no rebuild)
$env:VIBECHEK_SIDECAR = "$pwd\..\.venv\Scripts\vibechek.exe"   # Windows
# export VIBECHEK_SIDECAR="$PWD/../.venv/bin/vibechek"          # macOS / Linux

npm run tauri:dev
```

First Tauri compile is 10-15 minutes (downloads + builds ~400 MB of crates). Subsequent runs are fast.

## Tests

```bash
cd ui
npm test           # vitest run — 38 tests
npm run test:watch # watch mode
npm run test:ui    # web UI for tests
```

Tests cover:
- `lib/keeperRules.test.ts` — pure auto-picker logic
- `api/rpc.test.ts` — JSON-RPC client wrappers + error mapping
- `components/LibraryFilters.test.tsx` — filter rendering + applyFilters
- `components/ConfirmModal.test.tsx` — modal behavior
- `components/DuplicatesView.test.tsx` — dedupe group rules + keeper override
- `components/Sidebar.test.tsx` — nav + viewMode

Tauri APIs are globally mocked in `src/test/setup.ts` so components that call `invoke`/`open`/etc. don't crash under jsdom.

## Production build

```bash
# Build CLI sidecar for the target OS
packaging\build-windows.bat               # or build-macos.sh / build-linux.sh
packaging\stage-sidecar.bat               # copy to ui/src-tauri/binaries/

# Build the Tauri app
cd ui
npm install
npm run tauri:build
```

Output: `ui/src-tauri/target/release/bundle/`:
- Windows: `.exe` (NSIS installer)
- macOS: `.dmg` (Apple Silicon; unsigned in beta — Gatekeeper bypass documented in the release notes)
- Linux: `.AppImage` + `.deb`

(`bundle.targets` in `tauri.conf.json` pins these; MSI/RPM were dropped — MSI rejects
non-numeric pre-release versions and RPM needs `rpmbuild`, absent on the runners.)

The CI workflow at `.github/workflows/release.yml` does this automatically on tag push
(`v*`). Code signing is opt-in: the `Configure code signing (opt-in)` step only enables
signing when cert secrets are present, otherwise it builds unsigned.

## Auto-updater

The "Software updates" section in Settings drives the in-app updater (`useUpdater.ts`):
check → download → install → relaunch. It's backed by `tauri-plugin-updater` (downloads
and verifies the public-key-signed `latest.json`) plus `tauri-plugin-process` (relaunches
the app to apply the staged update). Both are scoped in `capabilities/default.json` via the
`updater:default` and `process:default` permissions.

`createUpdaterArtifacts` ships **`false`** in `tauri.conf.json` — updater signing is opt-in.
CI only produces signed update artifacts + `latest.json` once a signing key is configured;
beta builds ship without them (the updater is inert until a key is enrolled). See
[`docs/RELEASING.md`](../docs/RELEASING.md).

## Sidecar protocol

JSON-RPC 2.0, one message per line on stdin/stdout. 44 methods. See [`vibechek/rpc.py`](../vibechek/rpc.py) for the authoritative list.

| Method | What |
|---|---|
| `ping`, `version` | Health check |
| `system_info`, `engine_gpu_status` | CPU/RAM/GPU (host) + actual-engine GPU probe |
| `preflight`, `wsl_status`, `doctor`, `verify_models`, `native_venv_status` | Readiness checks + diagnostics |
| `install_wsl`, `install_vibechek_in_wsl`, `install_cuda_libs_in_wsl`, `install_essentia_native`, `upgrade_vibechek_in_wsl`, `repair_wsl_shim` | Auto-setup / GPU / version-drift repair |
| `scan_directory`, `scan_only`, `count_new_tracks` | List files / shallow track records (no ML) |
| `analyze_directory` | Full ML pass (CPU/GPU/hybrid; supports `skip_paths` for incremental) |
| `find_duplicates`, `handle_duplicates` | Dedup scan + execute |
| `plan_organization`, `organize` | Genre-folder reorganization |
| `list_journals`, `revert_journal` | Operation undo (organize/dedupe) |
| `apply_ml_tags` | Write ML results to file tags (per-field toggles + vocal cutoffs) |
| `backup_tags`, `restore_tags`, `restore_tags_with_remap` | Snapshot + replay |
| `rename_library`, `tag_library` | Recent-library maintenance |
| `list_profiles`, `load_profile` | DJ setting presets |
| `download_models` | Pull Essentia models |
| `get_config`, `save_config`, `restore_default_config` | JSON config persistence |
| `library_state`, `forget_library`, `load_recent_analysis` | Recent-libraries |
| `backup_history`, `forget_backup` | Tag backup history |
| `get_log_tail` | Logs for LogsViewer |
| `cancel_operation` | Stop the currently running long op |

All long ops emit `progress` notifications:
```json
{"jsonrpc":"2.0","method":"progress","params":{"current":50,"total":100,"message":"..."}}
```
The Rust shell re-broadcasts these as Tauri events on channel `sidecar:progress`.

## Troubleshooting

**"failed to spawn sidecar"** in dev — `VIBECHEK_SIDECAR` not set, or path wrong. Echo it before `npm run tauri:dev` to confirm.

**Sidecar exits immediately** — Run it manually:
```bash
$VIBECHEK_SIDECAR rpc
# Type: {"jsonrpc":"2.0","id":1,"method":"ping"}
```
Anything it writes to stderr appears in the Tauri dev console prefixed with `[sidecar]`.

**Audio preview shows "Could not load audio"** — The Tauri asset protocol is gated by CSP. Confirm `tauri.conf.json` has `assetProtocol.enable: true` and the CSP includes `media-src 'self' asset: https://asset.localhost blob:`.

**npm install fails with ERESOLVE for plugin-react / vite** — `@vitejs/plugin-react@4.x` peer dep is `vite ^4 || ^5 || ^6 || ^7`. Don't bump Vite past 7 until plugin-react v5 ships.

**Frontend changes don't appear** — Vite HMR can get confused by Tauri's WebView cache. Ctrl+R inside the window to hard-reload.
