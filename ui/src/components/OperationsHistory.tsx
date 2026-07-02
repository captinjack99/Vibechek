/**
 * "Recent operations" modal — the durable undo surface for organize / dedupe.
 *
 * Lists the append-only journals the sidecar writes for every destructive file
 * operation (see vibechek/journal.py) and lets the user revert a move-based
 * one (organize, dedupe move-to-review). Trash operations are shown for the
 * record but can't be auto-restored (OS recycle bin).
 *
 * Opened via `useUIStore.historyOpen`. Reads journals on open; refreshes after
 * a successful revert.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { motion } from "framer-motion";
import {
  X, Undo2, FolderTree, Copy, Trash2, Loader2, AlertCircle, History,
} from "lucide-react";

import { useUIStore, useOperationStore, useNotificationStore, useLibraryStore } from "../stores";
import { listJournals, revertJournal } from "../api/rpc";
import type { JournalSummary } from "../api/methods";
import { isCancellation } from "../hooks/useSidecar";

function relativeTime(epochSeconds: number | null): string {
  if (!epochSeconds) return "";
  const secondsAgo = Math.max(0, Date.now() / 1000 - epochSeconds);
  if (secondsAgo < 60) return "just now";
  const m = Math.floor(secondsAgo / 60);
  if (m < 60) return m === 1 ? "1 min ago" : `${m} mins ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return h === 1 ? "1 hour ago" : `${h} hours ago`;
  const d = Math.floor(h / 24);
  return d === 1 ? "yesterday" : `${d} days ago`;
}

const KIND_META: Record<string, { label: string; icon: typeof FolderTree }> = {
  organize: { label: "Organize", icon: FolderTree },
  dedupe_move: { label: "Duplicates → review folder", icon: Copy },
  dedupe_trash: { label: "Duplicates → trash", icon: Trash2 },
};

export function OperationsHistory() {
  const open = useUIStore((s) => s.historyOpen);
  const setOpen = useUIStore((s) => s.setHistoryOpen);
  const active = useOperationStore((s) => s.active);
  const begin = useOperationStore((s) => s.begin);
  const finish = useOperationStore((s) => s.finish);
  const notify = useNotificationStore((s) => s.notify);

  const [journals, setJournals] = useState<JournalSummary[]>([]);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  // Path of the journal currently being reverted (disables its button + spins).
  const [revertingPath, setRevertingPath] = useState<string | null>(null);

  const isMounted = useRef(true);
  useEffect(() => {
    isMounted.current = true;
    return () => {
      isMounted.current = false;
    };
  }, []);

  const refresh = useCallback(async () => {
    setLoading(true);
    setLoadError(null);
    try {
      const res = await listJournals({});
      if (isMounted.current) setJournals(res.journals);
    } catch (e) {
      if (isMounted.current) {
        setLoadError(
          typeof e === "object" && e !== null && "message" in e
            ? String((e as { message: unknown }).message)
            : String(e),
        );
      }
    } finally {
      if (isMounted.current) setLoading(false);
    }
  }, []);

  // Load whenever the modal opens.
  useEffect(() => {
    if (open) void refresh();
  }, [open, refresh]);

  // Esc closes.
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, setOpen]);

  if (!open) return null;

  const handleRevert = async (j: JournalSummary) => {
    // Block a revert while another long op (analyze/organize/...) is running —
    // the sidecar serializes long ops and would reject it anyway, but failing
    // fast with a clear message is better UX.
    if (active !== null) {
      notify("Finish or cancel the current operation before reverting.", { kind: "info" });
      return;
    }
    setRevertingPath(j.path);
    // Register with the operation store so the global progress overlay shows
    // the revert AND the server-side long-op lock is mirrored client-side.
    const opId = begin("revert");
    try {
      const summary = await revertJournal({ journal_path: j.path }, opId);
      finish();
      // Follow the files back in the in-memory library — exactly like
      // organize's post-execute updateTrackPaths, in reverse. Without this,
      // every post-undo Preview / "Apply ML tags" / re-organize targeted the
      // now-nonexistent destination paths until a manual re-scan.
      if (summary.reverted_pairs?.length) {
        const pathMap: Record<string, string> = {};
        for (const [current, restored] of summary.reverted_pairs) {
          pathMap[current] = restored;
        }
        useLibraryStore.getState().updateTrackPaths(pathMap);
      }
      const parts = [`Restored ${summary.reverted}`];
      if (summary.skipped) parts.push(`skipped ${summary.skipped}`);
      if (summary.errors) parts.push(`${summary.errors} error${summary.errors === 1 ? "" : "s"}`);
      notify(`Undo complete — ${parts.join(", ")}`, {
        kind: summary.errors > 0 ? "info" : "success",
        detail:
          summary.trashed_not_reverted > 0
            ? `${summary.trashed_not_reverted} trashed file(s) can't be auto-restored — recover them from your OS recycle bin.`
            : undefined,
      });
      await refresh();
    } catch (e) {
      // Always clear op state. We use a soft info notification (not the sticky
      // red error toast) for an undo hiccup; a user-cancel is silent.
      finish();
      if (!isCancellation(e)) {
        notify(
          typeof e === "object" && e !== null && "message" in e
            ? `Undo failed: ${String((e as { message: unknown }).message)}`
            : `Undo failed: ${String(e)}`,
          { kind: "info" },
        );
      }
    } finally {
      if (isMounted.current) setRevertingPath(null);
    }
  };

  return (
    <motion.div
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      exit={{ opacity: 0 }}
      className="fixed inset-0 z-[60] bg-black/60 flex items-center justify-center px-4"
      onClick={() => setOpen(false)}
      role="dialog"
      aria-modal="true"
      aria-label="Recent operations"
    >
      <motion.div
        initial={{ scale: 0.96, y: 10 }}
        animate={{ scale: 1, y: 0 }}
        className="panel max-w-2xl w-full max-h-[80vh] flex flex-col"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-start gap-3 px-5 py-4 border-b border-white/5">
          <History className="w-6 h-6 flex-none mt-0.5 text-accent" />
          <div className="flex-1">
            <h2 className="font-display font-semibold text-lg">Recent operations</h2>
            <p className="text-xs text-white/50 mt-0.5">
              Undo an organize or move-to-review. Trashed files must be restored
              from your OS recycle bin.
            </p>
          </div>
          <button onClick={() => setOpen(false)} className="text-white/40 hover:text-white -m-1 p-1" aria-label="Close">
            <X className="w-5 h-5" />
          </button>
        </div>

        <div className="px-5 py-4 overflow-auto flex-1 space-y-2">
          {loading && (
            <div className="flex items-center justify-center gap-2 py-10 text-white/50 text-sm">
              <Loader2 className="w-4 h-4 animate-spin" /> Loading…
            </div>
          )}

          {!loading && loadError && (
            <div className="flex items-center gap-2 text-sm text-accent-red py-6">
              <AlertCircle className="w-4 h-4 flex-none" />
              <span className="break-words">{loadError}</span>
            </div>
          )}

          {!loading && !loadError && journals.length === 0 && (
            <div className="text-center py-10 text-white/40 text-sm">
              No operations yet. Organize or de-duplicate your library and the
              undo history shows up here.
            </div>
          )}

          {!loading && !loadError && journals.map((j) => {
            const meta = KIND_META[j.kind] ?? { label: j.kind, icon: FolderTree };
            const Icon = meta.icon;
            const revertible = j.move_count > 0; // trash-only journals can't revert
            const isReverting = revertingPath === j.path;
            return (
              <div
                key={j.path}
                className="flex items-center gap-3 panel-pad bg-white/[0.02] border border-white/5"
              >
                <Icon className="w-5 h-5 flex-none text-white/60" />
                <div className="flex-1 min-w-0">
                  <div className="text-sm text-white font-medium">{meta.label}</div>
                  <div className="text-xs text-white/50 truncate">
                    {relativeTime(j.started_at)}
                    {j.move_count > 0 && ` • ${j.move_count} moved`}
                    {j.trash_count > 0 && ` • ${j.trash_count} trashed (not revertible)`}
                    {j.root && ` • ${j.root}`}
                  </div>
                </div>
                {revertible ? (
                  <button
                    className="btn-ghost flex-none"
                    onClick={() => handleRevert(j)}
                    disabled={isReverting || active !== null}
                    title="Move these files back to their original locations"
                  >
                    {isReverting ? (
                      <Loader2 className="w-4 h-4 animate-spin" />
                    ) : (
                      <Undo2 className="w-4 h-4" />
                    )}
                    Undo
                  </button>
                ) : (
                  <span className="text-[11px] text-white/30 flex-none px-2" title="Trashed files go to the OS recycle bin; restore them there.">
                    recycle bin
                  </span>
                )}
              </div>
            );
          })}
        </div>
      </motion.div>
    </motion.div>
  );
}
