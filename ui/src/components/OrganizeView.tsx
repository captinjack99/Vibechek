/**
 * Plan and execute a genre-folder reorganization.
 *
 * Flow:
 *   1. Pick a source — either an analysis.json on disk, or the tracks
 *      currently loaded in the library.
 *   2. Tweak min-genre-size / use-subgenres / target-root.
 *   3. Click "Preview" — UI shows the move plan grouped by destination.
 *   4. Click "Execute" — actually moves the files.
 */

import { useMemo, useState } from "react";
import {
  FolderTree, FolderOpen, Play, Eye, AlertCircle, ArrowRight, FileJson,
  CheckCircle2, RotateCcw,
} from "lucide-react";
import { open as openDialog, save as saveDialog } from "@tauri-apps/plugin-dialog";

import {
  useConfigStore, useLibraryStore, useNotificationStore, useOperationStore, useUIStore,
} from "../stores";
import { rpc } from "../hooks/useSidecar";
import type { OrganizePlan, PlannedMove } from "../types";
import { TagBadge } from "./TagBadges";
import { ConfirmModal } from "./ConfirmModal";

type Source =
  | { kind: "in-memory" }
  | { kind: "file"; path: string }
  | null;

/** Result of a completed organize run, captured for the result panel. */
interface OrganizeResult {
  baseDir: string;
  planned: number;
  moved: number;
  errors: string[];
  /** path of the tag backup we created first, if any */
  backupPath: string | null;
  /** destination-folder → number of files moved into it */
  folderCounts: [string, number][];
}

export function OrganizeView() {
  const tracks = useLibraryStore((s) => s.tracks);

  const active = useOperationStore((s) => s.active);
  const begin = useOperationStore((s) => s.begin);
  const finish = useOperationStore((s) => s.finish);
  const fail = useOperationStore((s) => s.fail);
  const errorMsg = useOperationStore((s) => s.error);

  const plan = useOperationStore((s) => s.organizePlan);
  const setPlan = useOperationStore((s) => s.setOrganizePlan);

  const orgCfg = useConfigStore((s) => s.config.organization);
  const updateOrganization = useConfigStore((s) => s.updateOrganization);

  const notify = useNotificationStore((s) => s.notify);

  const [source, setSource] = useState<Source>(
    tracks.length > 0 ? { kind: "in-memory" } : null,
  );
  const [showConfirm, setShowConfirm] = useState(false);
  const [backupFirst, setBackupFirst] = useState(true);
  const [result, setResult] = useState<OrganizeResult | null>(null);

  const setViewMode = useUIStore((s) => s.setViewMode);

  const handlePickAnalysisFile = async () => {
    const path = await openDialog({
      directory: false,
      multiple: false,
      filters: [{ name: "Analysis JSON", extensions: ["json"] }],
    });
    if (typeof path === "string") setSource({ kind: "file", path });
  };

  const handlePickTargetRoot = async () => {
    const path = await openDialog({ directory: true, multiple: false });
    if (typeof path === "string") updateOrganization({ target_root: path });
  };

  const buildParams = () => {
    const base: Record<string, unknown> = {
      use_subgenres: orgCfg.use_subgenres,
      min_genre_size: orgCfg.min_genre_size,
      target_root: orgCfg.target_root,
    };
    if (source?.kind === "file") base.analysis_path = source.path;
    else if (source?.kind === "in-memory") base.analysis = { tracks };
    return base;
  };

  const handlePreview = async () => {
    if (!source) return;
    begin("organize");
    try {
      const result = await rpc<OrganizePlan>("plan_organization", buildParams());
      setPlan(result);
      finish();
    } catch (e) {
      fail(String(e));
    }
  };

  const handleExecuteClick = () => {
    if (!plan || plan.moves.length === 0) return;
    setShowConfirm(true);
  };

  const performExecute = async () => {
    setShowConfirm(false);
    if (!plan) return;

    // Optionally back up tags first so the user has a restore path
    let backupPath: string | null = null;
    if (backupFirst) {
      const out = await saveDialog({
        defaultPath: "tags_backup_pre_organize.json",
        filters: [{ name: "Tag backup (JSON)", extensions: ["json"] }],
      });
      if (typeof out !== "string") {
        // User cancelled the save dialog — abort the whole operation
        return;
      }
      backupPath = out;
      begin("backup");
      try {
        await rpc("backup_tags", { path: plan.base_dir, output_path: out });
        finish();
      } catch (e) {
        fail(String(e));
        return;
      }
    }

    // Capture the planned destinations before we mutate `plan`
    const capturedPlan = plan;

    begin("organize");
    try {
      const stats = await rpc<{ planned: number; moved: number; errors: string[] }>(
        "organize",
        { ...buildParams(), dry_run: false },
      );
      finish();

      // Build a destination-folder breakdown from the captured plan
      const folderCounts = new Map<string, number>();
      for (const move of capturedPlan.moves) {
        const folder = move.destination.replace(/[/\\][^/\\]+$/, "") || move.destination;
        // Show the folder relative to baseDir for readability
        const relative = folder.startsWith(capturedPlan.base_dir)
          ? folder.slice(capturedPlan.base_dir.length).replace(/^[/\\]+/, "") || "(root)"
          : folder;
        folderCounts.set(relative, (folderCounts.get(relative) ?? 0) + 1);
      }

      setResult({
        baseDir: capturedPlan.base_dir,
        planned: stats.planned,
        moved: stats.moved,
        errors: stats.errors,
        backupPath,
        folderCounts: [...folderCounts.entries()].sort((a, b) => b[1] - a[1]),
      });
      setPlan(null);

      // Brief notification too, in case the user navigated away mid-op
      notify(`Moved ${stats.moved} of ${stats.planned} files`, {
        kind: stats.errors.length > 0 ? "info" : "success",
      });
    } catch (e) {
      fail(String(e));
    }
  };

  const handleOrganizeAnother = () => {
    setResult(null);
    setSource(tracks.length > 0 ? { kind: "in-memory" } : null);
  };

  // When we have a completed-run result, replace the whole view with a
  // recap so the user actually reads what changed.
  if (result) {
    return (
      <OrganizeResultPanel
        result={result}
        onViewLibrary={() => setViewMode("library")}
        onAnother={handleOrganizeAnother}
      />
    );
  }

  return (
    <div className="h-full flex flex-col">
      {/* Header */}
      <div className="flex items-center justify-between px-4 py-3 border-b border-white/5">
        <div>
          <h1 className="font-display font-semibold text-white">Organize</h1>
          <p className="text-xs text-white/40 truncate max-w-md">
            {plan ? plan.base_dir : "no plan yet"}
          </p>
        </div>
        <div className="flex items-center gap-2">
          {plan && (
            <button
              className="btn-primary"
              onClick={handleExecuteClick}
              disabled={active !== null || plan.moves.length === 0}
            >
              <Play className="w-4 h-4" />
              Execute ({plan.moves.length} moves)
            </button>
          )}
        </div>
      </div>

      {errorMsg && (
        <div className="m-4 panel-pad text-sm text-accent-red flex gap-2">
          <AlertCircle className="w-4 h-4 flex-none mt-0.5" />
          <div className="break-words">{errorMsg}</div>
        </div>
      )}

      <div className="flex-1 overflow-auto px-4 py-4">
        {/* Source picker */}
        <Section title="Source" subtitle="Where do the ML genre labels come from?">
          <SourcePicker
            source={source}
            inMemoryAvailable={tracks.length}
            onUseInMemory={() => setSource({ kind: "in-memory" })}
            onPickFile={handlePickAnalysisFile}
          />
        </Section>

        {/* Rules */}
        <Section title="Rules" subtitle="How the folder tree looks">
          <div className="grid grid-cols-2 gap-4">
            <div>
              <div className="label">Min tracks per genre folder</div>
              <input
                type="number"
                min={1}
                className="input w-24"
                value={orgCfg.min_genre_size}
                onChange={(e) =>
                  updateOrganization({ min_genre_size: Number(e.target.value) || 1 })
                }
              />
              <div className="text-xs text-white/40 mt-1">
                Rarer genres get bucketed into <code>Other/</code>.
              </div>
            </div>
            <div>
              <div className="label">Subgenre subfolders</div>
              <label className="flex items-center gap-2 text-sm text-white/80 cursor-pointer">
                <input
                  type="checkbox"
                  checked={orgCfg.use_subgenres}
                  onChange={(e) => updateOrganization({ use_subgenres: e.target.checked })}
                  className="accent-accent"
                />
                Use them
              </label>
              <div className="text-xs text-white/40 mt-1">
                On: <code>House/Deep House/track.mp3</code>. Off: flat <code>House/</code>.
              </div>
            </div>
          </div>

          <div className="mt-4">
            <div className="label">Target root (override)</div>
            <div className="flex gap-2">
              <input
                type="text"
                className="input flex-1 font-mono text-xs"
                value={orgCfg.target_root ?? ""}
                placeholder="(default: same parent as analyzed tracks)"
                onChange={(e) => updateOrganization({ target_root: e.target.value })}
              />
              <button className="btn-ghost" onClick={handlePickTargetRoot}>
                <FolderOpen className="w-4 h-4" />
              </button>
            </div>
          </div>

          <button
            className="btn-primary mt-4"
            onClick={handlePreview}
            disabled={!source || active !== null}
          >
            <Eye className="w-4 h-4" />
            Preview plan
          </button>
        </Section>

        {plan && <PlanPreview plan={plan} />}
      </div>

      <ConfirmModal
        open={showConfirm && !!plan}
        variant="danger"
        icon={Play}
        title={plan ? `Move ${plan.moves.length} files?` : "Move files?"}
        message={
          plan && (
            <div className="space-y-2">
              <p>
                Vibechek will move <strong>{plan.moves.length}</strong> files into
                folders under{" "}
                <code className="font-mono text-xs text-white/90">{plan.base_dir}</code>.
              </p>
              <p className="text-xs text-white/60">
                First few moves:
              </p>
              <ul className="text-[11px] font-mono text-white/50 space-y-0.5 max-h-32 overflow-auto">
                {plan.moves.slice(0, 5).map((m) => (
                  <li key={m.source}>
                    {m.source.split(/[/\\]/).pop()} → {m.destination.replace(plan.base_dir, "").replace(/^[/\\]+/, "")}
                  </li>
                ))}
                {plan.moves.length > 5 && <li>... and {plan.moves.length - 5} more</li>}
              </ul>
              <p className="text-accent-yellow">There is no automatic undo.</p>
            </div>
          )
        }
        extra={
          <label className="flex items-start gap-2 cursor-pointer text-sm text-white/80">
            <input
              type="checkbox"
              checked={backupFirst}
              onChange={(e) => setBackupFirst(e.target.checked)}
              className="mt-0.5 accent-accent"
            />
            <div>
              <div>Back up all tags first <span className="text-xs text-accent-green">(recommended)</span></div>
              <div className="text-xs text-white/50">
                You'll be prompted for a save location. The backup lets you restore tags later if anything looks wrong.
              </div>
            </div>
          </label>
        }
        confirmLabel="Yes, move files"
        cancelLabel="Cancel"
        onConfirm={performExecute}
        onCancel={() => setShowConfirm(false)}
      />
    </div>
  );
}

// ---------------------------------------------------------------------------

function SourcePicker({
  source,
  inMemoryAvailable,
  onUseInMemory,
  onPickFile,
}: {
  source: Source;
  inMemoryAvailable: number;
  onUseInMemory: () => void;
  onPickFile: () => void;
}) {
  return (
    <div className="grid grid-cols-2 gap-3">
      <button
        onClick={onUseInMemory}
        disabled={inMemoryAvailable === 0}
        className={`panel-pad text-left ${
          source?.kind === "in-memory" ? "border-accent/60" : "hover:bg-white/[0.03]"
        } disabled:opacity-50 disabled:cursor-not-allowed`}
      >
        <div className="flex items-center gap-2 text-sm text-white/90">
          <FolderTree className="w-4 h-4 text-accent" />
          Currently loaded library
        </div>
        <div className="text-xs text-white/50 mt-1">
          {inMemoryAvailable > 0
            ? `${inMemoryAvailable} tracks in memory`
            : "Run analyze in the Library tab first."}
        </div>
      </button>

      <button
        onClick={onPickFile}
        className={`panel-pad text-left ${
          source?.kind === "file" ? "border-accent/60" : "hover:bg-white/[0.03]"
        }`}
      >
        <div className="flex items-center gap-2 text-sm text-white/90">
          <FileJson className="w-4 h-4 text-accent" />
          Load analysis.json
        </div>
        <div className="text-xs text-white/50 mt-1 truncate">
          {source?.kind === "file" ? source.path : "Pick a file saved by a previous analyze."}
        </div>
      </button>
    </div>
  );
}

// ---------------------------------------------------------------------------

function PlanPreview({ plan }: { plan: OrganizePlan }) {
  // Group moves by destination folder for a cleaner display
  const byDest: Record<string, PlannedMove[]> = {};
  for (const m of plan.moves) {
    const folder = m.destination.replace(/[/\\][^/\\]+$/, "") || m.destination;
    byDest[folder] = byDest[folder] ?? [];
    byDest[folder].push(m);
  }

  const folders = Object.entries(byDest).sort(([a], [b]) => a.localeCompare(b));
  const hasMoves = plan.moves.length > 0;

  return (
    <Section title="Plan" subtitle={`${plan.moves.length} moves planned`}>
      {!hasMoves && (
        <div className="text-sm text-white/50">
          No moves needed — everything is already in the right place.
        </div>
      )}

      {plan.small_genres.length > 0 && (
        <div className="mb-3 text-xs text-white/50">
          Small genres going to <code>Other/</code>:{" "}
          {plan.small_genres.map((g) => (
            <TagBadge key={g} color="neutral">
              {g}
            </TagBadge>
          ))}
        </div>
      )}

      {plan.errors.length > 0 && (
        <div className="mb-3 panel-pad text-xs text-accent-yellow">
          {plan.errors.length} planning issues (first 5 shown):
          <ul className="mt-1 space-y-0.5">
            {plan.errors.slice(0, 5).map((e, i) => (
              <li key={i} className="font-mono">{e}</li>
            ))}
          </ul>
        </div>
      )}

      <div className="space-y-3 max-h-[480px] overflow-auto">
        {folders.map(([folder, moves]) => (
          <FolderGroup key={folder} folder={folder} moves={moves} baseDir={plan.base_dir} />
        ))}
      </div>
    </Section>
  );
}

function FolderGroup({
  folder,
  moves,
  baseDir,
}: {
  folder: string;
  moves: PlannedMove[];
  baseDir: string;
}) {
  const relative = folder.startsWith(baseDir) ? folder.slice(baseDir.length).replace(/^[/\\]+/, "") : folder;
  return (
    <div className="panel-pad">
      <div className="flex items-center gap-2 mb-2">
        <FolderTree className="w-4 h-4 text-accent" />
        <div className="text-sm font-medium text-white">
          {relative || "(root)"}
        </div>
        <span className="text-xs text-white/40 ml-auto tabular-nums">{moves.length}</span>
      </div>
      <div className="space-y-1">
        {moves.slice(0, 8).map((m) => (
          <div key={m.source} className="flex items-center gap-2 text-xs text-white/60">
            <ArrowRight className="w-3 h-3 text-white/30 flex-none" />
            <span className="truncate font-mono">{m.source.split(/[/\\]/).pop()}</span>
          </div>
        ))}
        {moves.length > 8 && (
          <div className="text-[11px] text-white/40 pl-5">
            and {moves.length - 8} more...
          </div>
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------

function Section({
  title,
  subtitle,
  children,
}: {
  title: string;
  subtitle?: string;
  children: React.ReactNode;
}) {
  return (
    <div className="mb-6">
      <div className="flex items-baseline gap-3 mb-3">
        <h2 className="font-display font-semibold text-lg">{title}</h2>
        {subtitle && <span className="text-xs text-white/40">{subtitle}</span>}
      </div>
      <div className="panel-pad space-y-3">{children}</div>
    </div>
  );
}


/**
 * Result panel — shown after a successful organize. The user just performed a
 * destructive action across (potentially) thousands of files; they deserve a
 * real "here's what happened" screen, not just a toast that disappears.
 */
function OrganizeResultPanel({
  result,
  onViewLibrary,
  onAnother,
}: {
  result: OrganizeResult;
  onViewLibrary: () => void;
  onAnother: () => void;
}) {
  const success = result.errors.length === 0;
  const topFolders = result.folderCounts.slice(0, 10);
  const moreFolders = result.folderCounts.length - topFolders.length;

  return (
    <div className="h-full overflow-auto">
      <div className="px-6 py-8 max-w-3xl mx-auto space-y-5">
        {/* Headline */}
        <div className="flex items-start gap-4">
          {success ? (
            <CheckCircle2 className="w-12 h-12 text-accent-green flex-none" />
          ) : (
            <AlertCircle className="w-12 h-12 text-accent-yellow flex-none" />
          )}
          <div className="flex-1">
            <h1 className="font-display font-semibold text-2xl text-white">
              {success ? "Library organized" : "Mostly done"}
            </h1>
            <p className="text-sm text-white/60 mt-1">
              Moved <strong className="text-white">{result.moved.toLocaleString()}</strong>{" "}
              of <strong className="text-white">{result.planned.toLocaleString()}</strong> files
              into <strong className="text-white">{result.folderCounts.length}</strong> folders.
            </p>
            <p className="text-xs text-white/40 font-mono mt-1 break-all">{result.baseDir}</p>
          </div>
        </div>

        {/* Summary stats */}
        <div className="grid grid-cols-3 gap-3">
          <Stat label="Moved" value={result.moved.toLocaleString()} accent="green" />
          <Stat
            label="Errors"
            value={result.errors.length.toLocaleString()}
            accent={result.errors.length > 0 ? "yellow" : "neutral"}
          />
          <Stat label="Destinations" value={result.folderCounts.length.toLocaleString()} accent="neutral" />
        </div>

        {/* Folder breakdown */}
        <div className="panel-pad">
          <div className="flex items-center gap-2 mb-3">
            <FolderTree className="w-4 h-4 text-accent" />
            <h2 className="font-medium text-white">Where files landed</h2>
          </div>
          <div className="space-y-1">
            {topFolders.map(([folder, count]) => {
              const pct = (count / Math.max(result.moved, 1)) * 100;
              return (
                <div key={folder} className="flex items-center gap-3 text-sm">
                  <div className="flex-1 min-w-0 truncate font-mono text-xs text-white/80">
                    {folder}
                  </div>
                  <div className="w-32 h-1.5 bg-white/5 rounded-full overflow-hidden">
                    <div className="h-full bg-accent" style={{ width: `${pct}%` }} />
                  </div>
                  <div className="text-xs text-white/50 tabular-nums w-12 text-right">
                    {count}
                  </div>
                </div>
              );
            })}
            {moreFolders > 0 && (
              <div className="text-xs text-white/40 pt-2">
                ... and {moreFolders} more folders
              </div>
            )}
          </div>
        </div>

        {/* Tag backup notice */}
        {result.backupPath && (
          <div className="panel-pad bg-accent-green/5 border-accent-green/30 text-sm">
            <div className="flex items-start gap-2">
              <CheckCircle2 className="w-4 h-4 text-accent-green flex-none mt-0.5" />
              <div>
                <div className="text-accent-green">Tag backup saved</div>
                <div className="text-xs text-white/60 mt-1 font-mono break-all">
                  {result.backupPath}
                </div>
                <div className="text-xs text-white/50 mt-1">
                  If anything looks wrong, you can restore tags from this file in the Tags tab.
                </div>
              </div>
            </div>
          </div>
        )}

        {/* Errors (collapsible) */}
        {result.errors.length > 0 && (
          <details className="panel-pad bg-accent-yellow/5 border-accent-yellow/30">
            <summary className="cursor-pointer text-sm font-medium text-accent-yellow flex items-center gap-2">
              <AlertCircle className="w-4 h-4" />
              {result.errors.length} files couldn't be moved
            </summary>
            <ul className="mt-3 space-y-1 max-h-64 overflow-auto text-xs font-mono text-white/60">
              {result.errors.slice(0, 100).map((err, i) => (
                <li key={i} className="break-all">{err}</li>
              ))}
              {result.errors.length > 100 && (
                <li className="text-white/40">... and {result.errors.length - 100} more</li>
              )}
            </ul>
          </details>
        )}

        {/* Actions */}
        <div className="flex items-center justify-end gap-2 pt-4">
          <button className="btn-ghost" onClick={onAnother}>
            <RotateCcw className="w-4 h-4" />
            Organize another folder
          </button>
          <button className="btn-primary" onClick={onViewLibrary}>
            <Eye className="w-4 h-4" />
            View library
          </button>
        </div>
      </div>
    </div>
  );
}

function Stat({
  label,
  value,
  accent,
}: {
  label: string;
  value: string;
  accent: "green" | "yellow" | "neutral";
}) {
  const colorClass = {
    green: "text-accent-green",
    yellow: "text-accent-yellow",
    neutral: "text-white",
  }[accent];
  return (
    <div className="panel-pad">
      <div className="label">{label}</div>
      <div className={`text-2xl font-display font-semibold tabular-nums ${colorClass}`}>
        {value}
      </div>
    </div>
  );
}
