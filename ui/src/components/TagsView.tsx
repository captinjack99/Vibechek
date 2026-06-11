/**
 * Tag backup & restore view.
 *
 * The safest, most reassuring thing Vibechek does — exposed prominently so
 * users see it before they let the app modify files. Backup captures every
 * ID3/Vorbis/MP4 tag (including Rekordbox cue points and beat grids stored
 * in MP3 GEOB/PRIV frames) into a single JSON file.
 *
 * Restore replays the backup verbatim — any user-applied tags between the
 * backup and the restore are overwritten.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import {
  Archive, FolderOpen, Save, Upload, AlertCircle, CheckCircle2, Clock,
  RotateCcw, X, History, Shuffle, ChevronDown, ChevronRight,
} from "lucide-react";
import { open as openDialog, save as saveDialog } from "@tauri-apps/plugin-dialog";

import { useLibraryStore, useNotificationStore, useOperationStore } from "../stores";
import { rpc, useSidecarProgress } from "../hooks/useSidecar";
import { ConfirmModal } from "./ConfirmModal";
import type { AnalysisReport, BackupHistory, BackupRecord } from "../types";

/** Result payload from `restore_tags_with_remap`. */
interface RemapRestoreStats {
  total: number;
  restored: number;
  skipped_missing: number;
  skipped_size_mismatch: number;
  matched_exact: number;
  matched_filename_size: number;
  matched_filename: number;
  errors: string[];
  matches: Array<{
    original: string;
    matched: string | null;
    strategy: string | null;
    error?: string | null;
    substrategy?: string;
  }>;
}

/** Days after which we suggest re-running the backup. */
const STALE_AFTER_DAYS = 30;

/** After this long with no `setProgress`, surface a "seems stuck" banner. */
const PROGRESS_STALL_MS = 60_000;

/** Heuristic file-vs-folder check. Tauri has no fs.stat API in our deps. */
const AUDIO_EXTENSIONS = [
  ".mp3", ".m4a", ".aac", ".flac", ".wav", ".ogg", ".opus", ".aif", ".aiff",
  ".alac", ".wma", ".dsf", ".dff", ".ape",
];
function looksLikeAudioFile(path: string): boolean {
  const lower = path.toLowerCase();
  return AUDIO_EXTENSIONS.some((ext) => lower.endsWith(ext));
}

function relativeTime(epochSeconds: number): string {
  if (!epochSeconds) return "never";
  const seconds = Math.max(0, Date.now() / 1000 - epochSeconds);
  if (seconds < 60) return "just now";
  const m = Math.floor(seconds / 60);
  if (m < 60) return m === 1 ? "1 minute ago" : `${m} minutes ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return h === 1 ? "1 hour ago" : `${h} hours ago`;
  const d = Math.floor(h / 24);
  if (d === 1) return "yesterday";
  if (d < 30) return `${d} days ago`;
  const mo = Math.floor(d / 30);
  return mo === 1 ? "1 month ago" : `${mo} months ago`;
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}

export function TagsView() {
  const libraryPath = useLibraryStore((s) => s.libraryPath);
  const setTracks = useLibraryStore((s) => s.setTracks);
  const active = useOperationStore((s) => s.active);
  const begin = useOperationStore((s) => s.begin);
  const finish = useOperationStore((s) => s.finish);
  const fail = useOperationStore((s) => s.fail);
  const errorMsg = useOperationStore((s) => s.error);
  const notify = useNotificationStore((s) => s.notify);

  const [pathToBackup, setPathToBackup] = useState<string | null>(libraryPath);
  const [lastBackup, setLastBackup] = useState<{ file: string; count: number } | null>(null);
  const [restoreCandidate, setRestoreCandidate] = useState<string | null>(null);
  const [confirmRestore, setConfirmRestore] = useState(false);
  const [history, setHistory] = useState<BackupRecord[]>([]);

  // Forget-backup confirmation. Stash the record so the modal
  // can show its path before we ask the user to confirm.
  const [forgetCandidate, setForgetCandidate] = useState<BackupRecord | null>(null);

  // Progress-stall detector. We don't render a progress bar in
  // TagsView, but we do listen to progress events and reset a timer; if no
  // event arrives for PROGRESS_STALL_MS and an op is still active, we show
  // a "seems stuck" banner with a Cancel hint.
  const [progressStalled, setProgressStalled] = useState(false);
  const stallTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const clearStallTimer = useCallback(() => {
    if (stallTimerRef.current !== null) {
      clearTimeout(stallTimerRef.current);
      stallTimerRef.current = null;
    }
  }, []);

  const armStallTimer = useCallback(() => {
    clearStallTimer();
    setProgressStalled(false);
    stallTimerRef.current = setTimeout(() => setProgressStalled(true), PROGRESS_STALL_MS);
  }, [clearStallTimer]);

  // Reset stall state whenever the operation changes.
  useEffect(() => {
    if (active === "backup") {
      armStallTimer();
    } else {
      clearStallTimer();
      setProgressStalled(false);
    }
    return () => clearStallTimer();
  }, [active, armStallTimer, clearStallTimer]);

  // Re-arm the stall timer every time a progress event arrives during a
  // backup/restore op. (`useSidecarProgress` fires regardless of which view
  // is active — we just ignore it when we're not in a backup-class op.)
  useSidecarProgress((_e) => {
    if (active === "backup") {
      armStallTimer();
    }
  });

  // Remap restore dialog state (restore against a moved library)
  const [remapDialogOpen, setRemapDialogOpen] = useState(false);
  const [remapBackupPath, setRemapBackupPath] = useState<string>("");
  const [remapLibraryRoot, setRemapLibraryRoot] = useState<string>(libraryPath ?? "");
  const [remapResult, setRemapResult] = useState<RemapRestoreStats | null>(null);
  const [remapValidationError, setRemapValidationError] = useState<string | null>(null);

  const refreshHistory = useCallback(async () => {
    try {
      const h = await rpc<BackupHistory>("backup_history");
      setHistory(h.records ?? []);
    } catch {
      setHistory([]);
    }
  }, []);

  useEffect(() => {
    refreshHistory();
  }, [refreshHistory]);

  // Find the most recent backup for the current library, if any
  const lastBackupForCurrent = history.find(
    (r) => r.library_path === (pathToBackup ?? libraryPath ?? "") && !r.missing,
  );
  const lastBackupAgeDays = lastBackupForCurrent
    ? (Date.now() / 1000 - lastBackupForCurrent.created_at) / (24 * 3600)
    : null;
  const isStale = lastBackupAgeDays !== null && lastBackupAgeDays > STALE_AFTER_DAYS;

  const handlePickFolder = async () => {
    const p = await openDialog({ directory: true, multiple: false });
    if (typeof p === "string") setPathToBackup(p);
  };

  const handleBackup = async () => {
    if (!pathToBackup) return;
    const outFile = await saveDialog({
      defaultPath: "tags_backup.json",
      filters: [{ name: "Tag backup (JSON)", extensions: ["json"] }],
    });
    if (typeof outFile !== "string") return;

    begin("backup");
    armStallTimer();
    try {
      const stats = await rpc<{ total: number; backed_up: number; errors: string[] }>(
        "backup_tags",
        { path: pathToBackup, output_path: outFile },
      );
      finish();
      setLastBackup({ file: outFile, count: stats.backed_up });
      await refreshHistory();
    } catch (e) {
      fail(e);
    } finally {
      clearStallTimer();
      setProgressStalled(false);
    }
  };

  const handleStartRestore = async () => {
    const path = await openDialog({
      directory: false,
      multiple: false,
      filters: [{ name: "Tag backup (JSON)", extensions: ["json"] }],
    });
    if (typeof path !== "string") return;
    setRestoreCandidate(path);
    setConfirmRestore(true);
  };

  const handleRestoreFromHistory = (record: BackupRecord) => {
    setRestoreCandidate(record.backup_path);
    setConfirmRestore(true);
  };

  // Replaced the silent fire-and-forget X-button with a real
  // confirm. The actual call now happens via the ConfirmModal's onConfirm
  // (handleConfirmForget below).
  const handleAskForgetBackup = (record: BackupRecord) => {
    setForgetCandidate(record);
  };

  const handleConfirmForget = async () => {
    if (!forgetCandidate) return;
    const target = forgetCandidate;
    setForgetCandidate(null);
    try {
      await rpc("forget_backup", { backup_path: target.backup_path });
    } catch (e) {
      // Surface forget errors instead of swallowing them in a `finally`
      // (the original was `try { ... } finally { refresh }`).
      fail(e);
    } finally {
      await refreshHistory();
    }
  };

  // After a successful restore, the on-disk tags no longer match
  // any in-memory analysis state. Re-scan the library so the Library tab
  // (and the next Apply) sees the freshly-restored values. If the library
  // path isn't known (restoring from a backup of a different library), just
  // notify and rely on the user to reopen the right folder.
  const refreshLibraryAfterRestore = useCallback(
    async (restoredFor: string | null) => {
      const target = restoredFor ?? libraryPath;
      if (!target) {
        notify(
          "Library view may be out of date — open the restored folder to refresh.",
          { kind: "info" },
        );
        return;
      }
      try {
        // `scan_only` is the same RPC the Library tab uses for its initial
        // load; it returns the same `AnalysisReport` shape.
        const report = await rpc<AnalysisReport>("scan_only", { path: target });
        setTracks(report.tracks);
      } catch {
        // Non-fatal — just warn the user the in-memory view is now stale.
        notify(
          "Couldn't refresh library view automatically — reopen the folder.",
          { kind: "info" },
        );
      }
    },
    [libraryPath, setTracks, notify],
  );

  const handleConfirmRestore = async () => {
    if (!restoreCandidate) return;
    setConfirmRestore(false);
    // Look up the library_path the backup was originally for, so we can
    // re-scan the right folder afterwards. Fall back to the
    // currently-open library when unknown.
    const record = history.find((r) => r.backup_path === restoreCandidate);
    const restoredFor = record?.library_path ?? null;
    begin("backup"); // (operation kind: restore is a backup-class op)
    try {
      const stats = await rpc<{
        total: number; restored: number; skipped_missing: number; errors: string[];
      }>("restore_tags", { backup_path: restoreCandidate });
      finish();
      const detailLines: string[] = [];
      if (stats.skipped_missing > 0) {
        detailLines.push(`Skipped (missing on disk): ${stats.skipped_missing}`);
      }
      if (stats.errors.length > 0) {
        detailLines.push(`Errors: ${stats.errors.length}`);
      }
      notify(`Restored ${stats.restored} of ${stats.total} files`, {
        detail: detailLines.length > 0 ? detailLines.join("\n") : undefined,
        kind: stats.errors.length > 0 ? "info" : "success",
      });
      await refreshLibraryAfterRestore(restoredFor);
    } catch (e) {
      fail(e);
    }
  };

  // --- Remap restore: pick backup + library root, then run the remap RPC. ---

  const handleOpenRemapDialog = () => {
    setRemapResult(null);
    setRemapBackupPath("");
    setRemapLibraryRoot(libraryPath ?? "");
    setRemapValidationError(null);
    setRemapDialogOpen(true);
  };

  const handlePickRemapBackup = async () => {
    const p = await openDialog({
      directory: false,
      multiple: false,
      filters: [{ name: "Tag backup (JSON)", extensions: ["json"] }],
    });
    if (typeof p === "string") setRemapBackupPath(p);
  };

  const handlePickRemapLibrary = async () => {
    const p = await openDialog({ directory: true, multiple: false });
    if (typeof p === "string") {
      setRemapLibraryRoot(p);
      setRemapValidationError(null);
    }
  };

  const handleRunRemapRestore = async () => {
    if (!remapBackupPath || !remapLibraryRoot) return;

    // Client-side guard against picking a single audio file as
    // the library root. The Tauri folder picker enforces this, but the
    // input is freely editable so we still see paste-an-MP3 cases. We have
    // no fs.stat plugin so we use a filename-extension heuristic — good
    // enough to catch the common mistake; the backend should also reject.
    if (looksLikeAudioFile(remapLibraryRoot)) {
      setRemapValidationError(
        "Please pick a folder, not a file. The library root should be the directory that contains your audio files.",
      );
      return;
    }
    setRemapValidationError(null);

    begin("backup");
    try {
      const stats = await rpc<RemapRestoreStats>("restore_tags_with_remap", {
        backup_path: remapBackupPath,
        library_root: remapLibraryRoot,
      });
      finish();
      setRemapResult(stats);
      // Same as straight restore — refresh the in-memory library
      // so the user can't accidentally Apply over the freshly-restored tags.
      // The remap restore points at `remapLibraryRoot`, so use that.
      await refreshLibraryAfterRestore(remapLibraryRoot);
    } catch (e) {
      fail(e);
    }
  };

  // Once a result is shown the dialog is "sticky" — the user must
  // click X or Close. Backdrop clicks otherwise blow away the only place the
  // result panel renders, leaving the user with no way to see what happened.
  const handleCloseRemapDialog = () => {
    // Don't allow closing while the operation is running — would orphan progress UI
    if (active !== null) return;
    setRemapDialogOpen(false);
  };
  const handleBackdropClickRemapDialog = () => {
    // Active op: same rule as the explicit close.
    if (active !== null) return;
    // Result shown: require explicit dismissal so the result panel isn't lost.
    if (remapResult) return;
    setRemapDialogOpen(false);
  };

  return (
    <div className="h-full overflow-auto">
      <div className="flex items-center justify-between px-4 py-3 border-b border-white/5">
        <div>
          <h1 className="font-display font-semibold text-white">Tags</h1>
          <p className="text-xs text-white/40">Back up, then restore if anything goes wrong</p>
        </div>
      </div>

      <div className="px-6 py-5 space-y-6 max-w-3xl">
        {errorMsg && (
          <div className="panel-pad text-sm text-accent-red flex gap-2">
            <AlertCircle className="w-4 h-4 flex-none mt-0.5" />
            <div className="break-words">{errorMsg}</div>
          </div>
        )}

        {/* Stall detector banner. Only shown during a backup-class
            op when no progress event has fired for PROGRESS_STALL_MS. */}
        {progressStalled && active === "backup" && (
          <div className="panel-pad bg-accent-yellow/5 border-accent-yellow/30 text-sm flex items-start gap-3">
            <Clock className="w-5 h-5 text-accent-yellow flex-none mt-0.5" />
            <div className="flex-1">
              <div className="text-accent-yellow">This seems stuck.</div>
              <div className="text-xs text-white/60 mt-1">
                No progress for over a minute. The operation may still be working
                on a single large file, or it may have hung.
              </div>
            </div>
            {/* The old copy said "Cancel to abort" without offering a Cancel —
                embed the affordance instead of pointing at one elsewhere. */}
            <button
              className="btn-ghost btn-sm text-accent-yellow flex-none"
              onClick={() => { void rpc("cancel_operation").catch(() => undefined); }}
              title="Cancel the stuck operation"
            >
              Cancel
            </button>
          </div>
        )}

        {lastBackup && (
          <div className="panel-pad bg-accent-green/5 border-accent-green/30 text-sm">
            <div className="flex items-start gap-2">
              <CheckCircle2 className="w-4 h-4 text-accent-green flex-none mt-0.5" />
              <div>
                <div className="text-accent-green">Backup complete</div>
                <div className="text-white/60 mt-0.5">
                  Saved tags for <strong>{lastBackup.count}</strong> files to{" "}
                  <code className="font-mono text-xs">{lastBackup.file}</code>
                </div>
              </div>
            </div>
          </div>
        )}

        {/* Backup card */}
        <Card
          icon={<Archive className="w-6 h-6 text-accent" />}
          title="Back up all tags"
          subtitle="A single JSON file capturing every tag on every audio file — including Rekordbox cue points and beat grids. Run this before any tagging or organize operation."
        >
          <div className="space-y-3">
            <div>
              <div className="label">Folder to back up</div>
              <div className="flex gap-2">
                <input
                  type="text"
                  className="input flex-1 font-mono text-xs"
                  value={pathToBackup ?? ""}
                  placeholder="(no folder selected)"
                  onChange={(e) => setPathToBackup(e.target.value)}
                />
                <button className="btn-ghost" onClick={handlePickFolder}>
                  <FolderOpen className="w-4 h-4" />
                  Choose
                </button>
              </div>
            </div>
            <button
              className="btn-primary"
              disabled={!pathToBackup || active !== null}
              onClick={handleBackup}
            >
              <Save className="w-4 h-4" />
              Create backup
            </button>
            <p className="text-xs text-white/40">
              Backups can be many GB for large libraries — most of the size is base64-encoded
              binary Rekordbox data. Keep them somewhere safe.
            </p>
          </div>
        </Card>

        {/* Stale backup reminder */}
        {isStale && lastBackupForCurrent && (
          <div className="panel-pad bg-accent-yellow/5 border-accent-yellow/30 text-sm flex items-start gap-3">
            <Clock className="w-5 h-5 text-accent-yellow flex-none mt-0.5" />
            <div className="flex-1">
              <div className="text-accent-yellow">Your last backup is over a month old</div>
              <div className="text-xs text-white/60 mt-1">
                For <code className="font-mono">{pathToBackup}</code>, your most recent backup
                was {relativeTime(lastBackupForCurrent.created_at)}. Run a fresh one before
                any operation that writes tags.
              </div>
            </div>
          </div>
        )}

        {/* Restore card */}
        <Card
          icon={<Upload className="w-6 h-6 text-accent-yellow" />}
          title="Restore tags from a backup"
          subtitle="Replays a tag backup onto your files. Any tag changes since the backup will be overwritten."
        >
          <div className="flex flex-wrap gap-2">
            <button
              className="btn-ghost"
              disabled={active !== null}
              onClick={handleStartRestore}
            >
              <FolderOpen className="w-4 h-4" />
              Choose a backup file
            </button>
            <button
              className="btn-ghost"
              disabled={active !== null}
              onClick={handleOpenRemapDialog}
              title="Restore even if you renamed your music drive or moved the library — Vibechek will match by filename"
            >
              <Shuffle className="w-4 h-4" />
              Restore (auto-detect moved files)
            </button>
          </div>
          <p className="text-xs text-white/40 mt-2">
            Use "auto-detect" when you've renamed your music drive
            (e.g. <code className="font-mono">D:\</code> → <code className="font-mono">E:\</code>) or moved the
            whole library to a new folder. Vibechek will walk the new location and match each
            backup entry by filename.
          </p>
        </Card>

        {/* History */}
        {history.length > 0 && (
          <Card
            icon={<History className="w-6 h-6 text-accent" />}
            title="Past backups"
            subtitle={`${history.length} backup${history.length === 1 ? "" : "s"} you've created. Click Restore to replay one.`}
          >
            <div className="space-y-1">
              {history.map((r) => (
                <BackupRow
                  key={r.backup_path}
                  record={r}
                  disabled={active !== null}
                  onRestore={() => handleRestoreFromHistory(r)}
                  onForget={() => handleAskForgetBackup(r)}
                />
              ))}
            </div>
          </Card>
        )}
      </div>

      <ConfirmModal
        open={confirmRestore}
        variant="danger"
        title="Restore tags from backup?"
        message={
          <div className="space-y-2">
            <p>You're about to overwrite every tag on every file referenced by:</p>
            <code className="block font-mono text-xs text-white/70 bg-surface-300 p-2 rounded break-all">
              {restoreCandidate}
            </code>
            <p>This includes title, artist, genre, BPM, key, energy, mood, custom tags, and any
              Rekordbox binary frames captured in the backup. Any changes you've made since the
              backup was created will be lost.</p>
            <p className="text-accent-yellow">There is no undo.</p>
          </div>
        }
        confirmLabel="Yes, restore"
        cancelLabel="Cancel"
        onConfirm={handleConfirmRestore}
        onCancel={() => setConfirmRestore(false)}
      />

      {/* Forget-backup confirmation modal. */}
      <ConfirmModal
        open={forgetCandidate !== null}
        variant="default"
        title="Forget this backup record?"
        message={
          <div className="space-y-2">
            <p>The backup file itself stays where it is — only its entry in this list is removed.</p>
            {forgetCandidate && (
              <code className="block font-mono text-xs text-white/70 bg-surface-300 p-2 rounded break-all">
                {forgetCandidate.backup_path}
              </code>
            )}
            <p className="text-xs text-white/50">
              You can re-add it by running Create backup again, but you'll need the source folder to do so.
            </p>
          </div>
        }
        confirmLabel="Yes, forget it"
        cancelLabel="Cancel"
        onConfirm={handleConfirmForget}
        onCancel={() => setForgetCandidate(null)}
      />

      {remapDialogOpen && (
        <RemapRestoreDialog
          backupPath={remapBackupPath}
          libraryRoot={remapLibraryRoot}
          result={remapResult}
          busy={active !== null}
          validationError={remapValidationError}
          onPickBackup={handlePickRemapBackup}
          onPickLibrary={handlePickRemapLibrary}
          onBackupPathChange={(s) => {
            setRemapBackupPath(s);
            setRemapValidationError(null);
          }}
          onLibraryRootChange={(s) => {
            setRemapLibraryRoot(s);
            setRemapValidationError(null);
          }}
          onRun={handleRunRemapRestore}
          onClose={handleCloseRemapDialog}
          onBackdropClick={handleBackdropClickRemapDialog}
        />
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Remap restore dialog — restore against a moved library.
// ---------------------------------------------------------------------------

function RemapRestoreDialog({
  backupPath,
  libraryRoot,
  result,
  busy,
  validationError,
  onPickBackup,
  onPickLibrary,
  onBackupPathChange,
  onLibraryRootChange,
  onRun,
  onClose,
  onBackdropClick,
}: {
  backupPath: string;
  libraryRoot: string;
  result: RemapRestoreStats | null;
  busy: boolean;
  validationError: string | null;
  onPickBackup: () => void;
  onPickLibrary: () => void;
  onBackupPathChange: (s: string) => void;
  onLibraryRootChange: (s: string) => void;
  onRun: () => void;
  onClose: () => void;
  onBackdropClick: () => void;
}) {
  const canRun = backupPath.length > 0 && libraryRoot.length > 0 && !busy;
  // Esc mirrors the backdrop click (the handler upstream suppresses while busy).
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onBackdropClick();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onBackdropClick]);
  return (
    <div
      className="fixed inset-0 z-[60] flex items-center justify-center bg-black/60"
      onClick={onBackdropClick}
      role="dialog"
      aria-modal="true"
      aria-label="Restore (auto-detect moved files)"
    >
      <div
        className="panel shadow-2xl w-full max-w-xl mx-4 p-5"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-start justify-between mb-4">
          <div>
            <h2 className="font-display font-semibold text-lg text-white">
              Restore (auto-detect moved files)
            </h2>
            <p className="text-xs text-white/50 mt-1">
              For backups taken before you moved or renamed your library. Vibechek matches each
              backup entry to a file in the chosen folder by filename (and size when available).
            </p>
          </div>
          <button
            className="text-white/40 hover:text-white p-1"
            onClick={onClose}
            disabled={busy}
            title="Close"
            aria-label="Close"
          >
            <X className="w-4 h-4" />
          </button>
        </div>

        <div className="space-y-3">
          <div>
            <div className="label">Backup file (.json)</div>
            <div className="flex gap-2">
              <input
                type="text"
                className="input flex-1 font-mono text-xs"
                value={backupPath}
                placeholder="(no backup selected)"
                onChange={(e) => onBackupPathChange(e.target.value)}
              />
              <button className="btn-ghost" onClick={onPickBackup} disabled={busy}>
                <FolderOpen className="w-4 h-4" />
                Choose
              </button>
            </div>
          </div>
          <div>
            <div className="label">New library root</div>
            <div className="flex gap-2">
              <input
                type="text"
                className="input flex-1 font-mono text-xs"
                value={libraryRoot}
                placeholder="(no folder selected)"
                onChange={(e) => onLibraryRootChange(e.target.value)}
              />
              <button className="btn-ghost" onClick={onPickLibrary} disabled={busy}>
                <FolderOpen className="w-4 h-4" />
                Choose
              </button>
            </div>
            <p className="text-[11px] text-white/40 mt-1">
              The folder where your audio currently lives — Vibechek walks it recursively.
            </p>
            {/* Client-side validation message. */}
            {validationError && (
              <div className="mt-2 text-xs text-accent-red flex items-start gap-1.5">
                <AlertCircle className="w-3.5 h-3.5 flex-none mt-0.5" />
                <span>{validationError}</span>
              </div>
            )}
          </div>

          {result && <RemapResultPanel result={result} />}

          <div className="flex justify-end gap-2 pt-2">
            <button className="btn-ghost" onClick={onClose} disabled={busy}>
              Close
            </button>
            <button
              className="btn-primary"
              onClick={onRun}
              disabled={!canRun}
              title={!canRun && !busy ? "Pick both a backup file and a library root" : undefined}
            >
              <RotateCcw className="w-4 h-4" />
              {busy ? "Restoring…" : "Run restore"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

function RemapResultPanel({ result }: { result: RemapRestoreStats }) {
  const skipped = result.skipped_missing + result.skipped_size_mismatch;
  const allMissing = result.restored === 0 && skipped > 0;

  // Per-file detail. The backend already returns a `matches` array
  // with per-file strategy info — surface it in a collapsible section so the
  // user can spot ambiguous matches and resolve them by picking a more
  // specific library root. Default: collapsed (the panel could be huge).
  const [showDetails, setShowDetails] = useState(false);

  const ambiguous = result.matches.filter((m) => m.strategy === "ambiguous");
  const skippedDetail = result.matches.filter(
    (m) => m.matched === null || m.strategy === "ambiguous",
  );
  const errored = result.matches.filter((m) => !!m.error);

  return (
    <div
      className={`mt-2 panel-pad text-sm border ${
        allMissing
          ? "bg-accent-red/5 border-accent-red/30"
          : "bg-accent-green/5 border-accent-green/30"
      }`}
    >
      <div className="flex items-start gap-2 mb-2">
        {allMissing ? (
          <AlertCircle className="w-4 h-4 text-accent-red flex-none mt-0.5" />
        ) : (
          <CheckCircle2 className="w-4 h-4 text-accent-green flex-none mt-0.5" />
        )}
        <div className="text-white">
          <strong>{result.restored}</strong> of {result.total} restored
          {allMissing && " — no files matched, try a different library root"}
        </div>
      </div>
      <ul className="text-xs text-white/70 space-y-0.5 ml-6 list-disc">
        <li>Matched by exact path: <strong>{result.matched_exact}</strong></li>
        <li>Matched by filename + size: <strong>{result.matched_filename_size}</strong></li>
        <li>Matched by filename alone: <strong>{result.matched_filename}</strong></li>
        <li>
          Skipped: <strong>{skipped}</strong>
          {ambiguous.length > 0 && (
            <span className="text-accent-yellow">
              {" "}(of which {ambiguous.length} ambiguous — multiple candidates)
            </span>
          )}
        </li>
        {result.errors.length > 0 && (
          <li className="text-accent-yellow">Write errors: <strong>{result.errors.length}</strong></li>
        )}
      </ul>

      {(skippedDetail.length > 0 || errored.length > 0) && (
        <div className="mt-3">
          <button
            type="button"
            className="text-xs text-white/60 hover:text-white flex items-center gap-1"
            onClick={() => setShowDetails((v) => !v)}
          >
            {showDetails ? (
              <ChevronDown className="w-3.5 h-3.5" />
            ) : (
              <ChevronRight className="w-3.5 h-3.5" />
            )}
            {showDetails ? "Hide details" : "Show details"}
          </button>
          {showDetails && (
            <div className="mt-2 space-y-3">
              {ambiguous.length > 0 && (
                <RemapDetailSection
                  title={`Ambiguous (${ambiguous.length}) — filename matched multiple files`}
                  hint="Re-run against a more specific subfolder to disambiguate."
                  rows={ambiguous}
                />
              )}
              {skippedDetail.length > ambiguous.length && (
                <RemapDetailSection
                  title={`Skipped (${skippedDetail.length - ambiguous.length}) — no matching file in the new library root`}
                  hint="The filename from the backup wasn't found under the chosen folder."
                  rows={skippedDetail.filter((m) => m.strategy !== "ambiguous")}
                />
              )}
              {errored.length > 0 && (
                <RemapDetailSection
                  title={`Write errors (${errored.length})`}
                  hint="The file was matched but the tag write failed."
                  rows={errored}
                />
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function RemapDetailSection({
  title,
  hint,
  rows,
}: {
  title: string;
  hint: string;
  rows: RemapRestoreStats["matches"];
}) {
  // Cap rendered rows — a 12k library could blow up the dialog.
  const MAX = 50;
  const shown = rows.slice(0, MAX);
  const more = rows.length - shown.length;
  return (
    <div>
      <div className="text-xs text-white/70 font-medium">{title}</div>
      <div className="text-[11px] text-white/40 mb-1">{hint}</div>
      <ul className="text-[11px] text-white/60 space-y-0.5 max-h-40 overflow-auto bg-black/20 rounded p-2 font-mono">
        {shown.map((m, i) => (
          <li key={`${m.original}-${i}`} className="break-all">
            <span className="text-white/80">{m.original}</span>
            {m.matched && (
              <>
                {" "}
                <span className="text-white/40">→</span>{" "}
                <span className="text-white/60">{m.matched}</span>
              </>
            )}
            {m.error && <span className="text-accent-red"> — {m.error}</span>}
          </li>
        ))}
        {more > 0 && (
          <li className="text-white/40 italic">…and {more} more</li>
        )}
      </ul>
    </div>
  );
}

function Card({
  icon, title, subtitle, children,
}: {
  icon: React.ReactNode;
  title: string;
  subtitle: string;
  children: React.ReactNode;
}) {
  return (
    <div className="panel-pad">
      <div className="flex items-start gap-3 mb-4">
        <div className="flex-none">{icon}</div>
        <div className="flex-1">
          <h2 className="font-display font-semibold text-lg">{title}</h2>
          <p className="text-sm text-white/60">{subtitle}</p>
        </div>
      </div>
      {children}
    </div>
  );
}

function BackupRow({
  record,
  disabled,
  onRestore,
  onForget,
}: {
  record: BackupRecord;
  disabled: boolean;
  onRestore: () => void;
  onForget: () => void;
}) {
  return (
    <div
      className={`flex items-center gap-3 px-3 py-2 rounded-md border ${
        record.missing
          ? "border-accent-red/30 bg-accent-red/5 opacity-70"
          : "border-white/5 hover:bg-white/[0.02]"
      }`}
    >
      <Archive className={`w-4 h-4 flex-none ${record.missing ? "text-accent-red" : "text-accent"}`} />
      <div className="flex-1 min-w-0">
        <div className="text-sm text-white truncate" title={record.backup_path}>
          {record.backup_path.split(/[/\\]/).pop()}
        </div>
        <div className="text-[11px] text-white/40 truncate" title={record.library_path}>
          {record.library_path}
        </div>
      </div>
      <div className="hidden sm:flex flex-col items-end text-[11px] text-white/40 font-mono tabular-nums w-28">
        <span>{record.file_count.toLocaleString()} files</span>
        <span>{formatBytes(record.size_bytes)}</span>
      </div>
      <div className="text-[11px] text-white/40 w-24 text-right">
        {record.missing ? <span className="text-accent-red">missing</span> : relativeTime(record.created_at)}
      </div>
      <button
        onClick={onRestore}
        disabled={disabled || record.missing}
        className="btn-ghost text-xs disabled:opacity-30"
        title="Restore this backup"
      >
        <RotateCcw className="w-3.5 h-3.5" />
        Restore
      </button>
      <button
        onClick={onForget}
        className="text-white/30 hover:text-white p-1"
        title="Remove from history (file itself is not deleted)"
      >
        <X className="w-3.5 h-3.5" />
      </button>
    </div>
  );
}
