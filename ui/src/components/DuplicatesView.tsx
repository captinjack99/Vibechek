import { useEffect, useState } from "react";
import {
  Copy, Search, ArrowRight, FileAudio, AlertCircle,
  Folder, Trash2, Star,
} from "lucide-react";
import { open as openDialog } from "@tauri-apps/plugin-dialog";

import { useOperationStore, useConfigStore, useLibraryStore } from "../stores";
import { rpc } from "../hooks/useSidecar";
import type { DuplicateGroup, DuplicateReport, FileInfo } from "../types";
import { TagBadge } from "./TagBadges";

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

  const [scanPath, setScanPath] = useState<string | null>(libraryPath);

  // Per-group user choices:
  //   keeperOverrides[groupKey] = path of file the user picked to keep
  //   skippedGroups: groups the user wants to leave alone
  const [keeperOverrides, setKeeperOverrides] = useState<Record<string, string>>({});
  const [skippedGroups, setSkippedGroups] = useState<Set<string>>(new Set());

  // Reset overrides whenever the report changes
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

  const handleExecute = async (action: Action) => {
    if (!report) return;
    if (action === "move" && !dupCfg.review_folder) {
      const folder = await openDialog({ directory: true, multiple: false });
      if (typeof folder !== "string") return;
      updateDuplicates({ review_folder: folder });
      dupCfg.review_folder = folder;
    }

    // Build a filtered report that honors user choices
    const filtered = applyUserChoices(report, keeperOverrides, skippedGroups);

    begin("dedupe");
    try {
      const summary = await rpc<Record<string, number>>("handle_duplicates", {
        report: filtered,
        action,
        review_folder: dupCfg.review_folder,
      });
      finish();
      // Surface the result in-page
      alert(
        `Done. Moved: ${summary.moved ?? 0} • Trashed: ${summary.deleted ?? 0} • Errors: ${summary.errors ?? 0}`,
      );
      // Re-scan so the report reflects the new state
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
          active={active}
          keeperOverrides={keeperOverrides}
          setKeeperOverrides={setKeeperOverrides}
          skippedGroups={skippedGroups}
          setSkippedGroups={setSkippedGroups}
          onExecute={handleExecute}
        />
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------

function EmptyState({ scanPath }: { scanPath: string | null }) {
  return (
    <div className="flex-1 flex items-center justify-center">
      <div className="text-center max-w-md">
        <div className="w-16 h-16 mx-auto mb-4 rounded-2xl bg-white/5 flex items-center justify-center">
          <Copy className="w-8 h-8 text-white/30" />
        </div>
        <h2 className="text-xl font-display font-semibold mb-2">
          {scanPath ? "Ready to scan" : "Pick a folder"}
        </h2>
        <p className="text-white/50">
          {scanPath
            ? "Vibechek finds byte-identical (MD5) and acoustically identical (Chromaprint) duplicates. Pick a keeper per group, then move or trash the rest."
            : "Choose a folder above to scan for duplicates."}
        </p>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------

interface ReportViewProps {
  report: DuplicateReport;
  active: string | null;
  keeperOverrides: Record<string, string>;
  setKeeperOverrides: (m: Record<string, string>) => void;
  skippedGroups: Set<string>;
  setSkippedGroups: (s: Set<string>) => void;
  onExecute: (action: Action) => void;
}

function ReportView({
  report,
  active,
  keeperOverrides,
  setKeeperOverrides,
  skippedGroups,
  setSkippedGroups,
  onExecute,
}: ReportViewProps) {
  const { summary, exact_duplicates, audio_duplicates } = report;

  const overrideKeeper = (groupKey: string, path: string) =>
    setKeeperOverrides({ ...keeperOverrides, [groupKey]: path });

  const toggleSkip = (groupKey: string) => {
    const next = new Set(skippedGroups);
    if (next.has(groupKey)) next.delete(groupKey);
    else next.add(groupKey);
    setSkippedGroups(next);
  };

  const renderGroup = (g: DuplicateGroup) => (
    <GroupCard
      key={g.key}
      group={g}
      currentKeeper={keeperOverrides[g.key] ?? g.keep.path}
      onPickKeeper={(path) => overrideKeeper(g.key, path)}
      skipped={skippedGroups.has(g.key)}
      onToggleSkip={() => toggleSkip(g.key)}
    />
  );

  const activeGroups =
    summary.total_duplicates -
    [...exact_duplicates, ...audio_duplicates].filter((g) =>
      skippedGroups.has(g.key),
    ).length;

  return (
    <div className="flex-1 overflow-auto px-4 py-3">
      {/* Summary */}
      <div className="grid grid-cols-4 gap-3 mb-4">
        <Stat label="Total files" value={summary.total_files} />
        <Stat label="Exact dupes" value={summary.exact_duplicate_files} />
        <Stat label="Audio dupes" value={summary.audio_duplicate_files} />
        <Stat
          label="Recoverable"
          value={`${summary.space_recoverable_mb.toFixed(1)} MB`}
        />
      </div>

      {summary.total_duplicates === 0 ? (
        <div className="panel-pad text-center text-white/50">No duplicates found.</div>
      ) : (
        <>
          <ActionBar
            activeGroups={activeGroups}
            disabled={active !== null}
            onExecute={onExecute}
          />

          {exact_duplicates.length > 0 && (
            <Section title="Exact duplicates" subtitle="Byte-identical files (MD5)">
              {exact_duplicates.map(renderGroup)}
            </Section>
          )}

          {audio_duplicates.length > 0 && (
            <Section title="Audio duplicates" subtitle="Same audio, different encoding (Chromaprint)">
              {audio_duplicates.map(renderGroup)}
            </Section>
          )}
        </>
      )}
    </div>
  );
}

function ActionBar({
  activeGroups,
  disabled,
  onExecute,
}: {
  activeGroups: number;
  disabled: boolean;
  onExecute: (action: Action) => void;
}) {
  return (
    <div className="panel-pad mb-4 flex flex-wrap items-center gap-3">
      <div className="flex-1 text-sm">
        <span className="text-white">{activeGroups}</span>{" "}
        <span className="text-white/50">files queued to act on</span>
      </div>
      <button
        className="btn-primary"
        disabled={disabled || activeGroups === 0}
        onClick={() => onExecute("move")}
      >
        <Folder className="w-4 h-4" />
        Move duplicates to review folder
      </button>
      <button
        className="btn-danger"
        disabled={disabled || activeGroups === 0}
        onClick={() => onExecute("trash")}
      >
        <Trash2 className="w-4 h-4" />
        Send to trash
      </button>
    </div>
  );
}

function Stat({ label, value }: { label: string; value: number | string }) {
  return (
    <div className="panel-pad">
      <div className="label">{label}</div>
      <div className="text-2xl font-display font-semibold tabular-nums">{value}</div>
    </div>
  );
}

function Section({
  title,
  subtitle,
  children,
}: {
  title: string;
  subtitle: string;
  children: React.ReactNode;
}) {
  return (
    <div className="mb-6">
      <div className="flex items-baseline gap-3 mb-2">
        <h2 className="font-display font-semibold text-white">{title}</h2>
        <p className="text-xs text-white/40">{subtitle}</p>
      </div>
      <div className="space-y-2">{children}</div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Group card — pick keeper, mark skip
// ---------------------------------------------------------------------------

interface GroupCardProps {
  group: DuplicateGroup;
  currentKeeper: string;   // path
  onPickKeeper: (path: string) => void;
  skipped: boolean;
  onToggleSkip: () => void;
}

function GroupCard({
  group,
  currentKeeper,
  onPickKeeper,
  skipped,
  onToggleSkip,
}: GroupCardProps) {
  const files: FileInfo[] = [group.keep, ...group.duplicates];

  return (
    <div className={`panel-pad ${skipped ? "opacity-40" : ""}`}>
      <div className="flex items-center gap-2 mb-3">
        <TagBadge color={group.method === "md5" ? "green" : "cyan"}>
          {group.method === "md5" ? "exact" : "audio"}
        </TagBadge>
        <div className="flex-1 text-xs text-white/40">
          recoverable: {group.recoverable_mb.toFixed(1)} MB
        </div>
        <button
          onClick={onToggleSkip}
          className="text-xs text-white/50 hover:text-white"
        >
          {skipped ? "include" : "skip group"}
        </button>
      </div>

      <div className="space-y-1">
        {files.map((f) => (
          <FileRow
            key={f.path}
            file={f}
            isKeeper={f.path === currentKeeper}
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
        <ArrowRight className="w-4 h-4 text-white/30 flex-none" />
      )}
      <FileAudio className="w-3.5 h-3.5 text-white/40 flex-none" />
      <div className="flex-1 min-w-0">
        <div className="text-xs text-white/90 truncate">{file.filename}</div>
        <div className="text-[10px] text-white/40 font-mono truncate">{file.path}</div>
      </div>
      <div className="text-[11px] text-white/40 tabular-nums">
        {file.size_mb.toFixed(1)}M
      </div>
      {isKeeper && (
        <span className="text-[10px] uppercase tracking-wider text-accent-green font-semibold">
          keep
        </span>
      )}
    </button>
  );
}

// ---------------------------------------------------------------------------
// Apply user choices: rebuild the report so keeper/dup assignments match the UI
// ---------------------------------------------------------------------------

function applyUserChoices(
  report: DuplicateReport,
  keeperOverrides: Record<string, string>,
  skippedGroups: Set<string>,
): DuplicateReport {
  const rebuild = (g: DuplicateGroup): DuplicateGroup | null => {
    if (skippedGroups.has(g.key)) return null;
    const overridePath = keeperOverrides[g.key];
    if (!overridePath || overridePath === g.keep.path) return g;

    const all = [g.keep, ...g.duplicates];
    const newKeeper = all.find((f) => f.path === overridePath) ?? g.keep;
    const newDupes = all.filter((f) => f.path !== overridePath);
    return {
      ...g,
      keep: newKeeper,
      duplicates: newDupes,
      recoverable_mb: newDupes.reduce((s, f) => s + f.size_mb, 0),
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
