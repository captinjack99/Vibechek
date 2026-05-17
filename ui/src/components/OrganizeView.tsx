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

import { useState } from "react";
import {
  FolderTree, FolderOpen, Play, Eye, AlertCircle, ArrowRight, FileJson,
} from "lucide-react";
import { open as openDialog, save as saveDialog } from "@tauri-apps/plugin-dialog";

import { useConfigStore, useLibraryStore, useOperationStore } from "../stores";
import { rpc } from "../hooks/useSidecar";
import type { OrganizePlan, PlannedMove } from "../types";
import { TagBadge } from "./TagBadges";
import { ConfirmModal } from "./ConfirmModal";

type Source =
  | { kind: "in-memory" }
  | { kind: "file"; path: string }
  | null;

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

  const [source, setSource] = useState<Source>(
    tracks.length > 0 ? { kind: "in-memory" } : null,
  );
  const [showConfirm, setShowConfirm] = useState(false);
  const [backupFirst, setBackupFirst] = useState(true);

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
    if (backupFirst) {
      const out = await saveDialog({
        defaultPath: "tags_backup_pre_organize.json",
        filters: [{ name: "Tag backup (JSON)", extensions: ["json"] }],
      });
      if (typeof out !== "string") {
        // User cancelled the save dialog — abort the whole operation
        return;
      }
      begin("backup");
      try {
        await rpc("backup_tags", { path: plan.base_dir, output_path: out });
        finish();
      } catch (e) {
        fail(String(e));
        return;
      }
    }

    begin("organize");
    try {
      const stats = await rpc<{ planned: number; moved: number; errors: string[] }>(
        "organize",
        { ...buildParams(), dry_run: false },
      );
      finish();
      window.alert(
        `Moved ${stats.moved} of ${stats.planned} files.\n` +
        `Errors: ${stats.errors.length}` +
        (backupFirst ? `\n\nTag backup saved.` : ""),
      );
      setPlan(null);
    } catch (e) {
      fail(String(e));
    }
  };

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
