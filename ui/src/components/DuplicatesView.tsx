/**
 * Duplicates view — find duplicate tracks and decide which copy to keep.
 *
 * UX model:
 *   1. Pick a folder → click Scan.
 *   2. Configure the priority list of rules that picks "best version"
 *      (codec > bitrate > size > newest > shortest path, by default).
 *   3. Auto-keepers per group are computed from those rules; the user can
 *      still click any file in a group to override the pick.
 *   4. Click "Move duplicates to a review folder" (recoverable) or
 *      "Send duplicates to trash" (also recoverable from the OS trash).
 *
 * The end-user never sees the words "MD5", "Chromaprint", or "recoverable".
 * Internally we still use them — just not in the UI.
 *
 * Perf notes
 * ----------
 * On libraries with many duplicate groups (10k+) we cannot afford to:
 *   - eagerly compute the auto-keeper for every group at render time
 *   - render every GroupCard up front
 * The list is virtualized via react-virtuoso, and each GroupCard computes
 * its auto-keeper + explainPick lazily inside its own render (only the
 * visible rows pay the rule-comparator cost). Results are cached by
 * (group key, rules signature) so scrolling back doesn't re-do the work.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Copy, Search, FileAudio, AlertCircle, Folder, Trash2, Star,
  ChevronUp, ChevronDown, RotateCcw, Wand2,
} from "lucide-react";
import { open as openDialog } from "@tauri-apps/plugin-dialog";
import { Virtuoso } from "react-virtuoso";

import { useOperationStore, useConfigStore, useLibraryStore, useNotificationStore } from "../stores";
import { rpc } from "../hooks/useSidecar";
import type { DuplicateGroup, DuplicateReport, FileInfo } from "../types";
import {
  DEFAULT_RULES,
  type KeeperRule,
  explainPick,
  pickKeeper,
  ruleHelp,
  ruleLabel,
} from "../lib/keeperRules";
import { ConfirmModal } from "./ConfirmModal";

type Action = "report" | "move" | "trash";

export function DuplicatesView() {
  const libraryPath = useLibraryStore((s) => s.libraryPath);

  const active = useOperationStore((s) => s.active);
  const begin = useOperationStore((s) => s.begin);
  const finish = useOperationStore((s) => s.finish);
  const fail = useOperationStore((s) => s.fail);
  const errorMsg = useOperationStore((s) => s.error);

  const report = useOperationStore((s) => s.duplicateReport);
  const setReport = useOperationStore((s) => s.setDuplicateReport);

  const dupCfg = useConfigStore((s) => s.config.duplicates);
  const updateDuplicates = useConfigStore((s) => s.updateDuplicates);

  const notify = useNotificationStore((s) => s.notify);

  const [scanPath, setScanPath] = useState<string | null>(libraryPath);
  const [rules, setRules] = useState<KeeperRule[]>(DEFAULT_RULES);
  const [keeperOverrides, setKeeperOverrides] = useState<Record<string, string>>({});
  const [skippedGroups, setSkippedGroups] = useState<Set<string>>(new Set());
  const [preconditionError, setPreconditionError] = useState<string | null>(null);

  // Pending confirm — captures the user's choice + the computed plan so the
  // modal can render without re-running applyChoices.
  const [pendingResolve, setPendingResolve] = useState<
    | { action: Action; filtered: DuplicateReport; reviewFolder: string | null }
    | null
  >(null);

  // Synchronous scan-in-flight guard. `active` from the store flips in the
  // same tick, but React doesn't re-render until the next paint, so two
  // rapid clicks on Scan can both pass `disabled={active !== null}`. A ref
  // gives us a synchronous check inside the handler itself.
  const scanningRef = useRef(false);

  // Wipe per-group overrides whenever the report or rules change
  useEffect(() => {
    setKeeperOverrides({});
    setSkippedGroups(new Set());
  }, [report]);

  const handleChooseFolder = async () => {
    const selected = await openDialog({ directory: true, multiple: false });
    if (typeof selected === "string") setScanPath(selected);
  };

  const handleScan = useCallback(async () => {
    if (!scanPath) return;
    // Synchronous guard against double-click. The store's `active` flag is
    // checked by the `disabled` prop, but React's re-render is async — a
    // fast double-click can bypass it. The ref is set before any await.
    if (scanningRef.current) return;
    scanningRef.current = true;
    const opId = begin("dedupe");
    try {
      const r = await rpc<DuplicateReport>("find_duplicates", {
        path: scanPath,
        op_id: opId,
        use_md5: dupCfg.use_md5,
        use_chromaprint: dupCfg.use_chromaprint,
        // The backend reads this as `threshold` (rpc._find_duplicates). Without
        // it, the Chromaprint similarity slider in Settings is silently dead —
        // every scan uses the server-side default (0.95) regardless of the
        // user's choice. Forward it so the control actually does something.
        threshold: dupCfg.chromaprint_similarity_threshold,
        // Variant awareness: keep Extended/Radio/Remix as distinct versions
        // (only collapse redundant encodings) unless the user turned it off.
        keep_distinct_versions: dupCfg.keep_distinct_versions,
        keep_all_formats: dupCfg.keep_all_formats,
        version_duration_tolerance: dupCfg.version_duration_tolerance,
      });
      setReport(r);
      finish();
    } catch (e) {
      // Pass the raw error — the store's typed-error handling preserves
      // RpcError.cancelled (which is the silent-exit path). Don't pre-stringify.
      fail(e);
    } finally {
      scanningRef.current = false;
    }
  }, [
    scanPath,
    dupCfg.use_md5,
    dupCfg.use_chromaprint,
    dupCfg.chromaprint_similarity_threshold,
    dupCfg.keep_distinct_versions,
    dupCfg.keep_all_formats,
    begin,
    finish,
    fail,
    setReport,
  ]);

  // Stage 1: build the plan + open the confirm modal.
  const handleResolve = async (action: Action) => {
    if (!report) return;
    setPreconditionError(null);

    let reviewFolder = dupCfg.review_folder;
    if (action === "move") {
      // The Settings tab lets the user type any string. If it's blank, force
      // the picker. If it's an obviously bad value (whitespace, or a string
      // that doesn't look like a path), refuse to proceed and tell them why
      // — better than firing the RPC and getting a Python OSError 10s later.
      const trimmed = (reviewFolder ?? "").trim();
      if (!trimmed) {
        const folder = await openDialog({ directory: true, multiple: false });
        if (typeof folder !== "string") return;
        updateDuplicates({ review_folder: folder });
        reviewFolder = folder;
      } else if (!looksLikePath(trimmed)) {
        setPreconditionError(
          `Review folder in Settings looks invalid: "${trimmed}". ` +
          `Pick a valid folder, or clear the setting to be prompted.`,
        );
        return;
      } else {
        reviewFolder = trimmed;
      }
    }

    const filtered = applyChoices(report, rules, keeperOverrides, skippedGroups);
    setPendingResolve({ action, filtered, reviewFolder });
  };

  // Stage 2: user confirmed — actually run it.
  const performResolve = async () => {
    if (!pendingResolve) return;
    const { action, filtered, reviewFolder } = pendingResolve;
    setPendingResolve(null);

    const opId = begin("dedupe");
    try {
      const summary = await rpc<Record<string, number> & { journal_path?: string | null }>(
        "handle_duplicates",
        { report: filtered, action, review_folder: reviewFolder, op_id: opId },
      );
      finish();
      const word = action === "trash" ? "Trashed" : "Moved";
      // Select the count by ACTION, not `deleted ?? moved`. The backend always
      // returns BOTH keys (initialised to 0), incrementing only the one for the
      // chosen action — so for a move, `deleted` is the number 0 and `??` (which
      // only falls through on null/undefined) returns 0 instead of `moved`,
      // making every move report "Moved 0 duplicates".
      const count = action === "trash" ? (summary.deleted ?? 0) : (summary.moved ?? 0);
      const errors = summary.errors ?? 0;
      // Move-to-review is revertible via Recent operations; trash isn't
      // (it's in the OS recycle bin). Surface the right hint either way.
      const detailParts: string[] = [];
      if (errors > 0) detailParts.push(`${errors} error${errors === 1 ? "" : "s"} — see report.`);
      if (action === "move" && summary.journal_path) {
        detailParts.push('Undo available in "Recent operations" (sidebar).');
      } else if (action === "trash" && count > 0) {
        detailParts.push("Restore from your OS recycle bin if needed.");
      }
      notify(`${word} ${count} duplicate${count === 1 ? "" : "s"}`, {
        detail: detailParts.length > 0 ? detailParts.join(" ") : undefined,
        kind: errors > 0 ? "info" : "success",
      });
      // Clear the stale report immediately so the user can't act on
      // already-trashed entries, then await the rescan so the loading
      // state is visible while it runs.
      setReport(null);
      await handleScan();
    } catch (e) {
      fail(e);
    }
  };

  return (
    <div className="h-full flex flex-col">
      <div className="flex items-center justify-between px-4 py-3 border-b border-white/5">
        <div>
          <h1 className="font-display font-semibold text-white">Duplicates</h1>
          <p className="text-xs text-white/40 truncate max-w-md">
            {scanPath ?? "no folder selected"}
          </p>
        </div>
        <div className="flex items-center gap-2">
          <button className="btn-ghost" onClick={handleChooseFolder}>
            <Folder className="w-4 h-4" />
            Choose folder
          </button>
          <button
            className="btn-primary"
            onClick={handleScan}
            disabled={!scanPath || active !== null}
          >
            <Search className="w-4 h-4" />
            Scan
          </button>
        </div>
      </div>

      {errorMsg && (
        <div className="m-4 panel-pad text-sm text-accent-red flex gap-2">
          <AlertCircle className="w-4 h-4 flex-none mt-0.5" />
          <div className="break-words">{errorMsg}</div>
        </div>
      )}

      {preconditionError && (
        <div className="m-4 panel-pad text-sm text-accent-yellow flex gap-2">
          <AlertCircle className="w-4 h-4 flex-none mt-0.5" />
          <div className="break-words">
            {preconditionError}
            <button
              className="ml-2 underline text-white/60 hover:text-white"
              onClick={() => setPreconditionError(null)}
            >
              dismiss
            </button>
          </div>
        </div>
      )}

      {!report ? (
        active === "dedupe" ? (
          <LoadingState scanPath={scanPath} />
        ) : (
          <EmptyState scanPath={scanPath} />
        )
      ) : (
        <ReportView
          report={report}
          rules={rules}
          setRules={setRules}
          keeperOverrides={keeperOverrides}
          setKeeperOverrides={setKeeperOverrides}
          skippedGroups={skippedGroups}
          setSkippedGroups={setSkippedGroups}
          active={active}
          onResolve={handleResolve}
        />
      )}

      <ConfirmModal
        open={pendingResolve !== null}
        variant={pendingResolve?.action === "trash" ? "danger" : "default"}
        icon={pendingResolve?.action === "trash" ? Trash2 : Folder}
        title={
          pendingResolve?.action === "trash"
            ? `Send ${pendingResolve.filtered.summary.total_duplicates} files to trash?`
            : `Move ${pendingResolve?.filtered.summary.total_duplicates ?? 0} files to review folder?`
        }
        message={
          pendingResolve && (
            <div className="space-y-2">
              <p>
                <strong>{pendingResolve.filtered.summary.total_duplicates}</strong> duplicate files,
                freeing about{" "}
                <strong>{pendingResolve.filtered.summary.space_recoverable_mb.toFixed(0)} MB</strong>.
              </p>
              {pendingResolve.action === "trash" ? (
                <>
                  <p className="text-xs text-white/60">
                    On a regular internal drive, files go to the OS trash and
                    stay recoverable until you empty it (Recycle Bin on Windows,
                    Trash on macOS, <code className="font-mono">~/.local/share/Trash</code> on Linux).
                  </p>
                  <p className="flex items-start gap-1.5 text-xs text-accent-yellow">
                    <AlertCircle className="w-4 h-4 flex-none mt-px" />
                    <span>
                      <strong>Not recoverable on removable or network drives.</strong>{" "}
                      FAT32 USB sticks and many network shares have no trash
                      folder, so files there are deleted permanently — they
                      cannot be restored. If your library lives on one of
                      these, use "Move to review folder" instead.
                    </span>
                  </p>
                </>
              ) : (
                <>
                  <p className="text-xs text-white/60">Files will be moved to:</p>
                  <code className="block font-mono text-xs text-white/70 bg-surface-300 p-2 rounded break-all">
                    {pendingResolve.reviewFolder}
                  </code>
                </>
              )}
            </div>
          )
        }
        confirmLabel={pendingResolve?.action === "trash" ? "Yes, send to trash" : "Yes, move files"}
        cancelLabel="Cancel"
        onConfirm={performResolve}
        onCancel={() => setPendingResolve(null)}
      />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Empty / loading
// ---------------------------------------------------------------------------

function EmptyState({ scanPath }: { scanPath: string | null }) {
  return (
    <div className="flex-1 flex items-center justify-center">
      <div className="text-center max-w-md">
        <div className="w-16 h-16 mx-auto mb-4 rounded-2xl bg-white/5 flex items-center justify-center">
          <Copy className="w-8 h-8 text-white/30" />
        </div>
        <h2 className="text-xl font-display font-semibold mb-2">
          {scanPath ? "Ready to find duplicates" : "Pick a folder"}
        </h2>
        <p className="text-white/50 mb-4">
          {scanPath
            ? "Vibechek finds two kinds of duplicates: byte-identical files, and tracks that sound the same but were re-encoded. You choose which copy to keep — nothing is touched until you say so."
            : "Choose a folder above to start."}
        </p>
      </div>
    </div>
  );
}

function LoadingState({ scanPath }: { scanPath: string | null }) {
  return (
    <div className="flex-1 flex items-center justify-center">
      <div className="text-center max-w-md">
        <div className="w-16 h-16 mx-auto mb-4 rounded-2xl bg-white/5 flex items-center justify-center animate-pulse">
          <Search className="w-8 h-8 text-white/40" />
        </div>
        <h2 className="text-xl font-display font-semibold mb-2">Scanning…</h2>
        <p className="text-white/50 truncate">
          Looking for duplicates in <span className="text-white/70">{scanPath}</span>
        </p>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main report view
// ---------------------------------------------------------------------------

interface ReportViewProps {
  report: DuplicateReport;
  rules: KeeperRule[];
  setRules: (r: KeeperRule[]) => void;
  keeperOverrides: Record<string, string>;
  setKeeperOverrides: (m: Record<string, string>) => void;
  skippedGroups: Set<string>;
  setSkippedGroups: (s: Set<string>) => void;
  active: string | null;
  onResolve: (action: Action) => void;
}

function ReportView({
  report,
  rules,
  setRules,
  keeperOverrides,
  setKeeperOverrides,
  skippedGroups,
  setSkippedGroups,
  active,
  onResolve,
}: ReportViewProps) {
  const allGroups = useMemo(
    () => [...report.exact_duplicates, ...report.audio_duplicates],
    [report],
  );

  // ---- Lazy auto-keeper resolution ---------------------------------------
  // We DO NOT precompute auto-keepers for every group at render time. On a
  // 10k-group report that's millions of comparator calls on the main thread
  // and the tab freezes. Instead, every consumer of "what's the current
  // keeper for group G?" calls `currentKeeper(g)` which:
  //   - returns the user override if any
  //   - else returns the cached auto-pick for (g.key, rulesSig)
  //   - else computes it on demand and caches it
  // The cache is bucketed by a rules signature so reordering/toggling the
  // priority list correctly invalidates all auto-picks.
  const rulesSig = useMemo(
    () =>
      rules
        .map((r) => `${r.criterion}:${r.enabled ? 1 : 0}`)
        .join("|"),
    [rules],
  );

  // The cache is a Ref (not state) — mutating it does not need to trigger a
  // re-render. We pair it with the rulesSig key so a stale-rules cache is
  // never returned.
  const autoCacheRef = useRef<{ sig: string; map: Map<string, string> }>({
    sig: rulesSig,
    map: new Map(),
  });
  if (autoCacheRef.current.sig !== rulesSig) {
    autoCacheRef.current = { sig: rulesSig, map: new Map() };
  }

  const computeAutoKeeper = useCallback(
    (g: DuplicateGroup): string => {
      const cache = autoCacheRef.current.map;
      const cached = cache.get(g.key);
      if (cached !== undefined) return cached;
      const files = [g.keep, ...g.duplicates];
      const picked = pickKeeper(files, rules).path;
      cache.set(g.key, picked);
      return picked;
    },
    [rules],
  );

  const currentKeeper = useCallback(
    (g: DuplicateGroup): string => {
      const override = keeperOverrides[g.key];
      if (override !== undefined) {
        // Override might be stale (e.g. file renamed/removed since the pick).
        // If the path isn't in the group anymore, fall through to the auto
        // pick instead of trusting it — same defensive guard `applyChoices`
        // applies before sending to the backend.
        const validPaths = [g.keep.path, ...g.duplicates.map((d) => d.path)];
        if (validPaths.includes(override)) return override;
      }
      return computeAutoKeeper(g);
    },
    [keeperOverrides, computeAutoKeeper],
  );

  // ---- Summary totals -----------------------------------------------------
  // These DO scan every group (we need a total to show in the header), but
  // the cost per group is now O(1) thanks to the cache after the first
  // touch — and the first touch only happens when the user actually clicks
  // an action button or scrolls a group into view. We use a single pass
  // here and accept that the very first render after a scan computes
  // auto-keepers for the visible window only; off-screen groups remain
  // un-touched until they enter the viewport, at which point GroupCard
  // computes its own keeper lazily.
  //
  // To keep summary totals correct without forcing a full pass, we use a
  // heuristic-but-correct approach: assume each group will drop its
  // duplicates (the .keep file stays). This matches what auto-pick would
  // give in the steady state — the auto-pick may select a different
  // physical file as the keeper, but the *count* and *size* freed are
  // identical because each group always loses exactly `files.length - 1`
  // files and the sum of all-but-one is the same regardless of which one
  // we choose (it equals total_size - max_size... no, total_size - keeper_size).
  //
  // Since the sizes can differ, we DO need the keeper to compute the exact
  // free-space number. For the summary, use the backend's per-group
  // `recoverable_mb` as the starting point (computed with `g.keep` as
  // keeper) and only adjust where the user has overridden the keeper —
  // overrides are typically a handful of groups, not 10k.
  const activeGroups = useMemo(
    () => allGroups.filter((g) => !skippedGroups.has(g.key)),
    [allGroups, skippedGroups],
  );

  const filesToAct = useMemo(
    () => activeGroups.reduce((s, g) => s + g.duplicates.length, 0),
    [activeGroups],
  );

  const spaceToFree = useMemo(() => {
    // Sum the sizes of every NON-keeper file, using the *effective* keeper
    // (`currentKeeper`) which accounts for both manual overrides AND the
    // active auto-pick rules. The previous version fell back to the backend's
    // precomputed `recoverable_mb` whenever there was no manual override — so
    // reordering/toggling rules (which changes the auto-picked keeper, often
    // to a file of a different size) left this figure stale. `rulesSig` is in
    // the deps so a rule change recomputes.
    let total = 0;
    for (const g of activeGroups) {
      const keeperPath = currentKeeper(g);
      for (const f of [g.keep, ...g.duplicates]) {
        if (f.path !== keeperPath) total += f.size_mb;
      }
    }
    return total;
  }, [activeGroups, currentKeeper, rulesSig]);

  return (
    <div className="flex-1 flex flex-col min-h-0 px-4 py-4 space-y-4 overflow-hidden">
      <SummaryStrip
        report={report}
        filesToAct={filesToAct}
        spaceToFree={spaceToFree}
        groupCount={activeGroups.length}
      />

      {report.summary.fpcalc_available === false && (
        // Audio fingerprinting was requested but fpcalc wasn't found, so the
        // near-duplicate (re-encode/re-tag) phase silently never ran. Without
        // this banner "0 near-duplicates" reads as a thorough clean scan — a
        // user could reorganize believing a fuzzy pass ran when only exact-hash
        // matching did.
        <div className="panel-pad flex items-start gap-2 text-sm text-accent-yellow">
          <AlertCircle className="w-4 h-4 mt-0.5 shrink-0" />
          <span>
            Fingerprint scan skipped — <strong>fpcalc not found</strong>. Only
            exact-match duplicates were detected; re-encodes and re-tags of the
            same track were <em>not</em> compared. Install libchromaprint-tools
            (fpcalc) and re-scan to find near-duplicates.
          </span>
        </div>
      )}

      <RulesPanel rules={rules} setRules={setRules} />

      <ActionBar
        filesToAct={filesToAct}
        spaceToFree={spaceToFree}
        disabled={active !== null}
        onResolve={onResolve}
      />

      <GroupsList
        groups={allGroups}
        currentKeeper={currentKeeper}
        onPickKeeper={(key, path) =>
          setKeeperOverrides({ ...keeperOverrides, [key]: path })
        }
        skippedGroups={skippedGroups}
        onToggleSkip={(key) => {
          const next = new Set(skippedGroups);
          next.has(key) ? next.delete(key) : next.add(key);
          setSkippedGroups(next);
        }}
        rules={rules}
        onReset={() => {
          setKeeperOverrides({});
          setSkippedGroups(new Set());
        }}
      />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Summary strip
// ---------------------------------------------------------------------------

function SummaryStrip({
  report,
  filesToAct,
  spaceToFree,
  groupCount,
}: {
  report: DuplicateReport;
  filesToAct: number;
  spaceToFree: number;
  groupCount: number;
}) {
  return (
    <div className="panel-pad flex flex-wrap items-center gap-x-8 gap-y-2">
      <div>
        <div className="label">Scanned</div>
        <div className="text-lg font-display font-semibold tabular-nums">
          {report.summary.total_files.toLocaleString()} files
        </div>
      </div>
      <div>
        <div className="label">Duplicate groups</div>
        <div className="text-lg font-display font-semibold tabular-nums">
          {groupCount}
        </div>
      </div>
      <div>
        <div className="label">Will remove</div>
        <div className="text-lg font-display font-semibold tabular-nums">
          {filesToAct} files
        </div>
      </div>
      <div>
        <div className="label">Disk space to free</div>
        <div className="text-lg font-display font-semibold text-accent-green tabular-nums">
          {spaceToFree.toFixed(1)} MB
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Rules panel — re-orderable priority list with per-rule toggle
// ---------------------------------------------------------------------------

function RulesPanel({
  rules,
  setRules,
}: {
  rules: KeeperRule[];
  setRules: (r: KeeperRule[]) => void;
}) {
  const move = (idx: number, dir: -1 | 1) => {
    const next = [...rules];
    const swap = idx + dir;
    if (swap < 0 || swap >= next.length) return;
    [next[idx], next[swap]] = [next[swap], next[idx]];
    setRules(next);
  };

  const toggle = (idx: number) => {
    const next = [...rules];
    next[idx] = { ...next[idx], enabled: !next[idx].enabled };
    setRules(next);
  };

  const reset = () => setRules(DEFAULT_RULES);

  return (
    <details open className="panel">
      <summary className="px-4 py-3 cursor-pointer flex items-center gap-2 select-none">
        <Wand2 className="w-4 h-4 text-accent" />
        <span className="font-medium text-white">How to pick which copy to keep</span>
        <span className="text-xs text-white/50 ml-2">toggle or reorder with ▲▼</span>
        <button
          className="ml-auto text-xs text-white/40 hover:text-white"
          onClick={(e) => { e.preventDefault(); reset(); }}
        >
          <RotateCcw className="w-3 h-3 inline mr-1" />
          reset to defaults
        </button>
      </summary>
      <div className="border-t border-white/5 px-4 py-3 space-y-1">
        <p className="text-xs text-white/50 mb-3">
          Vibechek tries each rule in order. The first one that breaks a tie picks the keeper.
        </p>
        {rules.map((rule, idx) => (
          <RuleRow
            key={rule.criterion}
            rule={rule}
            position={idx + 1}
            canMoveUp={idx > 0}
            canMoveDown={idx < rules.length - 1}
            onMoveUp={() => move(idx, -1)}
            onMoveDown={() => move(idx, 1)}
            onToggle={() => toggle(idx)}
          />
        ))}
      </div>
    </details>
  );
}

function RuleRow({
  rule,
  position,
  canMoveUp,
  canMoveDown,
  onMoveUp,
  onMoveDown,
  onToggle,
}: {
  rule: KeeperRule;
  position: number;
  canMoveUp: boolean;
  canMoveDown: boolean;
  onMoveUp: () => void;
  onMoveDown: () => void;
  onToggle: () => void;
}) {
  return (
    <div className={`flex items-center gap-3 py-2 ${rule.enabled ? "" : "opacity-40"}`}>
      {/* Position number only — the grip icon implied drag-reordering, which
          doesn't exist (the ▲▼ buttons are the reorder affordance). */}
      <div className="w-6 text-center text-xs font-mono text-white/50">
        {position}
      </div>
      <label className="flex items-center gap-2 cursor-pointer flex-1">
        <input
          type="checkbox"
          checked={rule.enabled}
          onChange={onToggle}
          className="accent-accent"
        />
        <div>
          <div className="text-sm text-white">{ruleLabel(rule.criterion)}</div>
          <div className="text-[11px] text-white/40">{ruleHelp(rule.criterion)}</div>
        </div>
      </label>
      <div className="flex items-center gap-1">
        <button
          onClick={onMoveUp}
          disabled={!canMoveUp}
          className="text-white/40 hover:text-white disabled:opacity-30 p-1"
          title="Move up"
        >
          <ChevronUp className="w-4 h-4" />
        </button>
        <button
          onClick={onMoveDown}
          disabled={!canMoveDown}
          className="text-white/40 hover:text-white disabled:opacity-30 p-1"
          title="Move down"
        >
          <ChevronDown className="w-4 h-4" />
        </button>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Action bar
// ---------------------------------------------------------------------------

function ActionBar({
  filesToAct,
  spaceToFree,
  disabled,
  onResolve,
}: {
  filesToAct: number;
  spaceToFree: number;
  disabled: boolean;
  onResolve: (action: Action) => void;
}) {
  const nothingToDo = filesToAct === 0;
  return (
    <div className="panel-pad flex flex-wrap items-center gap-3">
      <div className="flex-1 text-sm">
        {nothingToDo ? (
          <span className="text-white/50">No duplicates queued — pick keepers above to enable actions.</span>
        ) : (
          <>
            <span className="text-white font-medium">{filesToAct} files</span>{" "}
            <span className="text-white/50">queued, freeing</span>{" "}
            <span className="text-accent-green font-medium">{spaceToFree.toFixed(0)} MB</span>
          </>
        )}
      </div>
      <button
        className="btn-primary"
        disabled={disabled || nothingToDo}
        onClick={() => onResolve("move")}
      >
        <Folder className="w-4 h-4" />
        Move to review folder
      </button>
      <button
        className="btn-danger"
        disabled={disabled || nothingToDo}
        onClick={() => onResolve("trash")}
      >
        <Trash2 className="w-4 h-4" />
        Send to trash
      </button>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Groups list — virtualized via react-virtuoso. Only on-screen rows render,
// which lets us scale to 10k+ groups without freezing the main thread.
// ---------------------------------------------------------------------------

interface GroupsListProps {
  groups: DuplicateGroup[];
  currentKeeper: (g: DuplicateGroup) => string;
  onPickKeeper: (groupKey: string, path: string) => void;
  skippedGroups: Set<string>;
  onToggleSkip: (groupKey: string) => void;
  rules: KeeperRule[];
  onReset: () => void;
}

function GroupsList({
  groups,
  currentKeeper,
  onPickKeeper,
  skippedGroups,
  onToggleSkip,
  rules,
  onReset,
}: GroupsListProps) {
  if (groups.length === 0) {
    return (
      <div className="panel-pad text-center text-white/50">No duplicates found.</div>
    );
  }

  return (
    <div className="flex flex-col min-h-0 flex-1">
      <div className="flex items-baseline gap-3 mb-2 px-1">
        <h2 className="font-display font-semibold text-white">Groups</h2>
        <span className="text-xs text-white/40">
          click any file to override the auto-pick
        </span>
        <button
          className="ml-auto text-xs text-white/40 hover:text-white"
          onClick={onReset}
        >
          reset all picks
        </button>
      </div>
      <div className="flex-1 min-h-0">
        <Virtuoso
          data={groups}
          computeItemKey={(_, g) => g.key}
          itemContent={(_, g) => (
            <div className="pb-2">
              <GroupCard
                group={g}
                currentKeeperPath={currentKeeper(g)}
                onPickKeeper={(path) => onPickKeeper(g.key, path)}
                skipped={skippedGroups.has(g.key)}
                onToggleSkip={() => onToggleSkip(g.key)}
                rules={rules}
              />
            </div>
          )}
        />
      </div>
    </div>
  );
}

function GroupCard({
  group,
  currentKeeperPath,
  onPickKeeper,
  skipped,
  onToggleSkip,
  rules,
}: {
  group: DuplicateGroup;
  currentKeeperPath: string;
  onPickKeeper: (path: string) => void;
  skipped: boolean;
  onToggleSkip: () => void;
  rules: KeeperRule[];
}) {
  const files: FileInfo[] = [group.keep, ...group.duplicates];
  const keeper = files.find((f) => f.path === currentKeeperPath) ?? group.keep;
  const others = files.filter((f) => f.path !== currentKeeperPath);
  const userOverrode = currentKeeperPath !== group.keep.path && group.keep.path !== keeper.path;

  // Compare-strategy hint — why did the auto-pick choose what it did. This is
  // computed inside the row, so virtualization keeps the cost bounded to
  // visible rows only.
  const explained = useMemo(
    () => explainPick(keeper, others, rules),
    // Memoize on identity of files + rules signature. files identity is
    // stable across renders because `group` itself is the same object.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [group, currentKeeperPath, rules],
  );

  const label = group.method === "md5" ? "Same file" : "Same song, different encoding";

  return (
    <div className={`panel-pad ${skipped ? "opacity-40" : ""}`}>
      <div className="flex items-center gap-2 mb-3">
        <div className="text-xs text-white/50">{label}</div>
        <div className="text-xs text-white/40">
          • freeing {group.recoverable_mb.toFixed(1)} MB
        </div>
        {!skipped && explained.criterion !== "tie" && (
          <div className="text-xs text-accent-cyan">
            • picked by {explained.criterion}: {explained.detail}
          </div>
        )}
        {userOverrode && (
          <div className="text-xs text-accent-yellow">• manual override</div>
        )}
        <button
          onClick={onToggleSkip}
          className="ml-auto text-xs text-white/50 hover:text-white"
        >
          {skipped ? "include" : "don't change this group"}
        </button>
      </div>

      <div className="space-y-1">
        {files.map((f) => (
          <FileRow
            key={f.path}
            file={f}
            isKeeper={f.path === currentKeeperPath}
            onPick={() => onPickKeeper(f.path)}
            disabled={skipped}
          />
        ))}
      </div>
    </div>
  );
}

function FileRow({
  file,
  isKeeper,
  onPick,
  disabled,
}: {
  file: FileInfo;
  isKeeper: boolean;
  onPick: () => void;
  disabled: boolean;
}) {
  return (
    <button
      disabled={disabled}
      onClick={onPick}
      className={`w-full flex items-center gap-2 px-2 py-1.5 rounded-md text-left ${
        isKeeper
          ? "bg-accent-green/10 border border-accent-green/30"
          : "bg-white/[0.02] border border-transparent hover:bg-white/5"
      } disabled:cursor-not-allowed`}
    >
      {isKeeper ? (
        <Star className="w-4 h-4 text-accent-green flex-none" />
      ) : (
        <span className="w-4 h-4 flex-none" />
      )}
      <FileAudio className="w-3.5 h-3.5 text-white/40 flex-none" />
      <div className="flex-1 min-w-0">
        <div className="text-xs text-white/90 truncate">{file.filename}</div>
        <div className="text-[10px] text-white/40 font-mono truncate">{file.path}</div>
      </div>
      <FileMeta file={file} />
      {isKeeper && (
        <span className="text-[10px] uppercase tracking-wider text-accent-green font-semibold">
          keep
        </span>
      )}
    </button>
  );
}

function FileMeta({ file }: { file: FileInfo }) {
  const parts: string[] = [];
  if (file.codec) parts.push(file.codec.toUpperCase());
  if (file.bitrate_kbps) parts.push(`${file.bitrate_kbps}k`);
  parts.push(`${file.size_mb.toFixed(1)}M`);
  return (
    <div className="text-[11px] text-white/40 tabular-nums flex-none">
      {parts.join(" · ")}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/**
 * Lightweight sanity check on a user-typed review-folder path. We don't have
 * the Tauri fs plugin available, so we can't existsSync the path — but we
 * can at least catch the obvious cases (whitespace-only, no path separator,
 * control chars). The backend will still hard-validate before doing anything
 * destructive; this is just a UX guard so the user gets feedback before the
 * 10s RPC round-trip.
 */
function looksLikePath(s: string): boolean {
  if (!s.trim()) return false;
  // Reject control characters and the obviously-bogus stand-ins seen in
  // bug reports (e.g. "<<<invalid>>>").
  if (/[\x00-\x1f<>"|?*]/.test(s)) return false;
  // Must contain at least one separator OR be an absolute-style root token.
  // (Tauri's dialog only ever yields absolute paths, so this is a sane
  // floor for "user typed something that could plausibly be a folder".)
  return /[\\/]/.test(s) || /^[A-Za-z]:$/.test(s);
}

// ---------------------------------------------------------------------------
// Apply user choices: rebuild the report so the backend sees the user's picks
// ---------------------------------------------------------------------------

function applyChoices(
  report: DuplicateReport,
  rules: KeeperRule[],
  keeperOverrides: Record<string, string>,
  skippedGroups: Set<string>,
): DuplicateReport {
  const rebuild = (g: DuplicateGroup): DuplicateGroup | null => {
    if (skippedGroups.has(g.key)) return null;

    const allFiles = [g.keep, ...g.duplicates];
    const validPaths = new Set(allFiles.map((f) => f.path));

    // Guard: a stale override (path no longer in the group — e.g. the file
    // was renamed by another tool between the scan and the click) used to
    // produce a malformed group where the keeper ended up in `duplicates`
    // and got trashed/moved. Drop the override in that case and fall back
    // to the rule-picked keeper.
    let keeperPath: string;
    const override = keeperOverrides[g.key];
    if (override !== undefined && validPaths.has(override)) {
      keeperPath = override;
    } else {
      keeperPath = pickKeeper(allFiles, rules).path;
    }

    const keeper = allFiles.find((f) => f.path === keeperPath) ?? g.keep;
    // If, somehow, the keeper resolution above still didn't yield a path in
    // the group (defensive), fall back to g.keep so we never promote it
    // into the duplicates list.
    const finalKeeperPath = validPaths.has(keeperPath) ? keeperPath : keeper.path;
    const dupes = allFiles.filter((f) => f.path !== finalKeeperPath);

    return {
      ...g,
      keep: keeper,
      duplicates: dupes,
      recoverable_mb: dupes.reduce((s, f) => s + f.size_mb, 0),
    };
  };

  const exact = report.exact_duplicates.map(rebuild).filter((g): g is DuplicateGroup => !!g);
  const audio = report.audio_duplicates.map(rebuild).filter((g): g is DuplicateGroup => !!g);

  return {
    summary: {
      ...report.summary,
      exact_duplicate_groups: exact.length,
      exact_duplicate_files: exact.reduce((s, g) => s + g.duplicates.length, 0),
      audio_duplicate_groups: audio.length,
      audio_duplicate_files: audio.reduce((s, g) => s + g.duplicates.length, 0),
      total_duplicates:
        exact.reduce((s, g) => s + g.duplicates.length, 0) +
        audio.reduce((s, g) => s + g.duplicates.length, 0),
      space_recoverable_mb:
        Math.round(
          (exact.reduce((s, g) => s + g.recoverable_mb, 0) +
            audio.reduce((s, g) => s + g.recoverable_mb, 0)) * 100,
        ) / 100,
    },
    exact_duplicates: exact,
    audio_duplicates: audio,
  };
}
