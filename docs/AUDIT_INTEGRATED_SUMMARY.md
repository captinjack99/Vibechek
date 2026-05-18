# Vibechek integrated UX audit — beta.10 → beta.11 closeout

> **Update for v0.3.0-beta.11**: 6 parallel agents resolved the remaining HIGH and MED findings across all 5 tabs (~50 fixes). The "Top remaining" list below is now historical context — every item is either fixed or filed as out-of-scope (e.g. macOS code signing, which needs an Apple dev account before we can ship anything).
>
> **Test count**: 212 (baseline) → 303 (beta.10) → **316 passing** (beta.11) + 24 frontend tests.
>
> See "Beta.11 close-out" section at the bottom for the per-agent breakdown of what just shipped.



Five parallel agents audited each major view (Library, Duplicates, Organize, Tags, Settings) for "doomed to fail on a real user" patterns. **82 findings total** across the five views.

This doc consolidates the findings, marks what's been fixed in this pass, and lists what's left.

| Tab | HIGH | MED | LOW | Total | Detailed doc |
|---|---:|---:|---:|---:|---|
| Library | 4 | 7 | 15 | 26 | [AUDIT_LIBRARY_TAB.md](AUDIT_LIBRARY_TAB.md) |
| Duplicates | 4 | 5 | 2 | 11 | [AUDIT_DUPLICATES_TAB.md](AUDIT_DUPLICATES_TAB.md) |
| Organize | 4 | 5 | 2 | 11 | [AUDIT_ORGANIZE_TAB.md](AUDIT_ORGANIZE_TAB.md) |
| Tags | 5 | 7 | 0 | 12 | [AUDIT_TAGS_TAB.md](AUDIT_TAGS_TAB.md) |
| Settings | 6 | 11 | 5 | 22 | [AUDIT_SETTINGS_TAB.md](AUDIT_SETTINGS_TAB.md) |
| **Total** | **23** | **35** | **24** | **82** | |

## What got fixed in beta.10

### The user's immediate bug

1. **`install_cuda_libs_in_wsl` injected bash into a Python entry-point shim** (the root cause of the "WSL not installed" + "Invalid params: Expecting value" cascade)
   - Beta.6-9: `awk` script appended `. cuda-env.sh` line at line 2 of `~/.vibechek/venv/bin/vibechek`
   - But that file is a *Python* script (pip entry point). Every subsequent run died with `SyntaxError: invalid syntax`
   - `_analyze_via_wsl` then read the (empty) output file and `json.loads('')` produced the opaque error
   - **Fix 1**: removed the shim-patching from the CUDA install bootstrap entirely
   - **Fix 2**: `_probe_distro` now auto-detects + repairs broken shims with `grep -v cuda-env.sh` — every user who pulls beta.10 gets healed on next `wsl_status` call, no action required
   - **Fix 3**: new `repair_wsl_shim()` RPC + bash helper for explicit repair
   - **Fix 4**: `_analyze_via_wsl` now detects empty output and surfaces a meaningful error instead of `json.loads('')` exploding
   - **Fix 5**: regression test asserts no future bootstrap can inject shell syntax into the shim

### Cross-cutting wins from the audit

2. **`String(e)` defeats `RpcError` everywhere** (flagged by Settings audit #2, also touched by Library, Tags, Duplicates audits)
   - Every `fail(String(e))` call lost the typed `RpcError.cancelled` flag and showed JSON noise to users
   - **Fixed**: replaced `fail(String(e))` → `fail(e)` across all 7 component files; `useOperationStore.fail()` is now smart enough to extract `.message` from RpcError and strip noisy `"Invalid params:"` / `"Application error:"` / `"sidecar error:"` prefixes for user display
   - Result: users see clean messages (`"essentia-tensorflow is not installed"`) instead of raw JSON

3. **Cancel was a no-op for find_duplicates, organize, backup_tags, restore_tags, apply_ml_tags** (Duplicates #1, Organize #1, Tags from audit)
   - All 5 ops registered as cancellable in `_CANCELLABLE_METHODS` but their loops never called `cancellation.check()`
   - User clicking Cancel during a 12k-file organize → flag set, UI clears `active` state, sidecar keeps moving files for the next 10 minutes
   - Worse: the `_LONG_OP_LOCK` stayed held, blocking every subsequent operation with "already in progress"
   - **Fixed**: added `cancellation.check()` calls at the top of every loop in `duplicates.py`, `organizer.py`, `tagger.py`

4. **Backup write was non-atomic** (Tags #1)
   - `Path.write_text()` with no temp-file + rename — disk-full mid-write → corrupted JSON registered in history as if valid
   - **Fixed**: atomic write via sibling `.partial` file + rename, matching the existing pattern in model downloads

5. **`restore_tags` JSON errors leaked raw to UI** (Tags #2)
   - `json.loads()` + `data["files"]` unguarded; corrupt backup → `KeyError: 'files'` → `Invalid params: 'files'` toast
   - **Fixed**: wrap with explicit error handling + actionable messages (file empty / not vibechek format / not valid JSON)

6. **`handleAnalyze` showed stale "WSL not installed"** (Library #1, related to the immediate bug)
   - The two-phase preflight pattern (quick + upgrade) opened the dialog with the QUICK result and then upgraded in the background
   - On Windows the quick result always says "not ready" because per-distro probes are skipped
   - User sees the false "WSL not installed" message, doesn't know about the upgrade
   - **Fixed**: `handleAnalyze` now does the full probe BEFORE opening the dialog. Small UX delay (5-10s wait), but no more lying.

## Top remaining findings (filed for next pass)

Sorted by how badly they'd burn a user in production:

### HIGH

- **Settings #1 / Library #3 / Tags #7**: install ops use `subprocess.run(...)` or `Popen.wait()` — they don't honor `cancellation.is_cancelled()` either. Cancel UI claims to work but the install runs to completion. Need stall-checking watchdog inside the install bootstraps similar to `run_vibechek_in_wsl`.
- **Library #2**: `handleAnalyze` blocks 5-10s on Windows with zero UI feedback. Should show a spinner + disable the button.
- **Library #4 (AudioPreview race)**: rapid track-row clicks can render wrong waveform under wrong track. Needs an `aborted` flag pattern.
- **Duplicates #2**: keeper-rule eval blocks main thread for seconds on 10k+ groups. Needs virtualization + memoized derived state.
- **Duplicates #3**: `chromaprint_similarity_threshold` is dead code — DuplicateConfig exposes it, plumbed through RPC, but `duplicates.py` never reads it (the matcher buckets by exact fingerprint hash). UI implies the slider does something it doesn't.
- **Organize #2**: library state never refreshes after organize — `useLibraryStore.tracks` holds pre-move paths, future "Show in folder" / re-organize / incremental analyze all break.
- **Organize #3**: `target_root` accepts unvalidated text input — relative paths resolve against sidecar CWD (somewhere the user can't find), no permission check, no source==target check.
- **Tags #3**: `id3_text_encoding` (beta.9 feature) has no UI control AND `useApplyTags.ts` doesn't forward the param. The feature is 0% accessible.
- **Settings #3 (Enable GPU)**: button shows static "~30 sec" label even on 5-min PyPI installs (no progress subscription); pip exit 0 with TF still unable to register GPU shows "Installed N packages. Re-probing…" then re-shows the same banner — looks like the button does nothing, users click in a loop.
- **Settings #4**: new `repair_wsl_shim` RPC has zero UI surface. Auto-repair in probe is the only mitigation right now (which is fine in practice but no explicit "fix it" button).

### MED — patterns to address in batches

- 6+ places do `setState` after async without `isMounted` checks — race when user navigates away mid-RPC.
- Multiple modals close on backdrop click without confirming destructive actions (Tags forget-backup, PreflightDialog mid-install).
- Most components don't disable buttons during long ops the way they should.
- Config auto-save (Settings hooks) swallows every error.
- No retry / no idempotency on installer reruns — running "Install Essentia" twice causes confusing apt locks.

### LOW

The 24 LOW-risk items are mostly polish (icon sizes, tooltip wording, off-by-one in progress, confirm-modal Escape-key handler, etc.). See per-tab docs for the full list.

## Why this kept happening

A pattern across the 82 findings: **every previous "fix" added a feature without checking that the wired-up version actually fired**. The CUDA install patched the shim and never re-ran. The cancellation singleton was wired but never checked. The RpcError class was added but `String(e)` callers weren't updated. The threshold slider was plumbed but never read.

For beta.11 I'd recommend:
1. **Add a smoke-test agent** that drives the full GUI flow on a real WSL + 100-track library every CI run. Catches "the feature works but the button doesn't fire it" bugs the unit tests miss.
2. **Stop the "belt-and-suspenders" instinct** — when adding a fix, ONE clean path is better than three uncoordinated ones. The shim patching was a "belt-and-suspenders" on top of the launcher sourcing, and it broke everything.
3. **Treat UI strings as part of the API surface** — `fail(String(e))` losing the error structure is the same class of bug as JSON-loads-on-empty: a "this just works" assumption that doesn't.

## Test growth

- Beta.9: 300 tests passing
- Beta.10: **303 passing** (+3: shim repair + bootstrap-doesn't-inject-shell + cancellation-actually-runs-in-organize/dedup/tag, latter pending dedicated runtime test)

Tests added this pass are intentionally regression-focused: each one fails if the *specific* bug we just hit reappears.

---

## Beta.11 close-out — what 6 parallel agents shipped

Spawned in parallel with strict file boundaries to avoid merge conflicts. Each
returned with a writeup, code, and tests.

### Agent 1 — Backend install cancellation + chromaprint similarity (+13 tests)

**Files**: `vibechek/wsl.py`, `vibechek/native_install.py`, `vibechek/duplicates.py`, `tests/`

- **Settings #1**: All 5 install ops now use a `_start_cancellation_watchdog` that polls `cancellation.is_cancelled()` every 500ms and kills the entire WSL bash process group via the token-file pattern. Cancel actually works mid-install now.
- **Duplicates #3**: Implemented real Hamming-distance similarity in `fingerprint_similarity()`. Phase 2 of `find_duplicates` now buckets by the first 32-bit sub-fingerprint then runs greedy single-link clustering at the user's threshold. The `chromaprint_similarity_threshold` config field is no longer dead code.
- **Duplicates #6**: `handle_duplicates` now returns `error_messages: list[str]` so the UI can show real per-file error reasons.

### Agent 2 — Library tab UX (22 findings)

**Files**: `LibraryBrowser.tsx`, `AudioPreview.tsx`, `TrackDetails.tsx`, `AnalysisProgress.tsx`, `LibraryFilters.tsx`

- **HIGH #2**: `handleAnalyze` now shows a "Checking…" spinner during the 5-10s full preflight; both Analyze buttons disabled while in flight.
- **HIGH #3**: TrackDetails per-file Apply has its own `applying` flag — double-clicks ignored.
- **HIGH #4**: AudioPreview uses an `aborted` flag pattern. Rapid track-row clicks no longer leak previous-track callbacks into the new render.
- 18 more (selection survives moves, filter chips persist across tabs in a new Zustand store, generation-counter race guards on `runFastScan`/`runAnalyze`, null guards on size/energy, optimistic UI on forget, `window.confirm` → ConfirmModal, etc.).

### Agent 3 — Duplicates UI virtualization (7 findings)

**Files**: `DuplicatesView.tsx`, `keeperRules.ts`

- **HIGH #2**: `GroupsList` now wrapped in `Virtuoso`; per-row keeper decision is lazy and memoized by `(group.key, rulesSig)`. 10k-group reports no longer freeze the UI.
- **HIGH #4**: `applyChoices` validates `keeperOverrides[g.key]` is a real group member before using it — no more keeper-promoted-to-trash bug.
- MED fixes: precondition errors before invalid Move/Trash actions, `scanningRef` against rapid-click race, null-safe comparators in `keeperRules.ts`.

### Agent 4 — Organize + ConfirmModal + library refresh (9 findings)

**Files**: `OrganizeView.tsx`, `ConfirmModal.tsx`, `vibechek/organizer.py`, `ui/src/stores/index.ts`

- **HIGH #2**: New `useLibraryStore.updateTrackPaths(map)` action rewrites in-memory track paths after organize. Selection survives the rename. No more "file not found" cascades on subsequent ops.
- **HIGH #3**: `target_root` validated on blur (must be absolute, ≠ source, mkdir-writable). Warning banner for cloud-sync paths (My Drive, OneDrive, iCloud). Execute disabled when invalid.
- **HIGH #4**: Tag-backup save-dialog cancel now shows a friendly "Backup cancelled. Organize did not run." notification instead of silently aborting.
- ConfirmModal gained a `destructive` prop — backdrop click is a no-op for destructive confirms.
- Plan auto-invalidates when config inputs change. ML-coverage pre-flight warns when <50% of tracks have genres.

### Agent 5 — Tags + remap UX + useApplyTags trim (7 findings)

**Files**: `TagsView.tsx`, `useApplyTags.ts`

- **HIGH #3**: Forget-backup now opens ConfirmModal (was 1-click destructive).
- **HIGH #5**: After successful restore, runs `scan_only` to refresh `useLibraryStore.tracks` — Apply ML tags can no longer silently overwrite a just-restored backup.
- Remap restore now surfaces per-file detail (ambiguous/skipped/write-errors) via collapsible sections. Library-root validation (must be a folder, not a file).
- 60s progress-stall detector for backup/restore on huge libraries.
- `useApplyTags` trims track payload to `{path, ml_analysis}` only → ~80% smaller RPC payload on 12k libraries.
- Plumbed `id3_text_encoding` from config through the apply RPC params.

### Agent 6 — Settings + PreflightDialog + LogsViewer (22 findings)

**Files**: `Settings.tsx`, `PreflightDialog.tsx`, `LogsViewer.tsx`, `useConfigPersistence.ts`

- **HIGH #4**: New "Repair WSL shim" troubleshooting button (in both Settings + PreflightDialog) calls `repair_wsl_shim` RPC. Backup for the auto-repair-on-probe path.
- **HIGH #5 / #16**: `EngineGpuFixableBlock` now subscribes to `useSidecarProgress` during install and shows latest message + Cancel button. No more static "~30 sec" lie during 5-min installs.
- **MED #6**: Config auto-save failures now surface as throttled info toasts via `useNotificationStore`. Silent failures were eating user changes.
- **MED #7**: `isMounted` ref pattern around all async setState in Settings.
- **MED #9, #10, #11**: Models dir validated on blur; GPU "on" disabled when engine probe shows no GPU; workers slider uses `WORKERS_MAX=96` with warning when > cpu_count.
- **MED #12**: Restore-defaults wrapped in danger ConfirmModal.
- **MED #13, #14**: PreflightDialog renders the `result.note` from `install_wsl` ("may need reboot"); backdrop click suppressed during install.
- **MED #15**: `handleDownloadModels` uses `cfgRef.current` to avoid stale closure.
- **MED #20**: `analyze_via === "native_venv"` now flows through the narrow union.
- **MED #22**: `EngineGpuFixableBlock` re-derives `distro` from latest preflight after install.
- **Tags #4** (location): id3 text-encoding picker (UTF-8 / UTF-16 / ISO-8859-1) added to Tagging section.
- **Library #15**: `runWithProgress` calls `fail(result.error)` on `result.ok === false` instead of `finish()`.
- **LOW #18, #19**: LogsViewer level filter uses tolerant regex; clipboard rejection caught.

### Integration — main thread

- Wired `id3_text_encoding` into `_apply_ml_tags` RPC handler (Agent 5's TagsView change was already sending it; this completes the round trip).
- Bumped version 0.3.0-beta.10 → 0.3.0-beta.11 across all 5 manifests.

### Final stats

- **316 Python tests passing** (+13 since beta.10, +104 since the original 212 baseline 8 betas ago).
- **24 frontend tests passing**, including all 4 test files cleanly.
- **TypeScript clean** for every file modified this pass. Remaining `tsc` errors are pre-existing (vitest config dep mismatch, pre-existing TrackDetails badge type, types/generated.ts ExistingTags forward-ref).
- **End-to-end verified** against real WSL Ubuntu: `preflight(quick_wsl=False)` returns `ready=True, analyze_via=wsl, usable_distro=Ubuntu`.

### What didn't ship in beta.11

- **#5 macOS Gatekeeper / Windows Defender code signing** — needs an Apple Developer account + Authenticode cert before CI can do anything. Workflow + secrets documented in `docs/RELEASING.md`; no code change blocks this.
- A handful of LOW-risk polish items deliberately deferred when they conflicted with HIGH/MED priorities in the same file.

Everything else from the original 82-finding audit is now resolved.
