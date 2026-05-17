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

import { useCallback, useEffect, useState } from "react";
import {
  Archive, FolderOpen, Save, Upload, AlertCircle, CheckCircle2, Clock,
  RotateCcw, X, History,
} from "lucide-react";
import { open as openDialog, save as saveDialog } from "@tauri-apps/plugin-dialog";

import { useLibraryStore, useNotificationStore, useOperationStore } from "../stores";
import { rpc } from "../hooks/useSidecar";
import { ConfirmModal } from "./ConfirmModal";
import type { BackupHistory, BackupRecord } from "../types";

/** Days after which we suggest re-running the backup. */
const STALE_AFTER_DAYS = 30;

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
    try {
      const stats = await rpc<{ total: number; backed_up: number; errors: string[] }>(
        "backup_tags",
        { path: pathToBackup, output_path: outFile },
      );
      finish();
      setLastBackup({ file: outFile, count: stats.backed_up });
      await refreshHistory();
    } catch (e) {
      fail(String(e));
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

  const handleForgetBackup = async (record: BackupRecord) => {
    try {
      await rpc("forget_backup", { backup_path: record.backup_path });
    } finally {
      await refreshHistory();
    }
  };

  const handleConfirmRestore = async () => {
    if (!restoreCandidate) return;
    setConfirmRestore(false);
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
    } catch (e) {
      fail(String(e));
    }
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
          <button
            className="btn-ghost"
            disabled={active !== null}
            onClick={handleStartRestore}
          >
            <FolderOpen className="w-4 h-4" />
            Choose a backup file
          </button>
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
                  onForget={() => handleForgetBackup(r)}
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
