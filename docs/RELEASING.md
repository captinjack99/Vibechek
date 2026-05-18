# Releasing Vibechek

How a new release happens, end-to-end. Most of the heavy lifting is automated; the human pieces are version bumps, the tag, and the final "publish" click on GitHub.

---

## TL;DR — cutting a beta or release

```bash
# 1. Bump all four version strings (must match the git tag exactly):
#    - vibechek/__init__.py        __version__
#    - pyproject.toml              project.version  (PEP 440 form: 0.3.0b1)
#    - ui/package.json             version
#    - ui/src-tauri/Cargo.toml     package.version
#    - ui/src-tauri/tauri.conf.json version
git add -A && git commit -m "chore: bump to v0.3.0-beta.1"

# 2. Tag and push (the CI workflow keys on `v*` tag pushes):
git tag v0.3.0-beta.1
git push origin main
git push origin v0.3.0-beta.1

# 3. Wait ~20-30 min. GitHub Actions:
#    - Builds the PyInstaller CLI on Win/Mac/Linux
#    - Builds the Tauri desktop bundle on Win/Mac/Linux
#    - Creates a DRAFT release with all artifacts attached
#
# 4. Review the draft at https://github.com/papapew/Vibechek/releases
#    Edit the release notes, then click "Publish release".
```

That's the whole process. The rest of this doc is detail you only need when something goes wrong.

---

## Version-string rules

Four files carry a version. They must all agree, and they must match the git tag (without the leading `v`):

| File | Format | Example |
|---|---|---|
| `vibechek/__init__.py` | Free-form semver | `0.3.0-beta.1` |
| `pyproject.toml` | **PEP 440** (no hyphen for pre-release) | `0.3.0b1` |
| `ui/package.json` | npm semver | `0.3.0-beta.1` |
| `ui/src-tauri/Cargo.toml` | Cargo semver | `0.3.0-beta.1` |
| `ui/src-tauri/tauri.conf.json` | Tauri semver | `0.3.0-beta.1` |

The git tag itself is `v0.3.0-beta.1`. Keep these in sync — `vibechek --version` reads `__init__.py`, the desktop installer name reads `tauri.conf.json`, and pip install reads `pyproject.toml`. Drift causes "the about page says v0.3.0 but the installer says v0.2.9"-class bugs.

### Pre-release suffix conventions

- Alpha (early, broken expected): `0.3.0-alpha.1` / pyproject `0.3.0a1`
- Beta (feature-complete, bug hunt): `0.3.0-beta.1` / pyproject `0.3.0b1`
- Release candidate: `0.3.0-rc.1` / pyproject `0.3.0rc1`
- Stable: `0.3.0` / pyproject `0.3.0`

---

## What the CI release workflow builds

`.github/workflows/release.yml` runs on every `v*` tag push (or manual dispatch). It has three jobs:

### 1. `build-cli` (matrix: ubuntu / macos / windows)

Runs PyInstaller via the per-OS scripts in `packaging/`. Produces:

- `vibechek-linux-x64.tar.gz`
- `vibechek-macos-arm64.tar.gz` (or `x86_64` on older runners)
- `vibechek-windows-x64.zip`
- Plus a raw sidecar binary (`vibechek-sidecar-<rust-triple>`) used by job 2

### 2. `build-tauri` (matrix: ubuntu / macos / windows)

Downloads the sidecar binary from job 1, stages it at `ui/src-tauri/binaries/`, then builds the desktop installers via `tauri-action`:

- Windows: `Vibechek_0.3.0-beta.1_x64-setup.exe` + `.msi`
- macOS: `Vibechek_0.3.0-beta.1_aarch64.dmg`
- Linux: `Vibechek_0.3.0-beta.1_amd64.AppImage` + `.deb`

### 3. `release` (ubuntu, runs after both)

Collects all artifacts, creates a **draft** release with them attached. You manually click Publish.

---

## Running a build locally (smoke test before tagging)

Sometimes you want to verify a build will work before burning a CI run. The PyInstaller side is fast (~3 min); the Tauri side is slower (~10-15 min).

### CLI / sidecar — local

```bash
# Windows
packaging\build-windows.bat

# macOS
chmod +x packaging/build-macos.sh && packaging/build-macos.sh

# Linux
chmod +x packaging/build-linux.sh && packaging/build-linux.sh
```

Result: `dist/vibechek/vibechek(.exe)` and a tarball/zip alongside.

### Tauri desktop — local

```bash
cd ui
npm install
# Stage the sidecar built above:
mkdir -p src-tauri/binaries
cp ../dist/vibechek/vibechek src-tauri/binaries/vibechek-sidecar-<triple>
chmod +x src-tauri/binaries/vibechek-sidecar-<triple>  # Unix
npm run tauri:build
```

Rust triples:
- Windows: `x86_64-pc-windows-msvc.exe`
- macOS Apple Silicon: `aarch64-apple-darwin`
- macOS Intel: `x86_64-apple-darwin`
- Linux: `x86_64-unknown-linux-gnu`

Output lands in `ui/src-tauri/target/release/bundle/`.

---

## After the draft release exists

GitHub auto-fills release notes from commits since the previous tag. Review and edit:

- Lead with the **headline change**, not the version number.
- Use the four-bucket structure: **New features**, **Improvements**, **Fixes**, **Breaking changes**.
- Link to relevant docs (USER_GUIDE.md, INSTALL.md).
- For a beta, add a "Known caveats" section honestly.

Then click **Publish release**. This:
- Makes the artifacts publicly downloadable from `/releases/latest`
- Updates the GitHub homepage badge
- Sends notifications to repo watchers

---

## What if I need to retag?

If you tagged but realized you needed a fix before publishing:

```bash
# Don't delete a published release. For a draft, delete the draft from
# the GitHub UI, then:
git tag -d v0.3.0-beta.1            # local delete
git push origin :refs/tags/v0.3.0-beta.1  # remote delete

# Make the fix, commit, then re-tag:
git tag v0.3.0-beta.1
git push origin v0.3.0-beta.1
```

If a release is already **published**, do not delete — issue a `v0.3.0-beta.2` instead. Re-using a published version number breaks anyone who's already downloaded it.

---

## Troubleshooting CI failures

**PyInstaller fails on macOS** with `'_decimal' module not found`: macOS runner Python image is being upgraded; pin Python in the workflow to 3.12 explicitly (already done).

**Tauri build fails on Linux** with `libwebkit2gtk-4.1-dev not found`: the Ubuntu runner image upgraded; check the apt install list in `release.yml`.

**`tauri-action` fails to find the sidecar**: the artifact name and the destination path must match the rust triple convention exactly. See the `sidecar_name`/`sidecar_dest` matrix in `release.yml`.

**The release job doesn't run**: it has `if: startsWith(github.ref, 'refs/tags/v')` — make sure you pushed a tag, not just a commit.

---

## Hand-test checklist before promoting a beta to stable

- [ ] CLI smoke: `vibechek --version` and `vibechek preflight` on all three OSes
- [ ] Desktop boot: app starts, sidecar connects, Settings panel populates
- [ ] Full analyze run on a ≥5 000 track library, end-to-end
- [ ] Dedup → review → keep/discard cycle works
- [ ] Tag write + restore round-trip preserves Rekordbox GEOB/PRIV frames
- [ ] Windows: WSL auto-install flow works from a fresh Windows VM
- [ ] Windows: "Enable GPU" install on a machine with the libs missing
- [ ] All 192 Python tests + 24 frontend tests pass on CI

When all of these are green for a beta, drop the suffix and ship the stable.
