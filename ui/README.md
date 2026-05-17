# Vibechek Desktop UI

Tauri 2.x shell + React frontend that drives the Python core through a JSON-RPC sidecar.

```
ui/
├── src/                  React frontend (Vite, TypeScript, Tailwind, Zustand)
│   ├── components/       UI components (Sidebar, LibraryBrowser, ...)
│   ├── stores/           Zustand global state
│   ├── hooks/            useSidecar — RPC + event subscription
│   └── types/            TypeScript mirrors of Python dataclasses
├── src-tauri/            Rust shell
│   ├── src/
│   │   ├── lib.rs        Tauri setup
│   │   ├── sidecar.rs    Spawns `vibechek rpc`, multiplexes IPC
│   │   └── commands.rs   Tauri commands exposed to the frontend
│   ├── capabilities/     Tauri 2.x permissions
│   └── tauri.conf.json   App config
└── package.json
```

## Architecture

```
┌────────────────────────────────────────────────────────┐
│  React frontend (Vite)                                 │
│  ─ Zustand stores, virtualized track list, settings UI │
└──────────────────────────┬─────────────────────────────┘
                           │ Tauri invoke("rpc_call", method, params)
                           ▼
┌────────────────────────────────────────────────────────┐
│  Rust shell (Tauri 2.x)                                │
│  ─ spawns the sidecar                                  │
│  ─ multiplexes JSON-RPC requests by id                 │
│  ─ re-emits progress notifications as `sidecar:*` evts │
└──────────────────────────┬─────────────────────────────┘
                           │ JSON-RPC over stdin/stdout
                           ▼
┌────────────────────────────────────────────────────────┐
│  `vibechek rpc` (Python sidecar)                       │
│  ─ same package the CLI uses                           │
│  ─ exposes analyzer, tagger, dedupe, organize, ...     │
└────────────────────────────────────────────────────────┘
```

## Prerequisites

- Node.js 20+
- Rust 1.77+ (via [rustup.rs](https://rustup.rs))
- Tauri 2.x system prerequisites — see [Tauri's install guide](https://v2.tauri.app/start/prerequisites/)
- The Vibechek Python package installed (so the sidecar binary exists)

For dev mode, the easiest setup is:

```bash
# From the repo root:
python -m venv .venv
. .venv/Scripts/activate           # Windows
# source .venv/bin/activate         # macOS/Linux
pip install -e .

cd ui
npm install
```

## Dev mode

### One-time setup: stage the sidecar binary

Tauri's `externalBin` config validates that the sidecar binary exists even in
dev mode. Run this once after your first `packaging/build-windows.bat`
(or build-macos / build-linux):

```powershell
# Windows
packaging\stage-sidecar.bat

# macOS / Linux
./packaging/stage-sidecar.sh
```

This copies `dist/vibechek/vibechek(.exe)` to
`ui/src-tauri/binaries/vibechek-sidecar-<target-triple>(.exe)`, the path
Tauri expects. You only need to re-run it when the PyInstaller build is
regenerated. The binaries dir is gitignored.

### Run

The sidecar that *actually runs* at dev time comes from `VIBECHEK_SIDECAR`,
which lets you point at your editable Python install (instant code changes,
no rebuild needed):

### Windows (PowerShell)

```powershell
$env:VIBECHEK_SIDECAR = "$pwd\..\.venv\Scripts\vibechek.exe"
npm run tauri:dev
```

### macOS / Linux

```bash
VIBECHEK_SIDECAR="$PWD/../.venv/bin/vibechek" npm run tauri:dev
```

This launches Vite for the frontend on port 5173 and Tauri starts the desktop shell pointing at it. Edits to React files hot-reload automatically; Rust edits trigger a Tauri rebuild.

## Production build

```bash
# 1. Build the Python sidecar binary (from the repo root)
packaging\build-windows.bat                 # or build-macos.sh / build-linux.sh

# 2. Copy the sidecar binary into Tauri's binaries/ folder, named with its
#    platform triple suffix (Tauri's externalBin convention):
#
#    Windows: vibechek-sidecar-x86_64-pc-windows-msvc.exe
#    macOS:   vibechek-sidecar-aarch64-apple-darwin
#    Linux:   vibechek-sidecar-x86_64-unknown-linux-gnu
mkdir -p ui/src-tauri/binaries
cp dist/vibechek/vibechek.exe ui/src-tauri/binaries/vibechek-sidecar-x86_64-pc-windows-msvc.exe

# 3. Build the Tauri app
cd ui
npm install
npm run tauri:build
```

Output lands in `ui/src-tauri/target/release/bundle/` — `.msi` and `.exe` installers on Windows, `.dmg` on macOS, `.AppImage` and `.deb` on Linux.

## Sidecar protocol

See [`vibechek/rpc.py`](../vibechek/rpc.py) for the full method list. Quick reference:

| Method | What |
|---|---|
| `ping` | health check |
| `scan_directory` | list audio files (no ML) |
| `analyze_directory` | full ML analysis with progress |
| `find_duplicates` | MD5 + Chromaprint scan |
| `handle_duplicates` | move / trash flagged dupes |
| `plan_organization` | preview a genre-folder reshuffle |
| `organize` | execute the reshuffle |
| `apply_ml_tags` | write ML results to file tags |
| `backup_tags` / `restore_tags` | snapshot + restore |
| `download_models` | grab the ~800 MB Essentia models |
| `get_config` | default config payload |

All long-running methods emit `progress` notifications:
```json
{"jsonrpc":"2.0","method":"progress","params":{"current":50,"total":100,"message":"..."}}
```
The Rust shell re-broadcasts these as Tauri events on channel `sidecar:progress`.

## Troubleshooting

**"failed to spawn sidecar"** in dev — `VIBECHEK_SIDECAR` isn't set, or points at a path that doesn't exist. Echo it in the shell before `npm run tauri:dev` to confirm.

**"sidecar exited with status …"** at startup — your sidecar binary may be failing on import. Run it manually:
```bash
$VIBECHEK_SIDECAR rpc
# then send: {"jsonrpc":"2.0","id":1,"method":"ping"}
```
Anything it writes to stderr appears in the Tauri dev console prefixed with `[sidecar]`.

**Frontend changes don't appear** — Vite's HMR may be blocked by Tauri's WebView cache. Hit Ctrl+R inside the window to hard-reload.
