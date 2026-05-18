# Library Tab — Button & Flow Audit

A read-only audit of every interactive element on the Library tab and the RPC
flows behind them, focused on surfacing silent-failure modes and other
landmines before users hit them.

Scope:
- `ui/src/components/LibraryBrowser.tsx`
- `ui/src/components/TrackDetails.tsx`
- `ui/src/components/AudioPreview.tsx`
- `ui/src/components/AnalysisProgress.tsx`
- `ui/src/components/LibraryFilters.tsx`
- `ui/src/components/PreflightDialog.tsx`
- `ui/src/components/ConfirmModal.tsx`
- RPC handlers in `vibechek/rpc.py`
- `ui/src/hooks/useApplyTags.ts`, `ui/src/hooks/useSidecar.ts`
- `ui/src/stores/index.ts`

Findings sorted by risk descending.

---

## 1. `handleAnalyze` swallows preflight failure as raw JSON / `[object Object]`

**Risk**: HIGH
**Button / flow**: "Analyze with ML" / "Re-analyze all"
**File**: `ui/src/components/LibraryBrowser.tsx:316-318`

**What breaks**: The catch passes `e` (an `RpcError` object) straight to
`fail(e)`. The operation store does `String(error)` on it
(`stores/index.ts:108`), so `RpcError` ends up stringified as either its
`.message` (parsed) or the raw JSON-RPC error envelope. If the sidecar handler
throws an exotic exception (e.g. WSL probe raises during preflight), the user
sees the same opaque JSON blob that already burned them once.

Every other handler in this file calls `fail(String(e))` for at least *some*
shape control. This one is the only handler that just passes `e` through —
inconsistent with the rest of the file, and the line is right next to the
"preflight" comment that's already painfully familiar from the last bug.

**Reproduction**: Make `preflight()` raise (e.g. corrupt `wsl_status` cache,
kill `wsl.exe` mid-probe). Click "Analyze with ML". Banner shows the JSON
error.

**Fix**: Match the pattern used by every other handler — `fail(String(e))` or
better, `fail(e instanceof RpcError ? e.message : String(e))`. Long-term,
push the `String(e)` defensive call out of the store and into a shared helper
so every catch site is consistent.

---

## 2. Quick preflight then full preflight = ~10s of unexplained "nothing happening"

**Risk**: HIGH
**Button / flow**: "Analyze with ML" / "Re-analyze all"
**File**: `ui/src/components/LibraryBrowser.tsx:296-319`

**What breaks**: When the user clicks Analyze, `handleAnalyze` may fire TWO
preflight calls back-to-back: a quick probe (sub-second), then a full one
(5-10s on Windows because of the WSL distro probe). During those 10 seconds:
- No spinner appears (no `begin()` call before the awaits).
- The button is *not* disabled (it checks `active !== null`, but no operation
  is active during preflight — `begin()` only fires inside `runAnalyze`).
- The user can click "Analyze with ML" again — which fires another doubled
  preflight pair. On Windows this can stack several slow probes.
- Looks identical to a hang. The user clicked, nothing happened.

Comment at line 290-295 acknowledges "The user sees nothing for the 5-10s the
probe takes" as an explicit trade-off. That's the bug. The trade-off was made
to avoid showing a wrong dialog, not to give the user no feedback at all.

**Reproduction**: On Windows with WSL installed + Ubuntu present + essentia
installed: click "Analyze with ML". Stopwatch from click → either `runAnalyze`
spinner or preflight dialog. Expect 5-10s of dead UI.

**Fix**: Either (a) wrap the whole `handleAnalyze` in a transient "checking…"
state that disables the button, OR (b) call `begin("analyze")` before the
quick probe so AnalysisProgress shows. Also need a re-entrancy guard so the
second click is a no-op.

---

## 3. Single-track "Apply ML tags" button has no idempotency guard for double-clicks

**Risk**: HIGH
**Button / flow**: "Apply ML tags to this file" (TrackDetails right rail)
**File**: `ui/src/components/TrackDetails.tsx:127-134`

**What breaks**: The button disables itself when `active !== null`, but
there's a window between the user's click and the synchronous `begin("tag")`
inside `applyTags`. React event handlers do not block re-entry, and Tauri's
`invoke` is async. If the user double-clicks (or the click handler fires
twice due to a sticky mouse), two `apply_ml_tags` RPCs can race to the
sidecar.

The sidecar *does* have `_LONG_OP_LOCK` (`vibechek/rpc.py:51`) so the second
will be rejected with `"Another long-running operation (...) is already in
progress"`. That rejection is a *failure* in the UI: it flips the operation
store into the error state, clobbering the first op's "in-progress" state
and showing the user a scary banner about a busy op — even though the first
op is succeeding.

Same pattern in `LibraryBrowser.tsx` "Apply ML tags to N" buttons (lines
497-504, 511-520), and "Analyze new" / "Re-analyze all" (lines 522-541).

**Reproduction**: Add a 200ms delay to `apply_ml_tags` then double-click the
button. Or just hold the mouse button down and rapid-click.

**Fix**: Use a local `isPending` ref in the handler that synchronously
short-circuits a second invocation, independent of the React state cycle.
Better: make the operation store's `begin` itself return false if an op is
already active, and have handlers respect that.

---

## 4. `AudioPreview` rebuilds WaveSurfer on every `path` change with no cancellation

**Risk**: HIGH
**Button / flow**: Track row click → right-rail audio preview
**File**: `ui/src/components/AudioPreview.tsx:32-72`

**What breaks**: If the user clicks through tracks faster than WaveSurfer
can decode (1-3s on long FLACs per the comment), the effect's cleanup
destroys the previous WaveSurfer instance — but the `WaveSurfer.create({
url, ... })` from the old click may still resolve. WaveSurfer's `on('error')`
and `on('ready')` handlers are attached to a destroyed instance; the
`setReady` / `setError` calls happen on an unmounted (or now-stale) state and
race the new path.

Symptoms users will see:
- Wrong waveform appears under a different track's name (transient).
- "Could not load audio" flashes for a track that actually loads fine.
- Memory leak: aborted WebAudio decodes never get GC'd.

There's also no error path for the "convertFileSrc returned a URL the
webview can't fetch" case (e.g. path with `#` or `?` characters). The Tauri
asset protocol has quirks with certain Windows paths.

**Reproduction**: In a library with mostly long FLACs, click row 1 → row 2
→ row 3 → row 1 in rapid succession. Watch for waveform/title mismatch and
spurious "could not load" overlays.

**Fix**: Add an `aborted` flag in the effect that all the `setState` callbacks
check before firing. Pattern is standard in React data-fetching effects.

---

## 5. `handleForgetRecent` swallows errors via try/finally with no catch

**Risk**: MED
**Button / flow**: Right-click recent library card → confirm forget, OR the
`X` button on a recent library card
**File**: `ui/src/components/LibraryBrowser.tsx:130-136`

**What breaks**: The RPC error is swallowed entirely — the `finally` block
runs `refreshRecent`, but the underlying failure (e.g. permissions on the
recent-libraries state file, disk full) never surfaces. The card visibly
disappears from the UI because `refreshRecent` returns the same list, then
reappears on next mount. The user thinks the forget worked.

`refreshRecent` itself also silently swallows on line 98-100, blanking the
recents list to `[]`. If `library_state` RPC fails for a transient reason,
the user loses their entire recent-libraries display and the "Welcome back"
screen disappears. They get the cold-start "Open folder" screen with no
indication anything went wrong.

**Reproduction**: Make the `forget_library` RPC throw (lock the state file).
Click the X on a card. Card seems to disappear, then reappears.

**Fix**: Catch + surface via notification store. For `refreshRecent`,
distinguish "no recents" from "couldn't load recents" so the UI doesn't lie
about state.

---

## 6. `handleOpenRecent` doesn't validate report shape — `result.report.tracks` can throw

**Risk**: MED
**Button / flow**: Recent library card click
**File**: `ui/src/components/LibraryBrowser.tsx:117-124`

**What breaks**: `result.report` is typed as `AnalysisReport` but coming
from disk via `library_state.load_analysis`. If the saved JSON is from an
older Vibechek version with a different schema, or is partially corrupt
(e.g. `tracks` missing, replaced with `null`, or some entries lack `path`),
the code blindly does `setTracks(result.report.tracks)`. If `tracks` is
missing it throws `Cannot read properties of undefined`, which is then
caught by line 125-127 and stringified — exactly the opaque "JSON parse
error" UX the user hates.

The sidecar handler `_load_recent_analysis` reads the file with
`json.loads` and returns it raw on line 615 — no schema validation. A
corrupt file passes through.

**Reproduction**: Manually corrupt a saved analysis JSON (remove the
`tracks` key). Click the recent card.

**Fix**: Validate `result.report.tracks` is an array before calling
`setTracks`. On failure, fall back to a fresh scan and notify the user the
saved analysis was unreadable.

---

## 7. `handleAnalyze` falls through to `runAnalyze` if `quick.ready` but the lock contends

**Risk**: MED
**Button / flow**: "Analyze with ML" / "Re-analyze all"
**File**: `ui/src/components/LibraryBrowser.tsx:301-304`

**What breaks**: Race between two cancellable ops. When `quick.ready` is true
we immediately call `runAnalyze()`. But if any other long op kicked off in
the last few hundred ms (e.g. user clicked "Apply ML tags to all" then
"Analyze with ML" before the first one's `begin` registered the lock), the
sidecar rejects the second with `INVALID_REQUEST` and `{busy: true}`. That
error flows up to `fail(String(e))` and shows the user an inscrutable
"Another long-running operation ('tag') is already in progress" message.

The error message itself is fine in CLI, but the GUI shows it raw in the red
banner. There's no UI affordance for "the previous op is still running,
wait" — the buttons just appear disabled, then maybe not, depending on
timing.

**Reproduction**: Click "Apply ML tags to all" → very quickly click
"Re-analyze all".

**Fix**: In `LibraryBrowser`, when the sidecar returns `data.busy=true`,
don't treat it as failure — show a transient toast and leave the original
op's progress UI alone. Better: make `useOperationStore.begin` check
`active` and refuse to start a second op, never even sending the RPC.

---

## 8. `useApplyTags` sends every track's full analysis payload over the RPC pipe

**Risk**: MED
**Button / flow**: "Apply ML tags to {N}" / "Apply ML tags to all"
**File**: `ui/src/hooks/useApplyTags.ts:53-58`

**What breaks**: The RPC payload is `{ analysis: { tracks } }` — the entire
in-memory tracks array, serialised to JSON, written over stdin to the
sidecar. For a 12k-track library this is multiple MB. The newline-delimited
JSON-RPC frame either:
- Blocks the React thread for several hundred ms during `JSON.stringify`
  (UI freeze, no spinner during the freeze because `begin` happens *after*
  the stringify in the await chain).
- Blocks the Tauri stdin writer if the sidecar pipe is full.
- If the line is large enough, hits OS pipe buffer limits and stalls.

No upper bound on input size; the only mitigation is the user not having
12k+ tracks. The sidecar already has `analysis_path` support (rpc.py:665)
that would let the UI write to a tempfile and pass a path — but the UI never
uses that path.

**Reproduction**: Load a 10k+ track library. Click "Apply ML tags to all".
Measure time-to-spinner.

**Fix**: For large payloads (>1k tracks), write to a tempfile via Tauri's
`fs` API and pass `analysis_path` instead. Alternatively, the sidecar should
be willing to read this from its own copy on disk (the analyze step already
auto-saves).

---

## 9. `cancel_operation` button in `AnalysisProgress` ignores its own errors

**Risk**: MED
**Button / flow**: "Cancel" button on the floating progress panel
**File**: `ui/src/components/AnalysisProgress.tsx:55`

**What breaks**: `onClick={() => { void rpc("cancel_operation"); }}` — fire
and forget, no `.catch`, no feedback. If the RPC fails (sidecar dead, pipe
broken), the user has no way to know the cancel didn't land. They sit
watching the spinner spin forever, click cancel again, again, again, each
producing nothing.

Same fire-and-forget pattern in `PreflightDialog.tsx:68-74` (handleCancel),
where the catch block is an empty comment.

**Reproduction**: Kill the sidecar process mid-analyze. Click Cancel.
Spinner persists; no error surfaces.

**Fix**: Either await the cancel and show an error if it fails, or after
~2s with no progress event and no completion, surface "Cancel didn't take —
sidecar may be unresponsive. Restart the app."

---

## 10. `runAnalyze` race: tracks state set from `report.tracks` can clobber a parallel `scan_only`

**Risk**: MED
**Button / flow**: "Just show me my library" → then immediately "Analyze
with ML" before the first completes
**File**: `ui/src/components/LibraryBrowser.tsx:224-263`

**What breaks**: `runFastScan` and `runAnalyze` both call `setTracks(...)`
on completion. The sidecar lock prevents both from running simultaneously,
but the UI lock is `active !== null`. There's a window between an op
completing on the sidecar (which sends the response) and `finish()` running
on the next React tick. If the user clicks the second button in that
window, two RPCs go out — the second is rejected per finding #7, and the
error obliterates the first op's just-loaded tracks via the operation
store's `set({ active: null, ..., error: msg })`.

The track data itself is fine (set via a different store), but the user
sees their just-completed scan immediately replaced by a scary red banner
saying it failed — which is a lie.

**Reproduction**: Rapidly click "Just show me my library" then "Analyze
with ML". Watch operation store transitions in devtools.

**Fix**: Same as #3/#7 — make `begin()` reject if already active, and don't
let a rejected op clear the previous op's success state.

---

## 11. `setSelectedTrack` doesn't validate that the path still exists in tracks

**Risk**: MED
**Button / flow**: Track row click → opens TrackDetails
**File**: `ui/src/components/TrackDetails.tsx:28-31`

**What breaks**: `TrackDetails` does
`tracks.find((t) => t.path === selectedPath) ?? null`. If the user opens a
detail pane, then runs a fresh analyze, the new `tracks` array may not
contain the previously selected path (e.g. file deleted between scans, or
case-sensitivity mismatch on Windows). The detail pane silently closes,
losing the user's place.

Less critical: if the user re-runs analyze and the path *is* still there
but `ml_analysis` was cleared by the merge logic, the detail pane shows the
"No ML analysis for this track yet" notice — confusing right after they
just clicked Analyze.

The `setSelectedTrack` callback in `LibraryBrowser.tsx:586` doesn't pass any
fallback or persist the selection through re-renders.

**Reproduction**: Click a row to open details. Run "Re-analyze all". Detail
pane vanishes.

**Fix**: When tracks update and the selected path is no longer present,
notify the user ("track no longer in library") rather than silently closing.

---

## 12. `filters` chip state is local to LibraryBrowser; lost on tab switch

**Risk**: LOW
**Button / flow**: Genre/Energy/Mood/Vocal filter chips
**File**: `ui/src/components/LibraryBrowser.tsx:88`, `LibraryFilters.tsx`

**What breaks**: Filter state is `useState`, not in the library store. If
the user switches to the Dedup or Tags tab and back, all filters reset.
Same for `showErrorsOnly`. Easy thing to miss when refactoring; users with
a filter set, who tab away to compare with Dedup, will be unpleasantly
surprised.

Also: filter chips compute `genres = Array.from(g).sort()` on every render
of `FilterChips` even though `tracks` rarely changes. Not a correctness
issue but the `useMemo` only dedupes by tracks identity — which `setTracks`
creates anew on every analyze.

**Reproduction**: Apply genre filter "House". Switch to Dedup tab. Switch
back. Filter is gone.

**Fix**: Lift filter state into the library store. Persist `showErrorsOnly`
the same way.

---

## 13. `TrackRow` reads `track.size_mb.toFixed(1)` without null guard

**Risk**: LOW
**Button / flow**: Render of every track row
**File**: `ui/src/components/LibraryBrowser.tsx:724`

**What breaks**: If a record has `size_mb` missing/null (the error branch
in `_scan_only`, lines 200-203 of rpc.py, sets `size_mb: 0.0` — fine, but
older saved analyses from a buggy version may have set `size_mb: null`),
`toFixed` throws "Cannot read properties of null". React then unmounts the
list and the entire library view crashes with a blank screen + the
error-boundary text (if any).

Same risk in `TrackDetails.tsx:146` (`track.size_mb.toFixed(1)`).

**Reproduction**: Load a saved analysis where one track has `size_mb:
null`. Library renders blank.

**Fix**: `(track.size_mb ?? 0).toFixed(1)` everywhere, plus null-check the
loaded analysis report fields per #6.

---

## 14. `EnergyBar` reads `ml.ml_energy` and `DiffSection` does numeric coercion

**Risk**: LOW
**Button / flow**: Track row energy bar, TrackDetails diff section
**File**: `ui/src/components/LibraryBrowser.tsx:720`, `TrackDetails.tsx:222`

**What breaks**: `<EnergyBar level={Number(v)} />` in DiffSection (line 222).
If the existing tag's energy is `"high"` (a string), `Number("high")` is
`NaN`, which propagates into whatever EnergyBar renders. Probably draws
nothing or full-bar, depending on the implementation — either way silently
wrong.

Similarly the diff section does `String(row.existing).toLowerCase() !==
String(row.next).toLowerCase()` to detect "changed". Comparing existing
genre `"House"` to ML subgenre `"Tech House"` shows "changed" — which is
*technically* what was asked, but if the existing tag is `"house"`
lowercase the comparison passes as unchanged even though the ML writes
subgenre. Subtle but the user sees no arrow even though Apply will write
something.

**Reproduction**: Track with existing genre `"house"`, ML subgenre
`"Tech House"`. DiffRow shows them as unchanged.

**Fix**: Compare against the value that will actually be written
(subgenre if present, else genre) instead of the displayed `next`.

---

## 15. PreflightDialog's `runWithProgress` finalizes via `finish()` even on `result.ok=false`

**Risk**: LOW
**Button / flow**: Install WSL / Install Ubuntu / Install Vibechek+Essentia
in PreflightDialog
**File**: `ui/src/components/PreflightDialog.tsx:98-115`

**What breaks**: If `result.ok` is false, the code calls `fail(result.error
?? "install failed")` but *also* leaves `actionMessage` set. The
`AnalysisProgress` floating panel shows the error via `errorMsg`, AND the
PreflightDialog renders `actionMessage` in a red box. Two error displays for
one error. Not a *bug* exactly, but redundant.

More importantly: after a failed install, `reCheck()` is not called, so the
dialog's `preflight` prop is stale. The user might re-click the same button
expecting fresh state and get… exactly the same state, because nothing
refreshed. The `actionMessage` lingers from the last attempt and confuses
the next.

**Reproduction**: Trigger an install failure (e.g. revoke WSL admin
permissions mid-install). Watch the dialog show the error in two places.
Click the same button again — no re-probe.

**Fix**: Single source of truth for the error display, and call `reCheck()`
(or at least clear `actionMessage`) before retrying.

---

## 16. Bulk-tag confirm modal still uses cached `selectedIds.size` after re-render

**Risk**: LOW
**Button / flow**: "Apply ML tags to {N}" → Confirm modal
**File**: `ui/src/components/LibraryBrowser.tsx:592-651`

**What breaks**: The modal title uses `selectedIds.size` directly. If the
user opens the confirm modal, then the selection somehow changes (e.g. a
keyboard shortcut deselects all), the title and the "tracks" count desync
from what `tagPreview` was computed for. `tagPreview` is memoized on
`selectedIds`, so they should refresh together, but the title says "Apply
ML tags to 7 tracks?" while the body shows preview for 7 — and if the user
*had* deselected 3 of them just before clicking, the modal still uses the
selection at modal-open time? Actually no: it reads live state on each
render.

The real bug: there's nothing preventing selection from changing while the
modal is open. Modal is non-modal in the sense that the underlying tracks
list still responds to clicks (the dim background catches clicks but
keyboard shortcuts and the row-checkbox clicks can still mutate selection).
Pressing Confirm then operates on the current selection — possibly
different from what the user reviewed.

**Reproduction**: Open the confirm modal. Use keyboard or a stray click on
the checkbox to mutate selection. Confirm — see counts mismatch what was
displayed.

**Fix**: Snapshot `selectedIds` into local state when the modal opens, use
that snapshot for both the title and the `runBulkTag` invocation.

---

## 17. `Header.onOpen` button has no `disabled={active !== null}`

**Risk**: LOW
**Button / flow**: "Open folder" header button (top-right, always visible)
**File**: `ui/src/components/LibraryBrowser.tsx:665-668`

**What breaks**: The header's "Open folder" button is enabled even during a
running analyze/dedupe/tag. Clicking it opens the OS folder picker; if the
user picks a folder, `handleOpenFolder` runs which calls `setLibraryPath`,
`setTracks([])` (wiping the library), then `begin("analyze")` — which the
sidecar will reject because the previous op holds the lock. The user has
just wiped their in-progress library for no reason.

**Reproduction**: Start an analyze on a big library. Click "Open folder" in
the header. Pick any folder. Library wipes; error banner; previous analyze
still running on the sidecar with no UI to track it.

**Fix**: Disable the header Open Folder button while `active !== null`, or
prompt to cancel first.

---

## 18. `applyFilters` energy mapping uses `-1` sentinel that could collide

**Risk**: LOW
**Button / flow**: Energy filter chip
**File**: `ui/src/components/LibraryFilters.tsx:50-51`

**What breaks**: `const e = ml?.ml_energy ?? -1;` — uses -1 to mean "no
analysis". A track with an actual energy of -1 (shouldn't happen but ML
models can produce weird values) would match the "no energy" bucket. Same
pattern, no big deal in practice.

More concerning: filtering by a specific energy hides all unanalyzed
tracks silently. If the user picks energy=3 they get only analyzed tracks
at level 3 — which is the intent, but there's no "unanalyzed" pseudo-row
so the user can't tell at a glance whether their filter is hiding real
data or just genuinely empty buckets.

**Reproduction**: Apply energy=3 filter to a mostly-unanalyzed library.
"0 / 12000" displayed with no indication why.

**Fix**: Show a "(N unanalyzed tracks hidden by filter)" note when filters
exclude unanalyzed tracks.

---

## 19. `_emit_progress` rate-limits but UI shows last message indefinitely

**Risk**: LOW
**Button / flow**: Any long op (analyze, tag, dedupe) — the AnalysisProgress
panel
**File**: `vibechek/rpc.py:107-126`, `AnalysisProgress.tsx`

**What breaks**: Sidecar throttles progress to 20/sec. If the sidecar
hangs (e.g. inside a TF model load on a slow model), the last progress
message stays pinned on screen while the spinner spins. The user sees
"23/1000: track_24.mp3" frozen for minutes with no way to tell if it's
working or stuck. No "no progress in N seconds" heartbeat warning.

`startedAt` is tracked, so the elapsed counter ticks, but that's the only
liveness indicator. A user staring at the same `current/total` for 5
minutes will (rightfully) assume it's dead.

**Reproduction**: Analyze a library; midway through, suspend the sidecar
process (`SIGSTOP` on Unix). Watch the UI: elapsed keeps counting; progress
text and `pct` freeze.

**Fix**: Track time since last progress event in the UI. After 30s without
a tick, show "no progress in 30s — sidecar may be stuck" with a button to
kill the sidecar.

---

## 20. `convertFileSrc` in AudioPreview can fail silently for certain paths

**Risk**: LOW
**Button / flow**: Track row click → audio preview load
**File**: `ui/src/components/AudioPreview.tsx:43`

**What breaks**: Tauri's `convertFileSrc` doesn't validate the path; it
just URL-encodes and prepends the asset protocol scheme. For paths with
characters that Tauri's asset handler rejects (very long, NTFS junctions,
network shares without proper config), WaveSurfer will hit a 404 or CORS
error. The `error` handler at line 64 catches WaveSurfer errors, but
network/protocol errors from the underlying `fetch` may not all reach the
handler — some surface only in the browser console.

Also: Tauri's asset protocol requires the path to be in the allowlist
(via `tauri.conf.json`). If a user's library is somewhere outside the
allowed roots, every audio preview fails with "Could not load audio" and
no further info.

**Reproduction**: Open a library on a network share. Click any row.
Waveform fails to load; error message is unhelpful.

**Fix**: Catch the path-decode/protocol error explicitly and tell the user
"this path can't be previewed — Vibechek's asset handler doesn't allow
network paths" or similar.

---

## 21. `runBulkTag` notifies success even when many errors occurred

**Risk**: LOW
**Button / flow**: "Apply ML tags to {N}" / "Apply ML tags to all"
**File**: `ui/src/components/LibraryBrowser.tsx:268-285`

**What breaks**: The notification kind is `"info"` if there were any
errors, `"success"` otherwise. But the message itself says
`"Tagged ${applied + other} files"` with no acknowledgment in the headline
that some failed. If 500 files succeed and 200 fail, the user sees
"Tagged 500 files" with errors only visible in the detail expansion.

Also: notifications auto-dismiss (per the notification store). A user who
glances away during a long tag op may miss the toast entirely and not
realize 200 files failed.

**Reproduction**: Run apply-tags on a library with many read-only files.
Notice the cheerful green "Tagged N files" toast.

**Fix**: When `errors.length > 0`, prefix message with "Partial" or
similar, and set kind to "info" or maybe a new "warning" kind. Or surface
the error count in the operation store as a persistent error banner.

---

## 22. `forget_library` recent card removal isn't optimistic — full re-fetch

**Risk**: LOW
**Button / flow**: Right-click recent card → confirm forget, or X button
**File**: `ui/src/components/LibraryBrowser.tsx:130-136`

**What breaks**: After forget, the UI calls `refreshRecent` which re-fetches
from the sidecar. There's a network-level race: if the user forgets card A
then immediately card B before the first refresh lands, the second forget's
refresh sees the post-A state, then the first forget's refresh lands and
sees a state with B still present (because the first refresh was queued
before B was forgotten). Final UI state: depends on which refresh wins.

Not catastrophic — closing the app refetches — but UI flickers and may
briefly show a forgotten card again.

**Reproduction**: Quickly X out two recent cards in succession.

**Fix**: Optimistic UI — remove from local state immediately, refetch in
the background, reconcile.

---

## 23. `runAnalyze(incremental=true)` Map merge keeps stale tracks indefinitely

**Risk**: LOW
**Button / flow**: "Analyze new ({unanalyzedCount})" button
**File**: `ui/src/components/LibraryBrowser.tsx:251-258`

**What breaks**: Incremental analyze merges new results into existing
tracks by path. If a file was deleted/renamed since the last analyze and
the new pass doesn't see it, the stale entry persists in the in-memory
tracks list forever. User sees a ghost track that "exists" in the library
view but produces an audio-preview error on click.

The sidecar's `find_audio_files` only returns currently-present files;
that result populates `report.tracks`. The UI's merge keeps the union.
Fast scan (non-incremental) would correctly drop the missing files.

**Reproduction**: Analyze a library. Delete a file from disk. Click
"Analyze new". Deleted file still shows in the list.

**Fix**: After incremental analyze, drop entries whose `path` doesn't
exist among the new report's full scan. Sidecar could return both the
new-tracks list and the full current-paths set so the UI can prune.

---

## 24. `RecentLibraryCard` uses `window.confirm` for the destructive forget action

**Risk**: LOW
**Button / flow**: Right-click recent card
**File**: `ui/src/components/LibraryBrowser.tsx:740-746`

**What breaks**: `window.confirm` is OS-native, doesn't match the app's
visual style, blocks the JS thread, and can't be tested. Also: on some
Linux WMs / older Windows builds, `window.confirm` inside a Tauri webview
can render *behind* the main window. User clicks right-click → forget,
nothing visibly happens, app is frozen. They click around, eventually
Alt-tab finds the modal.

The rest of the app uses `ConfirmModal` for this purpose. Inconsistent.

**Reproduction**: Right-click a recent card on a multi-monitor Linux
setup with a tiling WM.

**Fix**: Replace with `ConfirmModal`.

---

## 25. `applyTags` ignores `tracks.length === 0` after filtering in `runBulkTag`

**Risk**: LOW
**Button / flow**: "Apply ML tags to {N}" with all selected tracks
unanalyzed
**File**: `ui/src/components/LibraryBrowser.tsx:272`, `useApplyTags.ts:50`

**What breaks**: `runBulkTag` early-returns if `targets.length === 0`.
Good. But `applyTags` also early-returns if `tracks.length === 0` —
returning `null`. The check is doubled and the failure modes diverge: if
the user selects 10 unanalyzed tracks and clicks Apply, `runBulkTag` passes
all 10 to `applyTags` which sends them to the sidecar; the sidecar writes
nothing useful (no ml_analysis to write); the result reports `applied: 0,
skipped: 0, other: 0` and the success toast says "Tagged 0 files".

Not strictly broken but a confusing no-op.

**Reproduction**: Select unanalyzed tracks. Click Apply ML tags. See
"Tagged 0 files" success toast.

**Fix**: Pre-filter to analyzed tracks before sending; show a notice if
the filter empties the selection.

---

## 26. Nothing refreshes `recentLibraries` after a successful `analyze_directory`

**Risk**: LOW
**Button / flow**: Successful "Analyze with ML" run
**File**: `ui/src/components/LibraryBrowser.tsx:238-263`, `94-105`

**What breaks**: The sidecar's `_analyze_directory` calls
`library_state.record_analysis` (auto_save=True by default), updating the
recent-libraries file. But the UI's `recentLibraries` state is only
populated on mount via `refreshRecent`. After a successful analyze, the
recent-libraries list in memory is stale.

If the user clears the library and goes back to the welcome screen (e.g.
by opening a different folder that fails to scan), they may see an
outdated `analyzed_count` or even a stale "last opened" timestamp on the
card for the library they just analyzed.

**Reproduction**: Run analyze on a fresh library. Then close the app and
reopen — see correct counts. Compare against in-app counts which were
never refreshed.

**Fix**: Call `refreshRecent` in the `runAnalyze` success path.

---

# Summary

26 findings total.

- **HIGH**: 4
- **MED**: 7
- **LOW**: 15

**Top 3 to fix first**:

1. **#1** — `handleAnalyze` swallows preflight failure as raw JSON (this is
   the exact UX disaster the user just hit, with a different RPC).
2. **#2** — Quick + full preflight gives 5-10s of unresponsive UI with no
   feedback before the dialog opens or analyze starts.
3. **#3** — Single-track Apply button has no idempotency guard; double-click
   triggers a sidecar lock-rejection that overwrites the in-progress op's
   state with a scary error banner.
