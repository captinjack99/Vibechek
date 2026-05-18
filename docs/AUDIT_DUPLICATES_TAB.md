# Duplicates tab — button-flow audit

Read-only audit performed against `ui/src/components/DuplicatesView.tsx`,
`ui/src/lib/keeperRules.ts`, `ui/src/components/ConfirmModal.tsx`,
`vibechek/rpc.py` (`_find_duplicates`, `_handle_duplicates`),
`vibechek/duplicates.py`, `vibechek/cancellation.py`, and the operation /
notification stores.

Findings are sorted by risk (HIGH → LOW). Total: 11 issues
(4 HIGH, 5 MED, 2 LOW).

---

## 1. "Cancel" during dedupe is a no-op — the long op runs to completion

**Risk**: HIGH
**Button / flow**: AnalysisProgress "Cancel" button, while a Duplicates scan is running
**File**: vibechek/duplicates.py:222-301 (no `cancellation.check()` calls anywhere); vibechek/rpc.py:713 (registered as cancellable)
**What breaks**: `find_duplicates` is listed in `_CANCELLABLE_METHODS` and `begin("dedupe")`
is called when the RPC starts, but the hashing loop (`for i, fp in enumerate(audio_files)`)
and the fingerprint loop never call `cancellation.check()`. Clicking the floating Cancel
button calls `rpc("cancel_operation")` which sets the flag — but no code reads it. The
sidecar continues hashing every file (MD5 + Chromaprint subprocess per file). On a 12k-
track library that's 10–30+ minutes of unkillable work. Worse, because the long-op lock
is held the whole time, every other long operation (analyze, organize, tag, backup) gets
rejected with `"Another long-running operation ('dedupe') is already in progress."` until
the original dedupe finishes naturally. `handle_duplicates` (move/trash phase) has the
same problem.
**Reproduction**: Pick a folder with >2k audio files → Scan → wait 5s → click Cancel
on the bottom-center progress card. Op continues to run; the progress card disappears
visually (because `useOperationStore.fail` matches the cancel string and clears `active`),
but the sidecar is still busy. Try to click Analyze on the Library tab — get the "already
in progress" error.
**Fix**: Sprinkle `cancellation.check()` inside the hashing loop (every N iterations
or in `report_progress`) and inside the move/trash loops. Also: kill the fpcalc
subprocess on cancel, or accept up to one `audio_fingerprint` call's worth of latency.

---

## 2. The keeper-rule eval runs on the React render thread; 10k-file scans freeze the UI

**Risk**: HIGH
**Button / flow**: After Scan completes — the report view re-computes auto-keepers and
the per-group "picked by X" hint synchronously inside `useMemo`
**File**: ui/src/components/DuplicatesView.tsx:285-292 (autoKeepers useMemo) and 640
(`explainPick` per GroupCard render)
**What breaks**: `autoKeepers` iterates every group and runs `pickKeeper(files, rules)`,
which sorts files using a 5-rule comparator. Then GroupCard runs `explainPick` for
every group — another N-rule iteration with `rest.find(...)` per rule. With 10k duplicate
groups, that's O(10k × 5 × log(group_size)) just to render once, all on the main
thread. Every time the user toggles a rule or reorders, the whole pile recomputes.
React's render is interruptible but `useMemo` is not — the browser tab freezes for
seconds. `applyChoices` runs the rules a SECOND time when the user clicks Move/Trash
(line 750), doubling the work right before showing the confirm modal.
**Reproduction**: Hard to reproduce without a 10k-dupe library, but synthesizable:
populate `report` in the store with a fixture of 10000 fake groups, click a rule
checkbox → tab freezes for ~3-8s on a modern laptop. Even at 1000 groups, dragging
a rule up/down feels visibly laggy.
**Fix**: Run rule eval in a Web Worker, or cache `autoKeepers[g.key]` keyed by
`(g.key, JSON.stringify(rules))` and invalidate lazily. At minimum, virtualize the
GroupsList (only render visible groups) — currently every GroupCard renders even
if offscreen.

---

## 3. `chromaprint_similarity_threshold` is dead code — the "similarity" is exact-hash equality

**Risk**: HIGH
**Button / flow**: N/A — silent misfeature
**File**: vibechek/duplicates.py:140-158, 263-298; vibechek/config.py:79;
vibechek/rpc.py:372
**What breaks**: The config field `chromaprint_similarity_threshold: float = 0.95`
exists; `_find_duplicates` reads `params.get("threshold", 0.95)` and stuffs it into
`DuplicateConfig`; the value is never read anywhere in `duplicates.py`.
`audio_fingerprint` md5-hashes the raw fingerprint string (`hashlib.md5(fp.encode())`)
and the matcher (line 273-282) just buckets by exact md5 equality. Two slightly
different masters → completely different md5s → reported as NOT duplicates. The
prompt mentioned a "similarity threshold slider" — there is no such slider in the
UI, and even if there were it would do nothing. The product copy in
`DuplicatesView.tsx:243` ("tracks that sound the same but were re-encoded") is
misleading: it really only catches re-encodes that produce *bitwise-identical
chromaprint fingerprints*, which happens for some pure transcodes but fails for
volume-normalized / re-mastered copies that humans would call "the same song."
**Reproduction**: Take a FLAC file, re-encode to 320 kbps MP3, and to a
volume-normalized MP3. Scan. Only the bitwise-identical fingerprint matches will
group; the volume-normalized one shows as a separate file. Users will assume the
tool missed real duplicates.
**Fix**: Either (a) implement real Hamming-distance similarity using
`chromaprint`/`acoustid` raw fingerprints with the threshold, or (b) delete the
dead config field and remove the misleading "tracks that sound the same" copy.

---

## 4. `applyChoices` can promote the keeper into the trash list if an override path is stale

**Risk**: HIGH
**Button / flow**: Move to review folder / Send to trash — Stage 2 confirm
**File**: ui/src/components/DuplicatesView.tsx:744-761 (`applyChoices` rebuild)
**What breaks**: `keeperPath = keeperOverrides[g.key] ?? pickKeeper(...).path` —
if `keeperOverrides[g.key]` is a path that doesn't exist in the current
`[g.keep, ...g.duplicates]`, the fallback `keeper = allFiles.find(...) ?? g.keep`
returns `g.keep`, but the subsequent `dupes = allFiles.filter(f => f.path !== keeperPath)`
keeps EVERY file (none match the stale path) — including the original keeper. The
keeper ends up in the duplicates array and will be trashed/moved. The useEffect at
DuplicatesView.tsx:69-72 clears `keeperOverrides` on report change, which guards the
common case, BUT: if a user clicks a file in a group (sets override), then a
background tool renames that file on disk, then the user clicks Move/Trash without
rescanning, the override path no longer matches anything. The keeper survives in
`keep` but is also listed in `duplicates`. `handle_duplicates` will then trash the
keeper. The summary will report "moved/deleted N+1" without flagging the keeper-loss.
**Reproduction**: Hard to hit organically. Easier path: edit `keeperOverrides`
state in DevTools to a fake path string, click Send to trash — confirm modal
shows N+1 files (one extra), proceed, keeper goes to trash.
**Fix**: In `applyChoices`, if the override path isn't in `allFiles`, either fall
back to the rule-picked keeper (don't pretend the override is valid) or surface a
"this group changed since you picked a keeper — rescan" error and refuse to act.

---

## 5. Post-action rescan leaves stale data visible and is unawaited

**Risk**: MED
**Button / flow**: After "Send to trash" / "Move to review folder" succeeds
**File**: ui/src/components/DuplicatesView.tsx:132 (`handleScan();` — no await,
no `setReport(null)` first)
**What breaks**: `performResolve` calls `handleScan()` (fire-and-forget) after
the toast. During the rescan, the old report stays visible — listing files that
were just trashed, with old "X MB recoverable" totals. If the user clicks
Move/Trash again during this window, the second call uses the stale `report` and
tries to act on already-trashed files. They'll hit the per-file
`if not src.exists(): summary["errors"] += 1` path in
vibechek/duplicates.py:338, so the user gets "Trashed 0 duplicates · N errors —
see report." with no actual "report" to see. Also: the unawaited promise means a
scan failure during the auto-rescan still surfaces (handleScan has its own
try/catch + fail), but `performResolve` looks finished while it's actually still
running. Two button states ("active" from handleScan begin, summary toast shown)
race.
**Reproduction**: Big library with many dupes → Send to trash → during the 30+s
rescan, click Send to trash again → see "0 trashed, N errors" with no detail.
**Fix**: `setReport(null)` immediately after the toast (or show a "rescanning…"
skeleton), and `await handleScan()`. Better: have `handle_duplicates` return the
new report alongside the summary so we don't pay for a second full scan at all.

---

## 6. The "errors — see report" toast points to a report that doesn't exist

**Risk**: MED
**Button / flow**: Any Move/Trash action that has per-file failures
**File**: ui/src/components/DuplicatesView.tsx:127-131; vibechek/duplicates.py:339, 347, 363, 369
**What breaks**: The notify call says `${errors} error${...} — see report.` There
is no error report shown to the user anywhere. `handle_duplicates` returns
`{moved, deleted, errors}` — just an integer count, no list of paths, no error
messages. The per-file failure reasons are `log.warning("Move failed for %s: %s")`
which goes to stderr / log file only. Users with read-only files, permission
errors, locked-by-another-app files, or full review folders get a misleading
"some errors happened, look elsewhere (where?)" toast.
**Reproduction**: Make the review folder read-only or pre-create a file with
the same name + a system lock → run Move. Toast says "Moved N · M errors — see
report." User has nowhere to see the report.
**Fix**: Either (a) return the per-file error list from `handle_duplicates` and
surface it in a results panel, or (b) drop the "see report" text and link to the
log file path (already returned by `get_log_tail`).

---

## 7. Review-folder picker bug — `dupCfg.review_folder` from Settings is trusted blindly

**Risk**: MED
**Button / flow**: "Move to review folder"
**File**: ui/src/components/DuplicatesView.tsx:99-105
**What breaks**: `if (action === "move" && !reviewFolder)` prompts for a folder
ONLY if the config value is falsy. If the user typed garbage into the Settings
"Review folder" text input (Settings.tsx:378-384 is a free-text input with no
validation), `reviewFolder` is truthy → no picker → confirm modal shows the bad
path in a code block → click Confirm → `handle_duplicates` calls
`dest_root.mkdir(parents=True, exist_ok=True)` (vibechek/duplicates.py:333) which
either silently creates a folder named whatever the user typed (e.g.
`Z:\nope\nope` becomes a real path under cwd on some Windows configs) or raises
`OSError` and the whole RPC fails with `String(e)` (a Python repr).
**Reproduction**: Settings → Advanced → set Review folder to `<<<invalid>>>` →
Duplicates → Move to review folder → confirm. Either get a real folder with that
name created under sidecar cwd, or get a JSON-stringified Python OSError as a
toast.
**Fix**: Validate `review_folder` before showing the confirm modal —
existsSync via Tauri filesystem API or just `Path.is_dir()` server-side with a
typed error. If invalid, force the picker open even when the config has a value.

---

## 8. Catch blocks use `String(e)` and bypass the typed RpcError

**Risk**: MED
**Button / flow**: Scan, Move, Trash — both catch sites
**File**: ui/src/components/DuplicatesView.tsx:91, 134
**What breaks**: `catch (e) { fail(String(e)); }` stringifies the error before
passing to the store. `useOperationStore.fail` is *designed* to receive the raw
error so it can branch on `(error as any).cancelled === true` from RpcError
(stores/index.ts:113). By pre-stringifying, the typed-flag check is lost —
cancellation detection then relies on substring-matching `"cancelled by user"` in
the error message, which works *today* because vibechek/cancellation.py:67
formats the message that way, but a future i18n/wording change would silently
break cancel-detection and turn cancels into red "Operation failed" banners.
Also: non-RpcError exceptions (e.g. a thrown string from a future bug) lose their
stack via String().
**Reproduction**: Change the cancellation message in cancellation.py:67 to
anything not containing "cancelled by user" → the substring check fails → cancel
appears as an error toast.
**Fix**: `catch (e) { fail(e); }` — let the store do its typed check.

---

## 9. Rapid-click Scan race: the disabled check guards against most but not all double-clicks

**Risk**: MED
**Button / flow**: Scan button
**File**: ui/src/components/DuplicatesView.tsx:152-159 + 79-93
**What breaks**: The button is `disabled={!scanPath || active !== null}` which
relies on React having re-rendered after `begin("dedupe")`. `begin` is
synchronous, so in practice the disabled prop flips before the next paint and
the second click is dropped — for normal mouse users. BUT: the sidecar's own
guard (rpc.py:762-772) is the actual safety net, and it returns a structured
INVALID_REQUEST with `data.busy: true` for the duplicate. The frontend treats
this as a regular failure: `catch { fail(String(e)) }` → red error banner saying
"Another long-running operation ('dedupe') is already in progress. Cancel it
before starting 'dedupe'." This is confusing — the user double-clicked their
OWN scan; they don't know what "another operation" means. Worse, the first
scan's `finish()` won't fire (because the SECOND scan's `fail` clears `active`),
so the original scan continues in the sidecar but the UI thinks nothing is
running, the progress card vanishes, and the eventual result from scan #1 lands
into a store that's no longer expecting it (sets a stale report after the user
has moved on).
**Reproduction**: Double-click Scan as fast as possible on a slow scan. Error
banner appears; progress UI disappears; report still pops in seconds later as a
surprise.
**Fix**: Detect `data.busy === true` in the catch and either treat it as a no-op
(the original op is still running, leave `active` alone) or just keep
`active="dedupe"` until the original op completes. Don't clear progress state
from a guard-rejection.

---

## 10. "Send to trash" confirm copy is reassuring but cross-platform behavior varies silently

**Risk**: LOW
**Button / flow**: Send to trash
**File**: ui/src/components/DuplicatesView.tsx:204-206; vibechek/duplicates.py:350-370
**What breaks**: The confirm modal says "Files go to the OS trash and stay
recoverable until you empty it." That's accurate on Windows (Recycle Bin), macOS
(Trash), and most Linux desktops with a `~/.local/share/Trash`. BUT: `send2trash`
on headless Linux / WSL / network-mounted drives / FAT32 USB drives falls back to
**permanent delete** with no warning, because there's no XDG trash spec on those
paths. The user reads "recoverable" and proceeds; files are gone. There's no
preflight that confirms send2trash can actually trash the specific files.
Also: send2trash is a late import (line 352); if the user never installed it
(`pip install send2trash` is a runtime dep not declared in pyproject.toml — I
didn't verify but worth checking), the RPC raises `RuntimeError` → toast says
the install message but the user has no install button.
**Reproduction**: Run the Move/Trash flow with files on a FAT32 USB stick on
Linux → files are permanently deleted, confirm copy lied.
**Fix**: Probe send2trash compatibility on the target path before showing the
confirm modal (a no-op `send2trash` of a temp file in the same folder, or just
check the filesystem type). If unsupported, warn in the modal: "These files
cannot be sent to the trash and will be permanently deleted." Also: verify
send2trash is in pyproject.toml.

---

## 11. Keeper-rule eval is fragile on malformed `FileInfo` — NaN comparators, silent ties

**Risk**: LOW
**Button / flow**: Auto-pick logic — every render of the report
**File**: ui/src/lib/keeperRules.ts:74-100
**What breaks**: `compareForRule` size branch (line 89): `(b.size_bytes ?? b.size_mb * 1024 * 1024)` — if BOTH `size_bytes` is null AND `size_mb` is null, this evaluates `null * 1024 * 1024 = 0`, not NaN, so it's actually safe-ish (both files compared equal → tie). But: `bitrate` branch coerces null → 0, so a file with no bitrate is treated as having LOWER bitrate than any file with any bitrate. If a corrupt FLAC has no bitrate but a clean MP3 does, the MP3 wins by the bitrate rule even though the codec rule should have won earlier — except the codec rule already put FLAC first, so the bitrate rule is never reached. Net: the bitrate-null-coerced-to-0 is rule-order-dependent and usually doesn't bite, but it's an unprincipled "0 means missing" that could flip if a user reorders rules to put bitrate first. Similarly `modified_time ?? 0` makes a file with unknown mtime look ancient (loses to everything by the modified-newer rule). The user has no signal that auto-pick is using sentinel values.
**Reproduction**: Construct a duplicate group where one file's mutagen probe failed (bitrate_kbps=null) and the other is a high-bitrate MP3. Put "bitrate" rule first. The null-bitrate file (which might be the FLAC) loses despite arguably being the right keeper.
**Fix**: Treat null as "skip this rule for this pair" (return 0), not "loses to anything." For sentinel-prone rules (bitrate, modified_time), filter the rule out of the comparator chain when either file has a missing value, falling through to the next rule.

---

## Notes / non-findings checked

- **`.catch(() => {})` silent swallows** — none in DuplicatesView; the only swallow is `PreflightDialog.tsx:71-73` (`/* nothing to do; server will surface the error if any */`) which is outside scope.
- **What if find_duplicates returns no duplicates?** — `GroupsList` renders "No duplicates found." inside the report shell. RulesPanel + ActionBar still render with disabled buttons; mildly cluttered but not broken.
- **What if user closes the tab mid-find?** — DuplicatesView is unmounted (App.tsx:54 mounts only when viewMode === "duplicates"), but the RPC continues in the sidecar and the global long-op lock stays held. When the user navigates back, the report eventually appears via setReport. See finding #1 for the cancel-impossible problem this creates.
- **Cancel mid-find leaves partial report around?** — Currently moot: cancel doesn't work (finding #1). If it did: `setReport` is only called on success, so a cancel would NOT mutate the existing report. Good.
- **`extra` slot on ConfirmModal for "back up tags first"** — not used in Duplicates flow; tag-backup nudge before destructive action is missing for Move/Trash too. Minor.
