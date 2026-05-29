# Maintainers / bus-factor notes

The stuff you'd otherwise have to learn the hard way. If you're picking this project up
cold, read this once. For the contributor workflow see [CONTRIBUTING.md](../CONTRIBUTING.md);
for the release mechanics see [RELEASING.md](RELEASING.md).

## The five version manifests must agree

A release bumps **five** files. They must all match, or the version-drift guard and CI
will complain:

1. `vibechek/__init__.py` — `__version__` (e.g. `"0.4.0-beta.7"`)
2. `pyproject.toml` — PEP 440 form (`0.4.0b7`)
3. `ui/src-tauri/Cargo.toml`
4. `ui/src-tauri/tauri.conf.json`
5. `ui/package.json`

The git tag form is `vMAJOR.MINOR.PATCH-beta.N`; PEP 440 maps that to `…bN`.

## Release flow (short version)

1. Bump the five manifests, update `CHANGELOG.md`, run `python scripts/update_readme_stats.py`.
2. Commit, then `git tag vX.Y.Z-beta.N`, then `git push origin main && git push origin <tag>`.
3. The tag push triggers [`.github/workflows/release.yml`](../.github/workflows/release.yml):
   builds the PyInstaller CLI + the Tauri installers per OS, drafts a GitHub Release.
4. Review the draft, then publish.

Full detail (signing, retag, troubleshooting) is in [RELEASING.md](RELEASING.md).

## Architecture landmines (the non-obvious ones)

- **Windows analysis runs in WSL.** Essentia has no Windows wheel, so `analyze` routes
  through `vibechek` installed in a WSL venv (`~/.vibechek/venv/`). Paths translate
  `C:\…` ↔ `/mnt/c/…` and that translation lives **only** in `vibechek/wsl.py` — the
  frontend never sees `/mnt/c`.
- **Version-drift guard.** The analyzer refuses to dispatch to WSL when the WSL install's
  `__version__` ≠ the sidecar's. A stale WSL install is the #1 "analyze silently fails"
  cause. The fix is the one-click "Update WSL install" (`upgrade_vibechek_in_wsl`) or
  re-running setup (which `pip install --upgrade`s).
- **`setsid -w` in the WSL launchers.** Both the install path and the run path wrap the
  WSL process in `setsid -w` so cancellation can kill the whole process group, AND so the
  parent waits instead of fork-and-exit. Dropping the `-w` makes the GUI report "done"
  while apt/pip/analyze keep running orphaned — a landmine that has regressed before.
- **Console-script vs `python -c`.** The production sidecar runs the venv console script
  (`venv/bin/vibechek`), whose `sys.path[0]` is `venv/bin`. Ad-hoc `python -c`/`-m` probes
  add the *cwd* to `sys.path`, so if cwd contains a `vibechek/` package (e.g. a stale git
  worktree), the probe imports the wrong code. Probes must `cd "$HOME"` first.
- **PyInstaller `--onefile`.** The sidecar is one self-contained binary per platform
  (`packaging/vibechek.spec`). `--onedir` broke Tauri's single-file `externalBin` contract.
  On Windows, `--clean` can hit a `PermissionError` removing `build/…/localpycs` when the
  repo lives on a file-syncing or networked drive (e.g. a cloud-sync folder); rebuild
  without `--clean`.
- **GEOB/PRIV preservation is the product.** Every tag write must capture and restore
  Rekordbox's binary GEOB/PRIV frames (cue points, beat grids). This is guarded by a
  synthetic-MP3 regression test — never let a refactor strip it.
- **Config is JSON, not TOML.** Since 0.3.0. A pre-0.3.0 `config.toml` is read once as a
  migration and rewritten as `config.json`. Unknown keys are dropped on load, so adding
  fields is backwards-safe.

## CI gotchas

- **`tsc --noEmit` + `npm run build` are enforcing.** They were advisory once and hid a
  broken build (the `ui/src/lib` gitignore incident). Keep them enforcing.
- **`.gitignore` `lib/` foot-gun.** A bare `lib/` pattern (meant for Python) also matched
  `ui/src/lib/`, so generated frontend files never got committed and fresh clones failed
  `tsc`. It's anchored to `/lib/` now — keep it anchored.
- **Bundle targets are pinned** in `tauri.conf.json` (`nsis`, `deb`, `appimage`, `dmg`,
  `app`). MSI rejects non-numeric pre-release versions; RPM needs `rpmbuild` (absent on the
  runners). Don't re-add them without handling those.
- **Code signing is opt-in.** The `Configure code signing (opt-in)` step in `release.yml`
  exports cert env vars only when the secret is non-empty. This is mandatory: Tauri 2's
  bundler reads `APPLE_CERTIFICATE` via `std::env::var()` → `Ok("")` for a defined-but-empty
  var, so an unset GitHub secret (which injects `""`) would otherwise make it try
  `security import` on empty data and hard-fail the macOS bundle. Never put `APPLE_*` /
  `WINDOWS_*` secrets directly in the build step's `env:`.
- **README stats freshness check.** CI fails if `README.md`'s stats line is stale relative
  to the code. Run `python scripts/update_readme_stats.py` after adding tests/RPCs/modules.

## Debugging WSL (Windows)

- `wsl_status` / `doctor` RPCs report what's installed where.
- Per-distro probe reads `__version__` from `site-packages/vibechek-*.dist-info`.
- To inspect by hand: `wsl -e bash -lic 'cd "$HOME"; ~/.vibechek/venv/bin/vibechek --version'`.
- Distro reset wipes `~/.local/share` — the ML models live there and will need re-download.

## Where things are stored (per user, via platformdirs)

| What | Location key |
|---|---|
| Config | `<config_dir>/Vibechek/config.json` |
| Recent libraries | `<config_dir>/Vibechek/library_state.json` |
| Backup history | `<config_dir>/Vibechek/backup_history.json` |
| ML models (~800 MB) | `<data_dir>/Vibechek/models/` |
| Auto-saved analyses | `<data_dir>/Vibechek/analyses/` |
| Operation journals | `<data_dir>/Vibechek/journals/` |
| Logs | `<data_dir>/Vibechek/logs/vibechek.log` |
