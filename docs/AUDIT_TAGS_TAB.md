# Vibechek Tags-tab landmine audit

Per-button audit of the Tags tab and its supporting RPC handlers, focused on
flows users hit in production. Sorted by risk, highest first.

Scope:
- `ui/src/components/TagsView.tsx` (backup, restore, remap restore, history)
- `ui/src/hooks/useApplyTags.ts` (ML tag apply from Library/Track Details)
- `vibechek/rpc.py` handlers `_backup_tags`, `_restore_tags`,
  `_restore_tags_with_remap`, `_apply_ml_tags`, `_backup_history`,
  `_forget_backup`
- `vibechek/tagger.py` core read/write/backup/restore/apply logic

Out of scope: install / preflight / analyze flows (covered in
`AUDIT_LANDMINES.md`).

---

## 1. Backup write is non-atomic — disk-full or mid-write crash leaves a truncated, unloadable JSON
**Risk**: HIGH
**Button / flow**: "Create backup"
**File**: `vibechek/tagger.py:329-332`
**What breaks**: `backup_tags` builds the entire dict in memory then calls
`Path(output_path).write_text(json.dumps(...), encoding="utf-8")`. There is
no temp-file + atomic rename, no `fsync`, no free-space pre-check. If the
target disk fills up mid-write (a 12 k-track Rekordbox library with GEOB
frames can be 2–6 GB), the file is left half-written. The user sees a
generic `OSError: [Errno 28] No space left on device` toast, the
`backup_history.record(...)` in `_backup_tags` (`rpc.py:508`) still runs in
its try/except, so the truncated file is **registered as a valid backup in
history** — until they try to restore from it and `json.loads` blows up.
The user has now also wasted the only safety net they were trying to create
before running Apply / Organize.
**Reproduction**:
1. Fill a USB drive to within ~100 MB of full.
2. Pick a 10 k+ track library.
3. Save backup to the USB drive.
4. Watch progress hit 100%, then get an `OSError` toast.
5. Open Tags tab — the broken backup is in history with its file size column.
6. Click Restore → cryptic `Expecting value: line N column M` JSON error.
**Fix**: Write to `<output>.partial`, `fsync`, then `os.replace` to final
path. Only record in history after the rename succeeds. Optionally
`shutil.disk_usage(output.parent).free` vs a rough estimate (sum of source
file sizes / 50, since base64-GEOB ratio is roughly that) before starting.

---

## 2. Restore-from-corrupt-backup surfaces raw Python JSON parser error instead of "this file isn't a Vibechek backup"
**Risk**: HIGH
**Button / flow**: "Choose a backup file" + "Restore" (both single restore
and history-row restore), and "Restore (auto-detect moved files)"
**File**: `vibechek/tagger.py:346, 563`; surfaced via
`ui/src/components/TagsView.tsx:184, 222`
**What breaks**: `restore_tags` and `restore_tags_with_remap` both start
with `json.loads(Path(backup_path).read_text(encoding="utf-8"))` with no
guard. Three real-world failure modes all surface as inscrutable errors:
- **Truncated / corrupt JSON** → `json.JSONDecodeError` → caught by
  `_dispatch` (`rpc.py:779`) in the `(TypeError, KeyError, ValueError)`
  branch → returned as `INVALID_PARAMS` with message
  `Invalid params: Expecting value: line 1234 column 5 (char 78901)`.
- **Wrong file format** (user picks any other JSON, e.g. a project tracker
  export) → `data["files"]` raises `KeyError: 'files'` → same handler →
  `Invalid params: 'files'`.
- **Version mismatch** (`BACKUP_VERSION` is hardcoded `"1.0"` at
  `tagger.py:56`; never read back) — no check at all; format drift in the
  future will silently produce wrong-shape data.

The UI then calls `fail(String(e))` in
`TagsView.tsx:184,222`, which surfaces `Error: Invalid params: 'files'`
as a red panel with no actionable guidance. The user is told their backup
file is broken with no hint that maybe they picked the wrong file.
**Reproduction**:
1. Click "Choose a backup file" → pick any non-Vibechek `.json`
   (e.g. `package.json`).
2. Confirm restore.
3. Red error panel: `Invalid params: 'files'`.
**Fix**: In `_restore_tags` / `_restore_tags_with_remap`, wrap the
`json.loads` and `data["files"]` access; raise a friendly
`ValueError("This file isn't a Vibechek tag backup. Pick a .json file
created by Tags → Create backup.")`. Bonus: verify
`data.get("version") == BACKUP_VERSION` and surface a version-mismatch
message.

---

## 3. Forget-backup deletes history entry with zero confirmation on a single misclick
**Risk**: HIGH
**Button / flow**: The little "x" on each backup history row
**File**: `ui/src/components/TagsView.tsx:155-161, 629-635`
**What breaks**: `handleForgetBackup` calls the `forget_backup` RPC with
no confirm dialog. The button is a 14 px `X` icon adjacent to the
`Restore` button; mis-targeting is easy on a high-DPI display. Tooltip
says "Remove from history (file itself is not deleted)" but the user has
to hover for ~1 s to see it. The catch block is `try { ... } finally { await refreshHistory(); }`
— **no error handling at all**, so a sidecar failure during forget is
invisible (the row vanishes locally on refresh whether or not the sidecar
actually forgot it; next sidecar restart will revive the record). The user
also has no UNDO and the backup file may live on a thumb drive they no
longer have plugged in, meaning "forget" is irreversible in practice (they
can't re-record the history entry without re-running backup).
**Reproduction**:
1. Tags tab → mouse near Restore on any history row.
2. Click the X 4 px to the right by accident.
3. Row disappears. No confirmation, no toast, no undo.
**Fix**: Add a small ConfirmModal: "Remove this backup from history? The
backup file at `<path>` is not deleted. You can re-add it by clicking
Create backup again, but you'll lose this entry from your list." Also
catch errors and surface them, not just `finally`-swallow.

---

## 4. `id3_text_encoding` (new in beta.9) has no UI control — feature is invisible
**Risk**: HIGH
**Button / flow**: N/A — missing UI surface
**File**: `ui/src/stores/index.ts:175-177` (default), `ui/src/components/Settings.tsx`
(no picker)
**What breaks**: The whole point of `id3_text_encoding` is for users on
old Rekordbox (5.x) to switch to UTF-16 so their genre/subgenre changes
actually appear in the app. The default is `3` (UTF-8). The store comment
at `index.ts:175-176` literally promises "Settings exposes a picker for
users on legacy software (Rekordbox 5 only reads encoding=1 UTF-16)" —
`grep -n "encoding\|UTF-8\|UTF-16\|ISO-8859\|Rekordbox 5" ui/src/components/Settings.tsx`
returns zero matches. The value can only be changed by hand-editing
`<config_dir>/config.json`, which is documented nowhere. Every Rekordbox
5 user who runs Apply ML tags will see no tag changes in Rekordbox and
will have no idea why.
**Reproduction**:
1. Open Settings → Tagging.
2. Look for ID3 encoding picker. There isn't one.
3. Run Apply ML tags on MP3 library, open in Rekordbox 5 — genre frames
   are written as UTF-8 and silently ignored by RB5.
**Fix**: Add a `<Select>` in Settings → Tagging with three options
(UTF-8 / UTF-16 / ISO-8859-1), defaulting to UTF-8, with an info tooltip
explaining when to switch. Pass `id3_text_encoding` through
`useApplyTags.apply` and the `apply_ml_tags` RPC (currently dropped on
the floor — `useApplyTags.ts:53-58` only sends `confidence`,
`skip_bpm_and_key`, `preserve_rekordbox_frames`).

Side effect: the `apply_ml_tags` RPC handler in `rpc.py:485-489` constructs
its own `TaggingConfig` from the params and **never honours the user's
stored `id3_text_encoding`** even if they edit the JSON manually, because
the param is not forwarded. So even the workaround doesn't work today.

---

## 5. After successful restore / remap restore, in-memory library state is not refreshed
**Risk**: HIGH
**Button / flow**: "Yes, restore" (ConfirmModal), "Run restore" (remap dialog)
**File**: `ui/src/components/TagsView.tsx:163-186, 211-224`
**What breaks**: `handleConfirmRestore` and `handleRunRemapRestore` both
call `notify()` / set local result state on success, but **never touch
`useLibraryStore`**. If the user has a library currently open in the
Library tab and they restore tags onto it, the Library tab continues to
show the pre-restore genre / BPM / energy values until they manually
re-open the folder. Worse: they then run Apply ML tags from the stale
Library view, the Apply dialog says "we'll write genre X to 142 tracks"
based on the pre-restore *analysis* state, and the apply writes the
analysis tags even though they don't match the freshly-restored on-disk
state any more — silently nullifying the restore for the analyzed tracks.
**Reproduction**:
1. Open a library, run analyze, switch to Library tab. Note current
   displayed genres.
2. Switch to Tags, restore a backup that has different genres.
3. Switch back to Library — old genres still shown.
4. Click Apply → confirmation shows old genres → click confirm → those old
   genres get re-written, undoing the restore.
**Fix**: After a successful restore, either
(a) call `useLibraryStore.setTracks([])` and surface a toast telling the
user to reopen the folder, or
(b) re-run `scan_only` against the library_path and `setTracks(result)`,
or
(c) at minimum clear `selectedIds` and show a `library is stale, reload?`
banner.

---

## 6. Remap restore: ambiguous-filename cases are silently bucketed into "Skipped" with no per-file detail
**Risk**: MED
**Button / flow**: "Run restore" in remap dialog → result panel
**File**: `ui/src/components/TagsView.tsx:528-561`; tagger logic at
`vibechek/tagger.py:622-643`
**What breaks**: When a backup entry's filename matches >1 file in the
new library root, the backend marks it `"strategy": "ambiguous"` and
increments either `skipped_size_mismatch` (sized-multi-match) or
`skipped_missing` (filename-only multi-match) depending on the path
through. The UI panel sums both into a single `Skipped: N` bullet with
no breakdown. The whole `matches: Array<...>` payload — which contains
the per-file `strategy` including `"ambiguous"`, `"backup_error"`,
`"write_error"` — is returned over the wire and **completely unused** by
the React component. The user sees "47 of 12,000 restored, 11,953 Skipped"
with no way to know that 8 of those skips were genuine ambiguities they
could resolve by pointing at a more specific subdirectory.
**Reproduction**:
1. Library has `Music/A/song.mp3` and `Music/B/song.mp3`.
2. Backup was taken when only `A/song.mp3` existed.
3. Remap restore against `Music/` → ambiguous, skip.
4. Result panel says "Skipped: 1" with no indication it's ambiguous.
**Fix**: Render `result.matches.filter(m => m.strategy === "ambiguous")`
as a collapsible "Skipped because multiple candidates matched" list with
the original path and both candidates, so the user can act on it (rerun
against `Music/A/` to disambiguate).

---

## 7. Apply ML tags during in-flight Backup shows a confusing busy error instead of a disabled button
**Risk**: MED
**Button / flow**: "Apply tags" (Library / Track Details) while a Backup
is running from the Tags tab
**File**: `ui/src/hooks/useApplyTags.ts:41-72`; sidecar gate at
`vibechek/rpc.py:756-772`
**What breaks**: The sidecar correctly serializes long ops via
`_LONG_OP_LOCK` + `cancellation.current_kind()` and returns
`INVALID_REQUEST` with `data: {busy: true, running: "backup"}`. But
`useApplyTags` only checks `active === "tag"` for `isApplying` — the
Library/TrackDetails Apply buttons don't disable when `active === "backup"`.
A user backing up a 10 k library (5 + minutes) can click Apply, the local
`begin("tag")` flips `active` to `"tag"` immediately, the RPC fires, the
sidecar rejects, `fail(String(e))` shows
`Error: Another long-running operation ('backup') is already in progress.
Cancel it before starting 'tag'.` as a red toast — **and the backup
operation in the Tags tab now shows `active="tag"` locally**, hiding the
backup progress UI. The user thinks Apply broke the backup. The backup
keeps running in the sidecar.
**Reproduction**:
1. Start a long backup in Tags tab.
2. Switch to Library, click Apply tags.
3. Backup progress disappears. Red error appears. Backup keeps running
   silently. State is a mess until the backup finishes and clears itself.
**Fix**: Either (a) expose `isApplying` as `active !== null` from
`useApplyTags` and disable the Apply button whenever any op is active, or
(b) gate `begin("tag")` behind a check that `active === null`, or
(c) make `useOperationStore.begin` reject if `active !== null` already.
Option (a) is cheapest and matches what TagsView already does.

---

## 8. Remap dialog backdrop click discards the result panel
**Risk**: MED
**Button / flow**: Remap dialog outer area (backdrop)
**File**: `ui/src/components/TagsView.tsx:441-445, 226-230`
**What breaks**: The outer `<div ... onClick={onClose}>` calls
`handleCloseRemapDialog`, which only blocks closing while
`active !== null`. The moment the restore completes (`active` flips back
to `null`), the result panel becomes visible — and any click outside the
inner card immediately closes the whole dialog, including the only place
that shows what happened. The user moves their mouse to read the result,
clicks anywhere near the bullets but in the dark area, dialog vanishes,
result state is set on the parent (`setRemapResult`) but unrendered (the
dialog `remapDialogOpen` is the only thing that renders it). They have
no way to reopen and see what happened. They re-run the restore — which
re-writes every tag a second time on the 47 files that already succeeded.
**Reproduction**:
1. Open remap dialog → run restore.
2. Result panel appears.
3. Click anywhere in the dimmed area to dismiss.
4. Open dialog again → result is gone (`handleOpenRemapDialog` resets it
   at line 192: `setRemapResult(null)`).
**Fix**: Don't close on backdrop click — require explicit Close button.
Or persist `remapResult` outside the dialog and render a result summary
in the Tags view itself after close.

---

## 9. `fail(String(e))` across all four buttons stringifies RpcError into noisy JSON-like blobs
**Risk**: MED
**Button / flow**: Every button in TagsView (`handleBackup`,
`handleConfirmRestore`, `handleRunRemapRestore`) and `useApplyTags.apply`
**File**: `ui/src/components/TagsView.tsx:135, 184, 222`;
`ui/src/hooks/useApplyTags.ts:67`
**What breaks**: `RpcError extends Error` (see `useSidecar.ts:21-43`)
and sets a real `message`. But every catch site calls `String(e)`, which
on a custom Error subclass produces `Error: <message>` prefixed — and
worse, for the non-Error case (e.g. if `rpc()` itself throws a Tauri
invoke promise rejection that's a `Record<string, unknown>`), `String(e)`
returns `[object Object]`. The user sees either useless prefixed messages
or literal `"[object Object]"` as the red error banner. The
`useOperationStore.fail` consumer also re-stringifies (line 108) and
does substring matching for cancellation detection — which works only
because of the duplicated `String(error)` happening to round-trip JSON.
**Reproduction**:
1. Kill the sidecar mid-backup (Task Manager → end the python.exe).
2. UI shows `Error: ` followed by a stack-trace-shaped string with a
   `traceback` JSON blob.
**Fix**: Pass `e` to `fail` directly — `fail` already calls `String(e)`
internally, and an `RpcError` argument lets it inspect `.cancelled`
typed-safely (the current branch on line 113 only works for the typed
case). Remove all `String(e)` wrappers in catch blocks.

---

## 10. Single-file picked as remap library root → silent "no matches" failure with misleading copy
**Risk**: MED
**Button / flow**: Remap dialog → "Choose" next to "New library root"
**File**: `ui/src/components/TagsView.tsx:206-209`;
`vibechek/utils.py:22-43`
**What breaks**: `openDialog({ directory: true })` on Windows is a folder
picker so the typical case is fine, but the input field at line 489-494
is freely editable — the user can type or paste a path to a single MP3.
On the backend, `find_audio_files(library_root)` does
`root.rglob("*")`, which on a non-directory Path returns an empty
iterator on Windows (and raises `NotADirectoryError` on Linux/macOS
mid-iteration). Result on Windows: 0 matches, every backup entry buckets
into "missing", and the error banner reads
"**0 of 12,000 restored — no files matched, try a different library
root**" with no hint that they pointed at a file. On Linux/macOS the
`NotADirectoryError` bubbles up as `Invalid params: ...` per finding #2.
**Reproduction**:
1. Open remap dialog.
2. Paste a path to any single MP3 file into "New library root".
3. Click "Run restore".
4. (Windows) Get "no files matched" panel with no hint. (Linux) Get a
   raw stack trace.
**Fix**: In `restore_tags_with_remap`, validate `library_root.is_dir()`
first and raise `ValueError("Library root must be a folder, not a single
file: <path>")`. In TagsView, also check `is_dir()` client-side via a
Tauri fs probe before enabling Run restore.

---

## 11. Apply ML tags: GEOB/PRIV preservation has no post-write verification
**Risk**: MED
**Button / flow**: "Apply tags" (anywhere it's called)
**File**: `vibechek/tagger.py:438-484`
**What breaks**: `_apply_mp3` snapshots `GEOB:*` / `PRIV:*` frames into a
list before the write, then re-adds them after `audio.save()`. There is
no read-back verification step — if `mutagen.MP3.save()` corrupts the
ID3 v2 frame ordering (rare but documented for files with malformed
TENC/TXXX combinations) or drops unknown sub-frames, the preserved data
is gone and the user has no way to know until they open Rekordbox and
discover their cue points are missing. The product surface promises this
preservation as the headline safety guarantee ("includes Rekordbox cue
points and beat grids" — `TagsView.tsx:268, 384`); the implementation
trusts mutagen blindly. Also: if `apply_genre=False` AND
`skip_bpm_and_key=True` AND no ML custom tags exist, the function still
calls `audio.save()` unconditionally (line 484), rewriting the file for
no reason — small file mtime churn but a real audit-trail concern for DJs
who track file modification dates.
**Reproduction** (hard to reproduce on demand — but the failure mode is
silent and irreversible without backup):
1. Apply ML tags on a 10 k library.
2. Open in Rekordbox. Spot-check 50 random tracks for cue point loss.
3. No way to know which (if any) lost frames until then.
**Fix**: After `audio.save()`, re-read the file and assert that
`{k for k in audio.tags if k.startswith("GEOB:") or k.startswith("PRIV:")}`
contains every key from the `preserved` snapshot. If not, append to
`stats.errors` with the specific filename so the user gets a list of
files to manually restore from backup. Also skip `audio.save()` entirely
when no actual writes happened.

---

## 12. Long backup / restore on 10 k+ libraries shows progress, but a hung Tauri stdout reader will freeze the UI silently
**Risk**: MED
**Button / flow**: "Create backup" and both restore flows
**File**: `vibechek/rpc.py:107-126`; `vibechek/tagger.py:321, 350, 589`
**What breaks**: The `_emit_progress` throttle (20/sec) prevents the
sidecar from flooding stdout, which is good. But on Windows the Tauri
shell's stdout reader has been observed (audit #15 in the parent doc) to
fall behind under heavy CPU contention. When that happens, the OS pipe
buffer fills, `_StdoutWriter.write` blocks the worker thread, and the
backup *appears* hung — but unlike analyze, there's no progress-stalled
banner. The TagsView UI relies on `active !== null` to disable buttons;
once active is set, the user sees "Create backup" disabled and no
progress feedback at all unless `setProgress` happens, which it won't
during a stdout stall. There's also no client-side timeout — the user
will sit looking at a spinner indefinitely (or close to it; the GUI
operation-progress UI is not in TagsView at all — TagsView shows no
spinner, just disables the button). For a 5-minute backup on a large
library, the user has no idea anything is happening.
**Reproduction**:
1. Pick a 10 k+ track library on a slow USB 2.0 drive.
2. Click Create backup. Pick destination.
3. Stare at a disabled button with no progress bar / spinner / file
   counter for ~5 minutes.
**Fix**: Add a progress bar to TagsView using the same
`useSidecarProgress` hook the analyze/dedupe views use. At minimum show
a spinner + current filename so the user knows it's alive. Bonus: detect
stalls (no progress for >30 s) and surface a "still working…" hint.

---

## Summary

12 findings — 5 HIGH, 7 MED, 0 LOW.

**Top 3 by user impact**:
1. **#1 Non-atomic backup write** — disk-full mid-write silently produces
   a corrupt backup that's recorded in history as if valid. Defeats the
   entire "safety net before any tagging op" value proposition.
2. **#2 Raw JSON parser errors on bad restore input** — three distinct
   bad-input cases (corrupt, wrong-format, version-mismatch) all surface
   as unintelligible Python error strings. Hostile to non-technical users.
3. **#4 `id3_text_encoding` invisibility** — the new beta.9 feature has
   no UI, no config-file documentation, and isn't even forwarded by the
   Apply RPC even if a user manually edits config.json. Every Rekordbox 5
   user who runs Apply will see no genre changes and have no diagnostic
   path.
