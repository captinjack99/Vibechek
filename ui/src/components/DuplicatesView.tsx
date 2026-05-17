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
 */

import { useEffect, useMemo, useState } from "react";
import {
  Copy, Search, FileAudio, AlertCircle, Folder, Trash2, Star,
  ChevronUp, ChevronDown, GripVertical, RotateCcw, Wand2,
} from "lucide-react";
import { open as openDialog } from "@tauri-apps/plugin-dialog";

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

  // Pending confirm — captures the user's choice + the computed plan so the
  // modal can render without re-running applyChoices.
  const [pendingResolve, setPendingResolve] = useState<
    | { action: Action; filtered: DuplicateReport; reviewFolder: string | null }
    | null
  >(null);

  // Wipe per-group overrides whenever the report or rules change
  useEffect(() => {
    setKeeperOverrides({});
    setSkippedGroups(new Set());
  }, [report]);

  const handleChooseFolder = async () => {
    const selected = await openDialog({ directory: true, multiple: false });
    if (typeof selected === "string") setScanPath(selected);
  };

  const handleScan = async () => {
    if (!scanPath) return;
    begin("dedupe");
    try {
      const r = await rpc<DuplicateReport>("find_duplicates", {
        path: scanPath,
        use_md5: dupCfg.use_md5,
        use_chromaprint: dupCfg.use_chromaprint,
      });
      setReport(r);
      finish();
    } catch (e) {
      fail(String(e));
    }
  };

  // Stage 1: build the plan + open the confirm modal.
  const handleResolve = async (action: Action) => {
    if (!report) return;

    let reviewFolder = dupCfg.review_folder;
    if (action === "move" && !reviewFolder) {
      const folder = await openDialog({ directory: true, multiple: false });
      if (typeof folder !== "string") return;
      updateDuplicates({ review_folder: folder });
      reviewFolder = folder;
    }

    const filtered = applyChoices(report, rules, keeperOverrides, skippedGroups);
    setPendingResolve({ action, filtered, reviewFolder });
  };

  // Stage 2: user confirmed — actually run it.
  const performResolve = async () => {
    if (!pendingResolve) return;
    const { action, filtered, reviewFolder } = pendingResolve;
    setPendingResolve(null);

    begin("dedupe");
    try {
      const summary = await rpc<Record<string, number>>("handle_duplicates", {
        report: filtered,
        action,
        review_folder: reviewFolder,
      });
      finish();
      const word = action === "trash" ? "Trashed" : "Moved";
      const count = summary.deleted ?? summary.moved ?? 0;
      const errors = summary.errors ?? 0;
      notify(`${word} ${count} duplicate${count === 1 ? "" : "s"}`, {
        detail: errors > 0 ? `${errors} error${errors === 1 ? "" : "s"} — see report.` : undefined,
        kind: errors > 0 ? "info" : "success",
      });
      handleScan();
    } catch (e) {
      fail(String(e));
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

      {!report ? (
        <EmptyState scanPath={scanPath} />
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
                <p className="text-xs text-white/60">
                  Files go to the OS trash and stay recoverable until you empty it.
                </p>
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

  // Apply rules to compute the "auto" keeper for each group. Overrides take
  // precedence; skipped groups still get an auto-keeper but aren't acted on.
  const autoKeepers = useMemo(() => {
    const m: Record<string, string> = {};
    for (const g of allGroups) {
      const files = [g.keep, ...g.duplicates];
      m[g.key] = pickKeeper(files, rules).path;
    }
    return m;
  }, [allGroups, rules]);

  const currentKeeper = (g: DuplicateGroup): string =>
    keeperOverrides[g.key] ?? autoKeepers[g.key] ?? g.keep.path;

  const activeGroups = allGroups.filter((g) => !skippedGroups.has(g.key));
  const filesToAct = activeGroups.reduce((s, g) => s + g.duplicates.length, 0);
  const spaceToFree = activeGroups.reduce((s, g) => {
    // Sum sizes of all files except the chosen keeper
    const keeper = currentKeeper(g);
    const allFiles = [g.keep, ...g.duplicates];
    return s + allFiles.filter((f) => f.path !== keeper).reduce((ss, f) => ss + f.size_mb, 0);
  }, 0);

  return (
    <div className="flex-1 overflow-auto px-4 py-4 space-y-4">
      <SummaryStrip
        report={report}
        filesToAct={filesToAct}
        spaceToFree={spaceToFree}
        groupCount={activeGroups.length}
      />

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
        <span className="text-xs text-white/40 ml-2">drag, toggle, or reorder</span>
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
      <GripVertical className="w-4 h-4 text-white/20" />
      <div className="w-6 text-center text-xs font-mono text-white/40">
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
// Groups list
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
    <div>
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
      <div className="space-y-2">
        {groups.map((g) => (
          <GroupCard
            key={g.key}
            group={g}
            currentKeeperPath={currentKeeper(g)}
            onPickKeeper={(path) => onPickKeeper(g.key, path)}
            skipped={skippedGroups.has(g.key)}
            onToggleSkip={() => onToggleSkip(g.key)}
            rules={rules}
          />
        ))}
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

  // Compare-strategy hint — why did the auto-pick choose what it did
  const explained = explainPick(keeper, others, rules);

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
    const keeperPath =
      keeperOverrides[g.key] ?? pickKeeper(allFiles, rules).path;

    const keeper = allFiles.find((f) => f.path === keeperPath) ?? g.keep;
    const dupes = allFiles.filter((f) => f.path !== keeperPath);

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
