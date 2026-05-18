# Organize Tab — Button-Flow Audit

Read-only audit of `OrganizeView.tsx`, `ConfirmModal.tsx`, and the
`plan_organization` / `organize` RPC handlers + `organizer.py`. Focus: failure
modes the user actually hits — silent swallows, stale state, missing
pre-flight, races, and destructive operations without escape hatches.

11 findings. **Risk tally: 4 HIGH, 5 MED, 2 LOW.**

---

## 1. Cancel button does nothing during organize — moves continue silently
**Risk**: HIGH
**Button / flow**: AnalysisProgress overlay → "Cancel" while organize runs
**File**: `vibechek/organizer.py:165-173` (the move loop) — never imports or
calls `vibechek.cancellation.check()`. The RPC dispatch registers the op as
cancellable (`rpc.py:716` `"organize": "organize"`), so the user sees the
Cancel button, clicks it, the flag flips, but the loop ignores it.

**What breaks**: User clicks Cancel mid-organize on a 12k-track library. The
progress bar keeps advancing. The frontend has already shown the spinner;
nothing tells them the cancel was ignored. Files keep moving until the loop
exhausts the plan. They'll learn after the fact that another 4,000 files moved
after they hit Cancel.

This is exactly the "cancel doesn't actually stop mid-batch" case called out in
the prompt.

**Reproduction**:
1. Plan an organize over a large library (5k+ tracks).
2. Click Execute. As soon as progress moves, click the Cancel chip.
3. Watch the progress percentage continue climbing. Sidecar logs show no
   `CancelledError` raised.

**Fix**: Add `cancellation.check()` inside the `for i, move in enumerate(...)`
loop in `organize_from_analysis`. The handler in `rpc.py:777` already turns
`CancelledError` into `data={"cancelled": true}`, which `useOperationStore.fail`
already silences. Just need the loop to honour the flag. (Same pattern as
analyze / dedupe — easy port.)

---

## 2. Library state is never refreshed after organize — every subsequent op operates on stale paths
**Risk**: HIGH
**Button / flow**: Execute → all moves succeed → "View library"
**File**: `OrganizeView.tsx:160-168` — `setResult(...)` + `setPlan(null)` but
`useLibraryStore.tracks` is left holding the pre-move paths.

**What breaks**:
- Click "View library" after organize → every track row points to a path that
  no longer exists on disk. Selecting a track and trying to play / re-tag /
  show in folder fails with `FileNotFoundError`.
- Re-running organize on the "same" library produces a plan with hundreds of
  `File not found:` entries in `plan.errors` (organizer.py:110), because
  `plan_organization` calls `source.exists()` against the OLD path.
- Re-running ML analysis (incremental) sees zero matches in `skip_paths` and
  re-analyzes everything from scratch.

The prompt explicitly asks: *"does the UI remember the new paths so future runs
find tracks at their new locations?"* — No. The user has to manually click "Open
folder" again to rescan from disk.

**Reproduction**:
1. Analyze a library (load 200 tracks into memory).
2. Organize using in-memory source. All moves succeed.
3. Click "View library". Click any track → "Show in folder" fails.
4. Click Organize again → preview shows 0 moves and N planning errors.

**Fix**: After a successful organize, either (a) run `scan_only` against
`plan.base_dir` and `setTracks(report.tracks)` to refresh, or (b) rewrite each
`track.path` in the existing array using the `capturedPlan.moves` map
(source → destination). Option (b) preserves ML analysis fields; (a) discards
them. Option (b) is the right call.

---

## 3. `target_root` accepts any string — silently creates folders under sidecar CWD
**Risk**: HIGH
**Button / flow**: Target root text input → Preview → Execute
**File**: `OrganizeView.tsx:276-286` (input has no validation), and
`rpc.py:442` blindly wraps it in `Path(...)`.

**What breaks**: The text input takes any string. If the user types something
non-absolute like `My Genres` (or pastes a path with a typo like
`C;\Music\sorted`), `Path("My Genres")` is a relative path. `plan_organization`
sets `base_dir = Path("My Genres")`, and the destinations are
`Path("My Genres/House/track.mp3")`. `mkdir(parents=True, exist_ok=True)` then
creates `My Genres/` **relative to the sidecar's CWD** — which on Windows
PyInstaller builds is something like `C:\Program Files\Vibechek\` or wherever
the user happened to launch from. The user has no idea where their music went.

Other unchecked failure modes for this field:
- Path to a folder the user doesn't own / can't write to → planning succeeds,
  every `shutil.move` raises `PermissionError`, captured into `stats.errors`,
  surfaced as a yellow "Mostly done" banner with a wall of error strings.
- Path to the source library itself → no-op for tracks already in
  `base_dir/Genre/`, but generates collision-renamed duplicates for the rest.
- Path on a different drive → `shutil.move` falls back to copy+delete, which
  is slow and not atomic; partial cancellations leave half-copied files.
- Path inside a OneDrive / Google Drive virtual-FS folder → exact landmine
  the existing audit `_probe_install_path` warns about for the install path,
  but completely unguarded for the user's chosen output.

**Reproduction**:
1. In Target root, type `sorted_music` (no drive letter, no leading slash).
2. Preview shows a plan rooted at `sorted_music/`. Execute.
3. Files land under wherever the Tauri shell's CWD is. Most users will never
   find them.

**Fix**: Validate the field on blur and on Preview:
- Require an absolute path (`Path(x).is_absolute()`).
- Verify it exists OR its parent exists (catch typos before any move).
- Verify it's writable (`os.access(x, os.W_OK)` + a `tempfile` probe).
- Warn (don't block) if it matches `_RISKY_PATH_SUBSTRINGS` from `rpc.py:801`.
- Disable Preview while the input is invalid; show inline error.

---

## 4. Tag-backup save dialog cancel is indistinguishable from organize cancel — and silently aborts
**Risk**: HIGH
**Button / flow**: Confirm "Yes, move files" → Tauri saveDialog → user clicks
Cancel on the OS dialog
**File**: `OrganizeView.tsx:118-126`

```ts
if (backupFirst) {
  const out = await saveDialog({...});
  if (typeof out !== "string") {
    // User cancelled the save dialog — abort the whole operation
    return;
  }
  ...
}
```

**What breaks**: When the user dismisses the save dialog (Esc, X, or Cancel),
the function returns with **no notification, no error, no operation-state
update, no log line**. The Confirm modal is already closed (line 113).
`useOperationStore.active` is still `null` because `begin()` hasn't been
called yet. From the user's perspective: "I clicked the big red Execute button
and... nothing happened."

A user who is mildly distracted will assume the click didn't register and
click again — see also finding #5 on double-confirm races.

This is precisely the "Modal closing without confirming" pattern flagged in
the prompt.

**Reproduction**:
1. Click Execute, leave "Back up tags first" checked, click "Yes, move files".
2. When the OS save dialog appears, hit Esc.
3. Observe: confirm modal is gone, no toast, no banner, no plan change. UI
   silently looks idle.

**Fix**: On dialog cancel, surface a `notify("Organize cancelled", { kind:
"info" })` and ideally re-open the confirm modal so the user can either pick
again or uncheck "Back up tags first". Don't punish the user with silence for
an action they took.

---

## 5. Double-click Execute fires two confirm modals → two parallel organize attempts
**Risk**: MED
**Button / flow**: Execute button → ConfirmModal
**File**: `OrganizeView.tsx:107-110` (`handleExecuteClick` just sets
`showConfirm=true`), `OrganizeView.tsx:208-216` (Execute button disabled only
on `active !== null`).

**What breaks**: Between clicking Execute and `begin("organize")` actually
running (which happens deep inside `performExecute`, *after* the
backup-tags step), `active` is still `null`. So:

1. User clicks Execute → confirm modal opens.
2. Tauri save dialog appears (if backup-first checked) — confirm modal is
   gone, but `active` is still null.
3. Sidecar's `_LONG_OP_LOCK` will catch a second org call on the Python side
   (`rpc.py:762`), returning `Another long-running operation already in
   progress`. So actual data loss is prevented.
4. *But*: backup_tags is **NOT** in `_CANCELLABLE_METHODS` keyed to
   `"organize"` — it's keyed to `"backup"`. So if the first call is in the
   backup phase, a second Execute click sneaks past `_LONG_OP_LOCK`'s
   single-op-at-a-time guard (different kinds), and you get two organize
   handlers attempting the same shutil.move plan in parallel. Half the
   destinations get collision-renamed.

Also: re-clicking the confirm button rapidly will fire `performExecute` twice
before `setShowConfirm(false)` re-renders — the confirm button has no
disabled state during in-flight work.

**Reproduction**: Hard to repro reliably without a slowdown injection, but
spam-clicking the confirm button on a slow machine is the lay-user version.

**Fix**:
- Make Execute disable while the confirm modal is open OR while
  `showConfirm` is set.
- In `ConfirmModal`, disable the confirm button after first click (track an
  internal `submitting` state, or accept a `loading` prop from the parent).
- Treat backup + organize as a single conceptual operation — block other
  long ops while either is running. Cleanest fix: add a separate
  "organize_with_backup" composite op kind so `_LONG_OP_LOCK` covers both.

---

## 6. `String(e)` swallows the structured RpcError — user sees "[object Object]" or unhelpful JSON
**Risk**: MED
**Button / flow**: Any RPC failure in OrganizeView
**File**: `OrganizeView.tsx:103, 132, 175` — all three error paths use
`fail(String(e))`.

**What breaks**: `useSidecar.ts:21-43` wraps every rejection in an `RpcError`
with structured fields (`code`, `data.traceback`, `cancelled`, `raw`). But
calling `String(rpcError)` produces `"RpcError: <message>"` — losing the
traceback that was put there specifically for diagnostics, and losing the
`cancelled` flag. (`useOperationStore.fail` does try to detect cancellation by
string-matching `"cancelled":true` inside the message, but that only works
because `RpcError.message` happens to be the parsed message string, not the
raw JSON. If `parsed.message` is undefined for any reason, the substring match
fails silently and a cancelled op shows up as a red error banner.)

Worse, if the catch fires from somewhere that throws a plain object instead of
an RpcError (e.g. a malformed payload before the RPC layer), `String(e)`
yields `"[object Object]"` and the user sees that as the error banner.

The prompt called out `String(e)` in catch blocks specifically. Three
instances in this file.

**Reproduction**: Force the sidecar to return an error with no `message`
field (e.g. by killing the sidecar mid-call). Frontend shows "Error: Error"
or similar instead of a useful diagnostic.

**Fix**: Replace `String(e)` with a helper that:
- Detects `e instanceof RpcError` and returns `e.message + (e.data?.traceback
  ? "\n\n" + e.data.traceback : "")` for the toast detail (gated behind a
  "Show details" toggle in the banner).
- Falls back to `e instanceof Error ? e.message : JSON.stringify(e)` for
  unknown shapes.

---

## 7. Plan persists across tab navigation and config edits — Execute uses fresh params, but the displayed plan is stale
**Risk**: MED
**Button / flow**: Preview → switch tabs → come back, OR change rules → Execute
**File**: `OrganizeView.tsx:54` (`plan` lives in `useOperationStore`, not local
state), and `OrganizeView.tsx:112-146` (`performExecute` calls `buildParams()`
fresh).

**What breaks**: The plan stored in `useOperationStore.organizePlan` survives
tab navigation. The rules inputs (`min_genre_size`, `use_subgenres`,
`target_root`) flow into `useConfigStore` and ARE persisted. So:

1. User clicks Preview with `min_genre_size=10`. Plan shows "150 moves".
2. User switches to Library tab, then back. Plan still shows "150 moves".
3. User edits `min_genre_size` to 50 (without re-clicking Preview). The
   "Execute (150 moves)" button still reads 150.
4. User clicks Execute. The Confirm modal still says "Move 150 files".
5. `performExecute` calls `buildParams()` which uses the LIVE config →
   sends `min_genre_size=50` to the sidecar.
6. Sidecar plans again with the new rules → moves 47 files. Toast says
   "Moved 47 of 47". User: "Wait, what happened to the other 103?"

**Reproduction**: As above. The display vs. action divergence is reproducible
100% of the time.

**Fix**:
- Invalidate `organizePlan` whenever `orgCfg` or `source` changes (`useEffect`
  with the params as deps → `setPlan(null)`).
- Or: capture the planning params alongside the plan, then re-run planning
  inside `performExecute` and bail if the diff is non-empty (with a "Plan
  changed; re-preview?" modal).

---

## 8. Pre-flight: no check that `ml_genre` exists in the analysis payload — silently routes everything to `Unknown/`
**Risk**: MED
**Button / flow**: Preview using analysis.json that lacks ML data (scan-only
output, or partial run)
**File**: `organizer.py:94-95`, `OrganizeView.tsx:90-91`

**What breaks**: `scan_only` (RPC) returns track records with `extension`,
`filename`, `size_mb`, but no `ml_analysis` field. If the user organizes the
in-memory library after a fast-scan instead of after a full analyze,
`ml.get("ml_genre")` returns None for every track → `sanitize_folder_name(None)`
returns `"Unknown"` (utils.py:49). Every single track gets moved to
`<base>/Unknown/`. Plan preview shows "150 moves into Unknown". Some users
will catch this in the preview. Many will not — Unknown looks plausible if
they're glancing — and the result panel just says "150 moves into 1
destination".

Same risk if the user picks a stale analysis.json from before they
re-analyzed with subgenres enabled.

**Reproduction**:
1. Open a folder, click "Just scan, don't analyze" (fast scan).
2. Go to Organize. Source = "Currently loaded library" (default).
3. Preview. The plan shows ~all tracks routing to `Unknown/` (or grouped in
   one folder because every track has the same fake genre).
4. Execute. 200 files now live in `Unknown/`.

**Fix**: In `OrganizeView`'s SourcePicker, detect that the in-memory library
has zero tracks with `ml_analysis` set and either (a) disable the in-memory
source button with explanatory copy ("Run Analyze first — fast scan doesn't
detect genres"), or (b) show a yellow warning panel above Preview.
Server-side: have `plan_organization` raise if 100% of tracks resolve to
"Unknown" — almost certainly a usage bug, not what the user wanted.

---

## 9. Folder-grouping in PlanPreview misroots when destination doesn't start with base_dir
**Risk**: MED
**Button / flow**: Preview after specifying a `target_root` that's a parent of
the analyzed library
**File**: `OrganizeView.tsx:412, 152-154` (also FolderGroup at 468)

**What breaks**: The folder grouping logic strips `base_dir` from each
destination to compute a relative path. But the relative-path strip uses raw
prefix matching:

```ts
folder.startsWith(capturedPlan.base_dir)
  ? folder.slice(capturedPlan.base_dir.length).replace(/^[/\\]+/, "") || "(root)"
  : folder;
```

This is wrong if:
- `base_dir` has a trailing slash and the destination doesn't (or vice versa).
- Path casing differs on Windows (`C:\Music` vs `C:/music`) — Windows is
  case-insensitive but `startsWith` is case-sensitive.
- Forward-slash vs backslash mismatch from `Path` stringification on Windows
  (sidecar returns `\` on Windows; UI may compare against either).

When the strip fails, the folder breakdown in the result panel shows the FULL
absolute path for some entries and just `House/` for others. Looks broken.
Doesn't cause data loss but undermines trust in the result screen.

**Reproduction**: Set `target_root` to e.g. `C:\Music` (no trailing slash);
analyze tracks from `c:\music\unsorted\...` (lowercase drive). The
`startsWith` check returns false for some entries, and the result panel
shows mixed absolute / relative folders.

**Fix**: Normalize both `base_dir` and destinations on the Python side
before serializing — use `os.path.relpath` and ship `relative_destination`
in the planned-move payload. The UI shouldn't be doing path arithmetic.

---

## 10. `min_genre_size` accepts arbitrarily large values — silently turns the whole library into Other/
**Risk**: LOW
**Button / flow**: Min-genre-size input → Preview
**File**: `OrganizeView.tsx:243-251`

**What breaks**: Input is `type="number" min={1}` (no max). User types
`1000000`. The plan computes `small_genres` as every genre (none has 1M
tracks), so every track ends up in `Other/<Genre>/`. Plan preview shows
hundreds of moves, all under `Other/`. User executes thinking the rule means
something else.

This is a UX trap, not a crash. Low risk because the preview does honestly
show the destinations — but the input itself gives no hint that 1000 is
already absurd for almost any real library.

**Reproduction**: Type a 6-digit number. Hit Preview. Every move goes to
`Other/`.

**Fix**: Cap input at 99 (max value sensible for ~10k-track libraries) OR
show a yellow inline warning when `min_genre_size > 100` AND show a
"Small genres count" stat in the plan preview header so the user can sanity-
check before clicking Execute.

---

## 11. Confirm modal closes on backdrop click — easy to dismiss "Yes, move files" by mistake
**Risk**: LOW
**Button / flow**: Execute → Confirm modal → click outside
**File**: `ConfirmModal.tsx:51` — backdrop has `onClick={onCancel}`.

**What breaks**: A `danger` variant modal warning the user about a
file-system-mutating operation closes when the user clicks anywhere outside
the panel. Easy to bump while reading the preview. Once dismissed, the plan
is still there and the user has to find the Execute button again — annoying
but not destructive. The bigger concern is the opposite case (clicking
backdrop while half-reading the modal feels like an OK-by-mistake), but
backdrop = cancel is actually safe. Risk is irritation, not data loss.

Also: no Escape key handler. Escape is conventional for cancel and is the
keyboard equivalent of a backdrop click. Inconsistent.

**Reproduction**: Open the confirm modal. Click outside. Modal vanishes.

**Fix**:
- For `variant="danger"`, disable backdrop-to-cancel — require explicit
  Cancel button click. (Pattern from macOS / iOS critical alerts.)
- Add an Escape key listener that calls `onCancel`. Conventional and
  expected.

---

## Cross-cutting concerns (not separate findings, but flagged)

- **File locking on Windows**: `shutil.move` against a file open in another
  app (Rekordbox, VLC, file Explorer preview pane) raises
  `PermissionError: [WinError 32]`. Caught by `except OSError`
  (organizer.py:171) and appended to `stats.errors`. The user sees a
  yellow "Mostly done" with WinError 32 strings — accurate but unhelpful.
  Consider mapping known WinError codes to plain-English messages
  ("File is open in another app — close it and retry").

- **Cross-filesystem moves**: `shutil.move` does copy+delete across
  filesystems. Combined with finding #1 (no cancellation), an organize that
  spans drives can run for tens of minutes with no abort path.

- **No "Plan only" / "Execute" distinction in the UI copy**: the prompt
  asked. The actual flow is Preview → Confirm → Execute, with Preview
  being a dry pure-Python plan (no `dry_run=True` sidecar call —
  `_plan_organization` is its own RPC). So technically there's no "Plan
  only" execute path. This is fine, but: the user CAN'T preview what
  errors organize will hit (file locks, perms) without actually running
  it. Consider a Preflight step (touch one tempfile in each destination
  folder) before the real move loop.
