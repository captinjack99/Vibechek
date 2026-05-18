# Settings tab + PreflightDialog button-flow audit

Scope: `ui/src/components/Settings.tsx`, `ui/src/components/PreflightDialog.tsx`,
`ui/src/components/LogsViewer.tsx`, `ui/src/hooks/useConfigPersistence.ts`,
`ui/src/hooks/useSidecar.ts`, `ui/src/stores/index.ts`, and the sidecar handlers
they call (`vibechek/rpc.py`, `vibechek/wsl.py`, `vibechek/native_install.py`,
`vibechek/config.py`).

Read-only audit. 22 findings, sorted by risk.

---

## 1. Install steps in PreflightDialog cannot actually be cancelled
**Risk**: HIGH
**Button / flow**: PreflightDialog "Cancel" button (inside the Live-progress panel) during Install WSL / Install Ubuntu / Install Vibechek in WSL / Install Essentia (native) / Enable GPU (CUDA wheels)
**File**: `ui/src/components/PreflightDialog.tsx:68-74`, `vibechek/wsl.py:882-937`, `vibechek/wsl.py:994-1404`, `vibechek/wsl.py:1464-1593`, `vibechek/native_install.py:188-331`
**What breaks**: All five install methods are registered as cancellable in `_CANCELLABLE_METHODS` (`vibechek/rpc.py:720-723`), and the dialog shows a Cancel button while busy. But none of the underlying functions ever call `cancellation.check()` or `cancellation.is_cancelled()`. They block on `subprocess.run(...)` or `subprocess.Popen(...).wait(timeout=...)`. The only cancellation watchdog in `native_install.py` lives in `run_vibechek_in_native_venv` (the analyze path), NOT in `install_essentia_native`. Result: user clicks Cancel, `cancellation.cancel()` flips the flag, the cancel_operation RPC returns instantly, but the install keeps running for up to 30 minutes. Meanwhile the cancellation singleton is in "cancelled" state — a subsequent op might short-circuit or behave oddly.
**Reproduction**: Start "Install in Ubuntu-24.04" from PreflightDialog → as soon as you see live log lines, click Cancel → button reports success → top of dialog still shows "Working...", apt still running inside WSL, no way to stop it short of killing the sidecar.
**Fix**: Either (a) wrap each install in a `subprocess.Popen` + poll loop that checks `cancellation.is_cancelled()` and calls `proc.terminate()`/`proc.kill()`, mirroring the watchdog in `run_vibechek_in_native_venv`; or (b) remove these methods from `_CANCELLABLE_METHODS` and hide the Cancel button in PreflightDialog during install steps so users aren't misled.

## 2. PreflightDialog Cancel sends cancel even when there is no cancellable op
**Risk**: HIGH
**Button / flow**: PreflightDialog "Cancel" button during the "Download models" step
**File**: `ui/src/components/PreflightDialog.tsx:139-151`, `vibechek/rpc.py:570-573`
**What breaks**: `handleDownloadModels` calls `begin("download-models")` and `rpc("download_models", ...)`. `download_models` is in `_CANCELLABLE_METHODS` (kind=`"download-models"`), but the actual implementation in `vibechek/analyzer.py` is a tight HTTP-streaming loop that never checks cancellation. Same shape as #1. Worse: `cancellation.cancel()` flips the global flag and returns the *previous* kind, but the download keeps running. After it eventually completes, the cancellation flag is still set unless `cancellation.end()` is invoked — which it is on the finally path of `_dispatch`. OK in this case, but very fragile.
**Reproduction**: Click "Download models" → click Cancel mid-download → download finishes anyway and the dialog auto-closes when ready, leaving users confused about whether Cancel did anything.
**Fix**: Add `cancellation.check()` calls into `download_models`' download loop (same pattern as analyzer.py:1136). Also: detect that no cancellable op is currently running before submitting the cancel RPC, to avoid spurious flag flips.

## 3. `String(e)` in fail() handlers loses the RpcError.cancelled flag
**Risk**: HIGH
**Button / flow**: every catch handler in Settings.tsx and PreflightDialog.tsx
**File**: `ui/src/components/Settings.tsx:38,74,130,795`, `ui/src/components/PreflightDialog.tsx:109,110,147`, `ui/src/stores/index.ts:107-120`
**What breaks**: `useSidecar.ts` carefully builds an `RpcError` with a parsed `.cancelled` boolean. Every call site immediately does `fail(String(e))`, which throws away the object and gives `"RpcError: <message>"`. The store's `fail()` then has to substring-match for `'"cancelled":true'` (won't match — the stringified error doesn't contain the raw JSON) or `"cancelled by user"` (matches *only* by convention because every CancelledError currently includes that exact phrase). The moment someone reworks a cancel message to "Operation aborted by user", every catch site silently switches from "silent finish" to "scary red error toast". The `RpcError` infrastructure added in beta.8 is being defeated everywhere.
**Reproduction**: Edit `vibechek/cancellation.py:67` to change the message to `"Operation '{_current_kind}' aborted"` → any user cancellation now surfaces as a red "Something went wrong: Operation 'analyze' aborted" toast in the UI.
**Fix**: Either change every `fail(String(e))` to `fail(e)` and pass the raw object (the store already checks `(error as any).cancelled === true`), or replace the whole pattern with `if (isCancellation(e)) return; fail(String(e));` at every catch site. Existing `isCancellation()` helper in `useSidecar.ts:56` is already unused.

## 4. The new `repair_wsl_shim` RPC is wired to no UI affordance
**Risk**: HIGH
**Button / flow**: there isn't one
**File**: `vibechek/rpc.py:340-349`, `vibechek/wsl.py:1406-1461`. No reference anywhere in `ui/src/`.
**What breaks**: A user whose shim was poisoned by a pre-beta.10 CUDA install on a distro that the auto-repair in `_probe_distro` (`vibechek/wsl.py:217-227`) hasn't visited yet has no way to invoke the repair from the UI. The probe-time auto-repair only runs when `detect_wsl(quick=False)` is called AND when the distro is not in the `_NON_LINUX_DISTROS` set. If the user has a custom distro name (`Ubuntu-custom`, `MyUbuntu`, etc.) and the auto-repair never fires for some reason, the user is stuck with `SyntaxError: invalid syntax` on every analyze and no way to recover from the GUI.
**Reproduction**: Manually poison the shim via `wsl -d Ubuntu-24.04 -- bash -c 'echo ". /home/$USER/.vibechek/cuda-env.sh" >> /home/$USER/.vibechek/venv/bin/vibechek'` → restart the sidecar → analyze → user sees a "SyntaxError" toast with no repair button. No UI surface for `repair_wsl_shim` exists.
**Fix**: Surface a "Repair WSL shim" button on the EngineGpuBlock failure state in Settings.tsx (or under a new "Troubleshooting" disclosure). Wire it to call `rpc("repair_wsl_shim", {distro})` and report the message. Bonus: have analyze itself detect the "cuda-env.sh" SyntaxError pattern and offer one-click repair inline in the toast.

## 5. The Enable-GPU button has no live progress or cancel
**Risk**: HIGH
**Button / flow**: "Enable GPU (install CUDA wheels)" in Settings → EngineGpuFixableBlock
**File**: `ui/src/components/Settings.tsx:760-849`, `vibechek/wsl.py:1464-1593`
**What breaks**: Sidecar streams pip output line-by-line as `progress` notifications (`vibechek/wsl.py:1543-1552`), but this block never subscribes via `useSidecarProgress`. User sees a static "Installing… (~30 sec)" label for whatever the real duration is. With slow PyPI mirrors a `nvidia-cublas` wheel can take 5-10 minutes; users will reach for the close button. There's no Cancel either (and per #1 it wouldn't work anyway). If pip fails partway, the user sees nothing useful until the install ends and `installError` populates from `result.error` (good) — but if pip exits 0 yet TF still can't see the GPU (very common: wrong CUDA major version), `result.ok=true` and we show a green "Installed N package(s). Re-probing…" with no hint of the underlying failure. The re-probe will then show the same fixable-state again, looking like the button did nothing.
**Reproduction**: Throttle network to 50 KB/s → click "Enable GPU" → button reads "Installing… (~30 sec)" for 10 minutes, user assumes it's hung → close dialog → install actually completed but no proof. Also: install on a system where `essentia-tensorflow` needs cu12 but the probe reported cu11 missing libs → pip "succeeds", TF still can't register the GPU, user sees the fixable banner again and clicks the button in a loop.
**Fix**: Wire `useSidecarProgress` into the block (mirror PreflightDialog's live-log pattern). After re-probe, if `gpu_hardware_visible && !gpu_available` is *still* true after install, surface an explicit "Install completed but GPU still not visible — see logs" warning instead of silently re-rendering the same fixable state.

## 6. Config auto-save failure is completely invisible
**Risk**: HIGH
**Button / flow**: any toggle / slider / picker in Settings (debounced auto-save)
**File**: `ui/src/hooks/useConfigPersistence.ts:46-50`
**What breaks**: `rpc("save_config", ...).catch(() => {})` swallows every failure with a comment claiming it's "surfaced via global error toast", but nothing in the chain ever surfaces it. If the user's config dir is read-only (NTFS perms, locked Google Drive folder, full disk), every settings change is silently discarded; the UI shows the new state but the next app launch reverts. Worse: in the "TOML→JSON migration" path, if save fails the legacy TOML stays on disk and the next load is again a migration — every launch shows the migration log line but no JSON ever lands.
**Reproduction**: `chmod -w "$(python -c "from platformdirs import user_config_dir; print(user_config_dir('Vibechek'))")"` on Linux/macOS, or revoke write on `%APPDATA%\Vibechek` on Windows → toggle anything in Settings → close + reopen app → setting reverted, no error shown.
**Fix**: Replace the `.catch(() => {})` with a proper handler that pushes to `useOperationStore.fail` (or a softer dedicated notification for the persistence case). The store dedupes via the `error` field, so a single "Couldn't save settings: {reason}" toast is fine. Also: on app start, if `LEGACY_CONFIG_FILE` exists AND `CONFIG_FILE` doesn't AND the migration save in `_save_config` succeeds, delete the TOML file so it doesn't keep triggering the migration log.

## 7. Settings.tsx component unmount during preflight leaks setState
**Risk**: MED
**Button / flow**: navigate away from Settings while the slow preflight or engineGpu probe is in flight (~10s on WSL)
**File**: `ui/src/components/Settings.tsx:80-119, 57-78`
**What breaks**: `refreshPreflight` and `refreshEngineGpu` set state in `.then()` / `.catch()` / `.finally()` chains with no `isMounted` guard. The slow phase-2 preflight takes 5-10s on Windows + the engine probe takes another ~10s. If the user opens Settings, then jumps back to Library while either is in flight, React will warn `setState on unmounted component`; the dialog state can also flip stale (e.g. an old engine probe result lands and overwrites a newer one if the user re-mounts Settings quickly). The `useConfigPersistence` hook handles this pattern correctly (`cancelled` flag, see :25-37) — Settings does not.
**Reproduction**: Open Settings, immediately click Library in the sidebar, wait 10s, watch the devtools console for React's "Can't perform a React state update on an unmounted component" warning. In production this is a memory + correctness bug rather than a visible crash.
**Fix**: Wrap each async chain with an `isMounted` ref or, better, an `AbortController` passed via params. The `useEffect` cleanup should set the ref to false. Same pattern as `useConfigPersistence.ts:25-37`.

## 8. PreflightDialog re-check now triggers a 10-minute timeout for cold WSL probes
**Risk**: MED
**Button / flow**: "Re-check" in PreflightDialog footer
**File**: `ui/src/components/PreflightDialog.tsx:78-87`, `vibechek/wsl.py:106-165, 195-283`
**What breaks**: The recent change to `rpc<PreflightResult>("preflight", { quick: false })` means re-check now does the per-distro probe in *all* WSL distros in parallel. The probe is bounded by `ThreadPoolExecutor.timeout=30` but each individual `_probe_distro` Popen has a `proc.communicate(timeout=30)` and the distro boot for a Stopped distro can take 15-30s. With 3 distros installed (Ubuntu, Debian, kali), the re-check can stall the dialog UI for 30+ seconds with no spinner, no progress, no Cancel — it's a plain `await rpc(...)`. The dialog footer's Re-check button is disabled (`busyAction !== null` is false during this call though — wait, busyAction is null because we're not in an install — so the user can click it multiple times). Each click queues another full slow probe.
**Reproduction**: On a Windows machine with 3 WSL distros installed and stopped → open PreflightDialog → spam-click Re-check 5 times → 5 parallel slow probes pile up on the sidecar's 8-worker pool, blocking other RPCs (system_info, get_log_tail) until they finish.
**Fix**: (a) Disable the Re-check button while a re-check is in flight (`const [rechecking, setRechecking] = useState(false)`). (b) Show a spinner inside the button. (c) Consider keeping the quick + slow two-phase load here too rather than going slow-only.

## 9. Models directory text field is unvalidated until analyze runs
**Risk**: MED
**Button / flow**: Models directory input under Analysis section
**File**: `ui/src/components/Settings.tsx:254-279`
**What breaks**: User can type any string; no existence check, no permission check, no "is this a network drive" warning. The path is debounced-saved into config 500ms later. On the next "Download models" click, the sidecar attempts to write 800MB to the path — if it's a slow OneDrive folder, it silently uploads each model file; if it's a non-existent path, you get a permission denied at the file open. The fixable case (typo in path) only surfaces after the 800MB download attempt.
**Reproduction**: Type `Z:\does-not-exist` in the Models directory field → click Download models → see a `FileNotFoundError` toast after the sidecar starts the download.
**Fix**: Add a debounced existence check (sidecar RPC `path_writable?` or just a Tauri `fs.exists`). Show inline yellow warning under the input if the path doesn't exist or isn't writable. Also flag risky path patterns (My Drive, OneDrive — same logic as `_RISKY_PATH_SUBSTRINGS` in rpc.py:801).

## 10. GPU dropdown "on" lies — silently falls back to CPU
**Risk**: MED
**Button / flow**: GPU acceleration "on" pill
**File**: `ui/src/components/Settings.tsx:210-252`, `vibechek/resources.py:185-200`
**What breaks**: The Hint text claims "on" forces GPU and "errors loudly if no GPU". `apply_gpu_preference("on")` actually just sets `CUDA_VISIBLE_DEVICES=0`. If no GPU exists or TF can't register it (missing CUDA libs), TF falls back to CPU with only a stderr warning — analyze runs, takes 10× longer, user has no idea they're on CPU. The Settings UI hint even says "If 'on' and no GPU, TF logs warning — which is user's expected feedback" but stderr isn't surfaced to the GUI.
**Reproduction**: On a machine where engine probe shows `gpu_hardware_visible=true, gpu_available=false` → set GPU dropdown to "on" → run analyze → it runs on CPU at ~2 tracks/sec instead of ~10/sec, no warning anywhere.
**Fix**: Either (a) make "on" actually error: have the analyzer check `engineGpu.gpu_available` at start and raise if "on" was requested but no GPU is usable, or (b) replace the three-way pill with just auto/off and let the engine-GPU block be the single source of truth. The current hint text overpromises.

## 11. Workers slider max = cpu_count silently caps user override after sysInfo loads
**Risk**: MED
**Button / flow**: Worker processes slider
**File**: `ui/src/components/Settings.tsx:177-208`
**What breaks**: Two issues. (1) `max={sysInfo?.cpu_count ?? 32}`: before sysInfo lands the user can drag to 32 even on a 4-core machine; after sysInfo lands the same slider clamps silently. (2) If a config previously saved `workers: 64` (manual edit of config.json), the slider's `value={Math.max(1, cfg.analysis.workers)}` shows 64 but the slider max is e.g. 8 → range input renders at max=8 visually, but the actual config still says 64, and analyze uses 64 worker processes (~32 GB RAM at 500MB/worker). No warning. The "auto" button next to it silently rewrites without confirmation.
**Reproduction**: Open `config.json`, set `workers: 64`, save → open Settings → slider visually at max but config still says 64 → click Analyze → 64 worker processes spawn → swap, OOM, app freezes.
**Fix**: (a) Default slider max to a safe ceiling (e.g. `cpu_count * 2`) and warn if `workers > cpu_count`. (b) Clamp `cfg.analysis.workers` to `[1, cpu_count]` on config load (or at least surface a warning if it exceeds cpu_count).

## 12. Restore-defaults button has no confirmation, wipes user customizations silently
**Risk**: MED
**Button / flow**: "Restore all settings to defaults" at the bottom of Settings
**File**: `ui/src/components/Settings.tsx:439-447, 33-40`
**What breaks**: One click, no `<ConfirmModal>` (which exists in the codebase!), instantly wipes models_dir / review_folder / target_root / id3_text_encoding / everything. The `restore_default_config` RPC also writes to disk immediately, so there's no undo. ConfirmModal is used elsewhere in the codebase for destructive actions (organize, dedupe). Skipping it here is inconsistent and unsafe.
**Reproduction**: Have a carefully-configured Models directory pointed at `D:\AI-Models\essentia` (800MB you don't want to re-download) → accidentally hit "Restore all" → instant wipe → next analyze re-downloads 800MB to the default user-data dir.
**Fix**: Wrap the handler in `<ConfirmModal title="Restore all settings?" body="This will reset Models directory, review folder, organization rules, and tagging preferences to defaults. Cannot be undone." confirmLabel="Restore">`. Pattern already used in `OrganizeView.tsx` / `DuplicatesView.tsx`.

## 13. PreflightDialog WindowsFlow doesn't render after WSL install if a reboot is needed
**Risk**: MED
**Button / flow**: "Install WSL + Ubuntu" then auto-reCheck
**File**: `ui/src/components/PreflightDialog.tsx:117-122, 78-87`, `vibechek/wsl.py:933-937`
**What breaks**: After install_wsl succeeds, `install_wsl` returns `{ok:true, note:"Vibechek may need a reboot to fully initialize WSL."}`. The dialog calls `reCheck()` which calls `preflight({quick:false})`. If the user needs a reboot, the preflight will report `wsl_feature_enabled=true, distros=[]` (because WSL was *just* enabled, the distro isn't actually registered yet pending reboot). WindowsFlow then routes to Step 2 ("No distro yet, install Ubuntu") which the user clicks — and that re-runs `wsl --install -d Ubuntu-24.04`, which on an un-rebooted machine fails with a cryptic kernel error. The reboot `note` from the install_wsl response is never displayed anywhere — `runWithProgress` only reads `result.ok` and `result.error`.
**Reproduction**: Fresh Windows machine, no prior WSL → click Install WSL → install succeeds → dialog suggests "Install Ubuntu" → click → cryptic failure → user is stuck in a loop, no mention of needing a reboot.
**Fix**: `runWithProgress` should surface `result.note` somewhere (toast or panel) when present. WindowsFlow should also check `preflight.wsl?.error` and the "just installed, reboot likely needed" hint, and show a "Restart Windows to complete WSL setup" panel rather than offering the next step.

## 14. PreflightDialog click-outside-to-close fires during an install
**Risk**: MED
**Button / flow**: clicking the dark backdrop while an install is running
**File**: `ui/src/components/PreflightDialog.tsx:153-167`
**What breaks**: The outer `<motion.div>` has `onClick={onClose}`. Inner panel does `stopPropagation`. But during an install, `busyAction !== null` only disables the footer Close button — it does NOT block backdrop-click-to-close. User clicks outside the dialog mid-install → dialog vanishes → install keeps running with no UI surface → user has no idea what's happening, no way to see live progress, and per #1 no way to cancel.
**Reproduction**: Click "Install in Ubuntu-24.04" → click anywhere on the dark backdrop → dialog closes → install keeps running invisibly for 5 minutes → "Setup needed before analyze" reappears when user clicks Analyze again because it didn't see the install finish.
**Fix**: In the backdrop onClick, check `if (busyAction) return; onClose();`. Same logic the footer's disabled Close button already implements.

## 15. handleDownloadModels uses stale `cfg.analysis.models_dir` from closure
**Risk**: MED
**Button / flow**: "Download models now" in Analysis section
**File**: `ui/src/components/Settings.tsx:121-132`
**What breaks**: `handleDownloadModels` reads `cfg.analysis.models_dir` from the closure captured at render time. Because of the 500ms auto-save debounce in `useConfigPersistence`, the user can type a new path → immediately click Download → the click fires BEFORE the debounce save has run AND before the next React render has captured the new closure (because zustand state has updated but the action hasn't re-rendered Settings yet — actually selectors will re-render, so the closure should be fresh). The real issue: if a sibling component drives a state change AND the user clicks within the same tick, the closure can be off by one. Lower-risk version of the React closure issue. Worth noting.
**Reproduction**: Hard to repro reliably; mostly a code-quality flag.
**Fix**: Use `useConfigStore.getState().config.analysis.models_dir` at click time instead of the captured value, or pass a function ref pattern.

## 16. EngineGpuFixableBlock has no cancel and timeouts at 30 minutes
**Risk**: MED
**Button / flow**: Enable GPU button (same as #5, different angle)
**File**: `ui/src/components/Settings.tsx:771-799`, `vibechek/wsl.py:1554-1558`
**What breaks**: Sidecar's 30-minute Popen timeout means the GUI button is in `installing=true` state for up to 30 minutes if pip hangs. The UI provides no cancel, no abort. PreflightDialog at least has a cancel button (even if it doesn't work, per #1) — Settings doesn't even have that.
**Reproduction**: Network drop mid-pip-install → button shows "Installing… (~30 sec)" for 30 minutes → only way to unstick: kill the app.
**Fix**: Add a cancel button next to the install button; wire it to `cancel_operation` RPC. Combined with the fix for #1 (actually checking cancellation in the install), this becomes useful.

## 17. RpcError.cancelled flag isn't checked in ANY catch handler
**Risk**: MED
**Button / flow**: every async RPC catch path
**File**: `ui/src/hooks/useSidecar.ts:55-58` (helper exists) — used by zero call sites
**What breaks**: `isCancellation(e)` is exported and well-typed but **grep shows zero usages outside its own definition**. Every catch in Settings / PreflightDialog / Library / Tags / Organize routes through `fail(String(e))` and depends on the string-substring fallback in the operation store (see #3). This means the RpcError abstraction is dead weight today; the substring fallback is what actually keeps the UI quiet on cancellation.
**Reproduction**: Already covered in #3.
**Fix**: Add a lint rule (or a code review note) that catch blocks calling sidecar RPCs use `if (isCancellation(e)) return;` before falling through to `fail`. Convert at least the most-clicked sites (Settings, PreflightDialog) as a starting point.

## 18. LogsViewer level filter relies on exact-spaces format and silently breaks on unknown formats
**Risk**: LOW
**Button / flow**: Level filter pills in LogsViewer
**File**: `ui/src/components/LogsViewer.tsx:71-74`
**What breaks**: The filter searches for ` ${filter.padEnd(7)} ` or ` ${filter} `. Logging format is `%(levelname)-7s` which produces `INFO   ` (7 chars). If the user opens an older rotated log file with a different formatter (or future code changes the format string in `logging_setup.py:28-31`), filtering to "INFO" silently shows 0 lines. The UI shows "0 of N lines" with the "No INFO lines" empty state, but the user just sees an empty pane with no clue why.
**Reproduction**: Manually edit a log line to remove a space → filter to that level → line is "missing". Or change `%(levelname)-7s` in logging_setup.py to `%(levelname)s`.
**Fix**: Use a regex like `/\b${filter}\b/` instead of brittle exact-spaces matching. Or tag log lines with structured metadata.

## 19. LogsViewer copy-to-clipboard never catches clipboard rejection
**Risk**: LOW
**Button / flow**: "Copy" button in LogsViewer toolbar
**File**: `ui/src/components/LogsViewer.tsx:76-80`
**What breaks**: `navigator.clipboard.writeText` can reject (denied permission, insecure context, Tauri 2 webview without permission). Code sets `copied=true` and ignores the rejection. User sees the "Copied" checkmark for 1.5s, paste yields nothing.
**Reproduction**: Disable clipboard permission in browser/webview settings → click Copy → false positive UI.
**Fix**: Wrap in try/catch; on failure show a notification ("Couldn't copy to clipboard").

## 20. preflight.analyze_via="native_venv" is dropped by Settings' narrow check
**Risk**: LOW
**Button / flow**: ResourcesSection's analyzeVia prop on Linux/macOS
**File**: `ui/src/components/Settings.tsx:158-165`
**What breaks**: Narrowing literal accepts only `"wsl"|"native"`. When sidecar reports `"native_venv"` (the new managed-venv route on Linux/macOS), it's passed as `null`. `EngineGpuBlock` then can't say "via TensorFlow inside ~/.vibechek/venv" — it falls through to the "no engine probe yet" / generic message. Confusing on Linux because users see "Probing the analyze engine…" forever-ish (until the probe finishes and reports its own engine="native"), but the spinner-vs-result reasoning is muddled.
**Reproduction**: Run on Linux with `~/.vibechek/venv` installed → open Settings → top-of-page "subtitle" correctly says "analyze will route through the managed venv" but the ResourcesSection's WSL-vs-native messaging is missing.
**Fix**: Include `"native_venv"` in the union: `analyzeVia: "native" | "wsl" | "native_venv" | null`. Update EngineGpuBlock to handle the three-way case (`engine="native"` covers both, but the WSL hint copy needs adjustment).

## 21. _save_config silently accepts a malformed config payload
**Risk**: LOW
**Button / flow**: any settings change → debounced save_config RPC
**File**: `vibechek/rpc.py:555-560`, `vibechek/config.py:202-223`
**What breaks**: `_from_dict` calls `_subset` which drops unknown keys and logs warnings for invalid coercions, then writes whatever was left. If the GUI ever sends a partial payload (say, only `{"analysis": {...}}` without `tagging`), `save_config` happily writes a file with default tagging — wiping the user's previous tagging preferences. Today the GUI always sends the full config from the zustand store so this doesn't happen, but the API has no schema guard.
**Reproduction**: Construct a corrupted RPC call with `params={"config": {"analysis": {"workers": 8}}}` → next `get_config` returns all-defaults for everything except analysis.workers, wiping user prefs silently.
**Fix**: Document the contract (full-config-replace), or add a server-side merge against the current on-disk config before writing. The latter is safer.

## 22. EngineGpuFixableBlock distro guard fails after the install
**Risk**: LOW
**Button / flow**: re-clicking "Enable GPU" after a successful install
**File**: `ui/src/components/Settings.tsx:771-799`, `:833-841`
**What breaks**: After install, `onRefresh` is called via 500ms `setTimeout` — but `engineGpu` may temporarily become `null` (if `setEngineGpu` is in flight during the re-probe). The button check `engineGpu.engine !== "wsl" || !engineGpu.distro` would no-op for the next click — but the button is rendered based on the *outer* `EngineGpuBlock` which only mounts this child when `gpu_hardware_visible && !gpu_available`. The double-button-state transition is awkward. Also, "Re-probe" is enabled while installing — clicking it during install kicks off a parallel `engine_gpu_status` RPC that might race the install's cache invalidation.
**Reproduction**: Click Enable GPU → during install, click Re-probe → two `engine_gpu_status` calls race; outcome depends on which lands last.
**Fix**: Disable Re-probe while `installing` (same as the install button's `disabled` state, but the Re-probe currently mirrors `installing` only — see line 842, OK actually). The real cleanup: refactor to a finite-state machine (`probing | fixable | installing | post-install-probing | ready`).

---

## Counts

- HIGH: 6
- MED: 11
- LOW: 5

## Top 3

1. **#1 — Install steps can't actually be cancelled.** The Cancel button in PreflightDialog is decorative for 5 of the 6 install methods. The cancellation singleton gets into an inconsistent state too. Most user-visible after a "user clicks Cancel" reaction to a slow apt mirror.
2. **#3 — `String(e)` defeats the RpcError class.** Every catch handler is one upstream message-text change away from spamming red "Something went wrong" toasts on every user cancellation. The `isCancellation()` helper exists and has zero call sites.
3. **#5 — Enable-GPU button shows no progress, can't be cancelled, and false-positive succeeds when pip exits 0 but TF still can't see the GPU.** This is the button the user just rewrote in beta.6; it's the most visible setup affordance in Settings and currently the most opaque.

## Honorable mentions

- **#4** (no UI for `repair_wsl_shim`) is the one the user explicitly asked about. The auto-repair in `_probe_distro` is the safety net, but the explicit RPC has zero UI surface — and the user just shipped it.
- **#6** (silent config save failure) is sneaky because the in-memory state shows the change, only the next launch reveals the loss.
- **#12** (no confirm on Restore defaults) is the one-click foot-gun. ConfirmModal already exists in the codebase, used elsewhere for this exact pattern.
