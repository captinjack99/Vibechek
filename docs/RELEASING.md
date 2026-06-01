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

## Auto-update + signing setup (one-time)

Vibechek ships an in-app auto-updater (`tauri-plugin-updater`). On launch — or when the user clicks **Settings → Software updates → Check for updates** — the app fetches the update manifest from GitHub Releases, verifies the new bundle's signature against a **public key baked into the app**, and (with the user's consent) downloads, installs, and relaunches.

This is a **separate keypair from the OS code-signing certs** below. The updater key signs the *update payload* so the running app can trust it; the OS certs sign the *installer* so Gatekeeper / SmartScreen trust it. You want both, but they're independent.

### One-time key generation

Generate the updater keypair once (do this on a trusted machine, not in CI):

```bash
# Either the standalone CLI...
tauri signer generate -w ~/.tauri/vibechek.key
# ...or via npx if you don't have the CLI installed globally:
npx @tauri-apps/cli signer generate -w ~/.tauri/vibechek.key
```

This prints a **public key** and writes the **private key** to `~/.tauri/vibechek.key` (you'll also be prompted for an optional password). Then:

1. **Commit the PUBLIC key** into `ui/src-tauri/tauri.conf.json` at `plugins.updater.pubkey`, replacing the placeholder `PLACEHOLDER_REPLACE_WITH_TAURI_SIGNER_PUBKEY`. The public key is safe to commit — it only lets the app *verify* updates, not sign them.
2. **Add the PRIVATE key as a GitHub repo secret** named `TAURI_SIGNING_PRIVATE_KEY` (Settings → Secrets and variables → Actions). Paste the full contents of `~/.tauri/vibechek.key`.
3. If you set a password during generation, **add it as `TAURI_SIGNING_PRIVATE_KEY_PASSWORD`**. If you generated without a password, you may omit this secret (or set it empty) — the gate in the workflow treats it as optional alongside the key.
4. **Flip `bundle.createUpdaterArtifacts` to `true`** in `ui/src-tauri/tauri.conf.json`. It ships as `false` so the current unsigned beta builds don't require a signing key (see "How the CI gate works" below); turning it on is what actually produces the signed updater bundles + `latest.json`.

> ⚠️ **Losing the private key permanently breaks updates.** Every released build's updater pubkey is fixed at build time; an update payload signed with a *different* private key will fail verification on every already-installed copy. There is no recovery other than shipping a fresh installer (which users must download manually) carrying a new pubkey. Back up `~/.tauri/vibechek.key` somewhere safe and offline.

### How the CI gate works

**`bundle.createUpdaterArtifacts` ships as `false`** so the current **unsigned beta builds keep working** — with it `true`, `tauri build` *requires* `TAURI_SIGNING_PRIVATE_KEY` and would fail the release when no key is configured. So enabling auto-update is a deliberate flip (step 4 above): set it to `true` once you've generated the keypair, committed the pubkey, and added the secret.

When `createUpdaterArtifacts: true`, the bundler emits the per-platform updater bundle (`*.nsis.zip` on Windows, `*.app.tar.gz` on macOS, the `*.AppImage` on Linux) **plus a detached `*.sig` signature** for each. The signing is gated exactly like the OS certs: the `Configure code signing (opt-in)` step in `build-tauri` exports `TAURI_SIGNING_PRIVATE_KEY` / `TAURI_SIGNING_PRIVATE_KEY_PASSWORD` to `$GITHUB_ENV` **only when the secret is non-empty**. With no key configured (and `createUpdaterArtifacts: false`), the build still succeeds — unsigned bundles, no `*.sig`, and the `release` job skips writing `latest.json`, so the in-app updater stays inert until you supply the key.

The `release` job collects the `*.sig` files and synthesizes `release/latest.json` (the manifest `plugins.updater.endpoints` points at: `https://github.com/papapew/Vibechek/releases/latest/download/latest.json`), then attaches it alongside the installers. Because the endpoint resolves to `/releases/latest`, only a **published** (non-draft) release is visible to the updater — drafts won't trigger updates, which matches the existing "review the draft, then Publish" flow.

### Windows / macOS *installer* signing

The updater key above does not satisfy the OS. For users not to see SmartScreen / Gatekeeper warnings, you also need OS code-signing certs feeding the same opt-in step — see [Codesigning + notarization](#codesigning--notarization-one-time-ci-setup) directly below:

- **Windows**: an Authenticode cert via `WINDOWS_CERTIFICATE` / `WINDOWS_CERTIFICATE_PASSWORD`. Free options for OSS exist — **[SignPath Foundation](https://signpath.org/)** offers free certificates for qualifying open-source projects, and **[Azure Trusted Signing](https://learn.microsoft.com/azure/trusted-signing/)** is a low-cost managed alternative. (Tauri 2 also supports Azure Trusted Signing directly; both ultimately produce an Authenticode signature on the `.exe`/`.msi`.)
- **macOS**: notarization requires an **Apple Developer ID** ($99/yr) — there is no free equivalent. Wire the `APPLE_*` secrets per the macOS steps below.

---

## Codesigning + notarization (one-time CI setup)

Without codesigning, **macOS Gatekeeper kills the sidecar on first launch** with a generic "can't be opened because Apple cannot check it for malicious software" dialog — most users misread that as malware and uninstall. On Windows, **Defender SmartScreen** triggers similar warnings. The release workflow (`.github/workflows/release.yml`) is wired to pass certs through `tauri-action` when these repo secrets are set.

> **Signing is OPT-IN (and must be).** The `Configure code signing (opt-in)` step in `build-tauri` exports each signing var to `$GITHUB_ENV` **only when its secret is non-empty**. This is not a nicety — it's required: Tauri 2's Rust bundler reads the cert via `std::env::var("APPLE_CERTIFICATE")`, which returns `Ok("")` for a *defined-but-empty* variable. If you wire `APPLE_CERTIFICATE: ${{ secrets.APPLE_CERTIFICATE }}` straight into the build step's `env:`, an **unset** secret still arrives as `APPLE_CERTIFICATE=` (empty but defined), the bundler thinks a cert is present, runs `security import` on empty data, and the whole macOS bundle dies with `SecKeychainItemImport: One or more parameters passed to a function were not valid`. Do **not** add `APPLE_*` / `WINDOWS_*` back into the build step's `env:` block — keep them flowing through the opt-in step.

### macOS

You need an **Apple Developer account** ($99/year). One-time setup:

1. In Xcode → Preferences → Accounts, add your Apple ID.
2. Create a **Developer ID Application** certificate at <https://developer.apple.com/account/resources/certificates>.
3. Download and double-click to install in Keychain.
4. Export it: Keychain Access → right-click the cert → Export → `.p12` format → set a password.
5. Base64-encode it:
   ```bash
   base64 -i ~/Downloads/DeveloperID.p12 | pbcopy
   ```
6. Create an **app-specific password** for notarization at <https://appleid.apple.com/account/manage> → App-Specific Passwords.
7. Find your Team ID at <https://developer.apple.com/account> → Membership.

Then add these as GitHub repo secrets (Settings → Secrets and variables → Actions):

| Secret | Value |
|---|---|
| `APPLE_CERTIFICATE` | The base64 from step 5 |
| `APPLE_CERTIFICATE_PASSWORD` | The password from step 4 |
| `APPLE_SIGNING_IDENTITY` | Something like `Developer ID Application: Your Name (TEAMID)` |
| `APPLE_ID` | Your Apple ID email |
| `APPLE_PASSWORD` | The app-specific password from step 6 |
| `APPLE_TEAM_ID` | The team ID from step 7 |

After the next release, the `.dmg` will be signed and notarized. Users get no scary dialog.

> **PyInstaller-specific note (macOS).** The sidecar is built with `pyinstaller --onefile` (see `packaging/vibechek.spec` for the rationale). At launch, the bootloader extracts the embedded Python framework to a temp dir and dlopen()s it. macOS hardened runtime's library-validation rule would normally block that. We work around it via `ui/src-tauri/entitlements.plist`, which sets four entitlements:
>
> - `com.apple.security.cs.disable-library-validation` — lets the bootloader load the extracted Python framework
> - `com.apple.security.cs.allow-dyld-environment-variables` — PyInstaller's bootloader sets `DYLD_LIBRARY_PATH` for the temp dir
> - `com.apple.security.cs.allow-jit` + `allow-unsigned-executable-memory` — keeps TF's XLA compiler (and any future ML hook) working
>
> Tauri 2 applies the same entitlements to the main app binary AND every signed Mach-O inside the bundle, including the sidecar — so `tauri-action`'s automatic codesign covers everything. **You should not need to do anything extra**, as long as `bundle.macOS.entitlements` is wired up in `tauri.conf.json` (it is).

### Windows

You need an **Authenticode code-signing certificate** (~$70-$400/year from DigiCert, Sectigo, or SSL.com). EV certs cost more but skip the SmartScreen reputation-building period.

1. Buy + download the cert as `.pfx`.
2. Base64-encode it:
   ```powershell
   [Convert]::ToBase64String([IO.File]::ReadAllBytes("C:\path\to\cert.pfx")) | clip
   ```
3. Add as GitHub repo secrets:

| Secret | Value |
|---|---|
| `WINDOWS_CERTIFICATE` | The base64 from step 2 |
| `WINDOWS_CERTIFICATE_PASSWORD` | The .pfx password |

After the next release, `.exe` and `.msi` installers will be Authenticode-signed.

> **PyInstaller-specific note (Windows).** The sidecar is a single `.exe` (no `_internal/` sibling). `tauri-action` signs the externalBin (the sidecar EXE) alongside the main app — both pick up Authenticode signatures from the same cert. The bootloader self-extracts the bundled Python interpreter to `%TEMP%` at runtime; those extracted files are not signed individually and Windows doesn't require them to be. SmartScreen looks at the parent `.exe` only.

### If you don't have certs yet

CI builds **unsigned** — and the `Configure code signing (opt-in)` step makes that work cleanly: with no secrets set it logs `macOS build will be UNSIGNED` and never attempts a `security import`, so the `.dmg` / `.app` / `.exe` build to completion. Users will see Gatekeeper / SmartScreen warnings on first launch and have to right-click → Open (macOS) or click "More info" → "Run anyway" (Windows). The release notes (generated from the `body:` in `release.yml`) already include the macOS `xattr -dr com.apple.quarantine` workaround.

> **Heads-up — a malformed cert secret will still fail the build.** The opt-in guard only protects the *empty/unset* case. If `APPLE_CERTIFICATE` is set to a **non-empty but invalid** value (base64 of a `.cer`/`.pem` instead of a `.p12`, line-wrapped base64, wrong `.p12` password, or a stray `data:` prefix), the bundler will try to import it and fail with the same `SecKeychainItemImport ... not valid`. To ship unsigned, **delete the secret entirely** (Settings → Secrets and variables → Actions) rather than blanking it. To ship signed, re-export a valid Developer ID Application `.p12` per the macOS steps above.

### Verifying a signed build

After downloading from a Release:

```bash
# macOS — should print "accepted" with no warnings
codesign --verify --deep --strict --verbose=2 /Applications/Vibechek.app
spctl --assess --verbose=4 /Applications/Vibechek.app

# Windows — right-click .exe → Properties → Digital Signatures tab
# OR via signtool:
signtool verify /pa /v Vibechek_0.3.0-beta.8_x64-setup.exe
```

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
