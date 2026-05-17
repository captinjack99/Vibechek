import { useCallback, useEffect, useMemo, useState } from "react";
import { Virtuoso } from "react-virtuoso";
import { AnimatePresence } from "framer-motion";
import {
  FolderOpen, Sparkles, Search, Music, AlertCircle, CheckSquare, Square, Tag,
  Eye, RefreshCw, Clock, X,
} from "lucide-react";
import { clsx as cx } from "clsx";
import { open as openDialog } from "@tauri-apps/plugin-dialog";

import {
  useLibraryStore,
  useOperationStore,
  useConfigStore,
  useUIStore,
  useNotificationStore,
} from "../stores";
import { rpc } from "../hooks/useSidecar";
import { useApplyTags } from "../hooks/useApplyTags";
import type {
  AnalysisReport, LibraryRecord, LibraryState, PreflightResult, TrackAnalysis,
} from "../types";
import { TagBadge, EnergyBar } from "./TagBadges";
import { PreflightDialog } from "./PreflightDialog";
import { ConfirmModal } from "./ConfirmModal";
import { FilterChips, applyFilters, emptyFilters, type LibraryFilters } from "./LibraryFilters";

/** Compact number formatter — "12k" instead of "12,466". */
const compactFmt = new Intl.NumberFormat(undefined, {
  notation: "compact",
  maximumFractionDigits: 1,
});

/** "3 hours ago", "yesterday", "2 days ago". Epoch seconds in, friendly string out. */
function relativeTime(epochSeconds: number): string {
  if (!epochSeconds) return "never";
  const secondsAgo = Math.max(0, Date.now() / 1000 - epochSeconds);
  if (secondsAgo < 60) return "just now";
  const minutes = Math.floor(secondsAgo / 60);
  if (minutes < 60) return minutes === 1 ? "1 minute ago" : `${minutes} minutes ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return hours === 1 ? "1 hour ago" : `${hours} hours ago`;
  const days = Math.floor(hours / 24);
  if (days === 1) return "yesterday";
  if (days < 30) return `${days} days ago`;
  const months = Math.floor(days / 30);
  if (months < 12) return months === 1 ? "1 month ago" : `${months} months ago`;
  const years = Math.floor(months / 12);
  return years === 1 ? "1 year ago" : `${years} years ago`;
}

/** Last path segment, handling both / and \ separators (Windows + POSIX). */
function basename(path: string): string {
  const segments = path.split(/[\\/]/).filter(Boolean);
  return segments[segments.length - 1] ?? path;
}

export function LibraryBrowser() {
  const tracks = useLibraryStore((s) => s.tracks);
  const libraryPath = useLibraryStore((s) => s.libraryPath);
  const setLibraryPath = useLibraryStore((s) => s.setLibraryPath);
  const setTracks = useLibraryStore((s) => s.setTracks);
  const searchFilter = useLibraryStore((s) => s.searchFilter);
  const setSearchFilter = useLibraryStore((s) => s.setSearchFilter);
  const selectedIds = useLibraryStore((s) => s.selectedIds);
  const toggleSelect = useLibraryStore((s) => s.toggleSelect);
  const selectAll = useLibraryStore((s) => s.selectAll);
  const clearSelection = useLibraryStore((s) => s.clearSelection);

  const active = useOperationStore((s) => s.active);
  const begin = useOperationStore((s) => s.begin);
  const finish = useOperationStore((s) => s.finish);
  const fail = useOperationStore((s) => s.fail);
  const errorMsg = useOperationStore((s) => s.error);

  const analysisCfg = useConfigStore((s) => s.config.analysis);
  const taggingCfg = useConfigStore((s) => s.config.tagging);

  const setSelectedTrack = useUIStore((s) => s.setSelectedTrack);
  const selectedTrackPath = useUIStore((s) => s.selectedTrackPath);

  const notify = useNotificationStore((s) => s.notify);
  const { apply: applyTags } = useApplyTags();

  const [scanCount, setScanCount] = useState<number | null>(null);
  const [preflightResult, setPreflightResult] = useState<PreflightResult | null>(null);
  const [confirmBulkTag, setConfirmBulkTag] = useState<"selected" | "all" | null>(null);
  const [filters, setFilters] = useState<LibraryFilters>(emptyFilters());
  const [showErrorsOnly, setShowErrorsOnly] = useState(false);
  const [recentLibraries, setRecentLibraries] = useState<LibraryRecord[]>([]);

  // Pull the recent-libraries list on mount + after a forget. Both empty-state
  // visibility and the cards themselves read from this.
  const refreshRecent = useCallback(async () => {
    try {
      const state = await rpc<LibraryState>("library_state");
      setRecentLibraries(state.recent ?? []);
    } catch {
      setRecentLibraries([]);
    }
  }, []);

  useEffect(() => {
    refreshRecent();
  }, [refreshRecent]);

  // Click handler for a recent-library card: reload the saved analysis and
  // hydrate the store. Falls back to just setting the path if the analysis
  // file went missing under us.
  const handleOpenRecent = async (record: LibraryRecord) => {
    begin("analyze");
    try {
      const result = await rpc<{ loaded: boolean; report?: AnalysisReport; reason?: string }>(
        "load_recent_analysis",
        { library_path: record.path },
      );
      if (!result.loaded || !result.report) {
        fail(result.reason ?? "Could not load saved analysis");
        await refreshRecent();
        return;
      }
      setLibraryPath(record.path);
      setTracks(result.report.tracks);
      finish();
    } catch (e) {
      fail(String(e));
    }
  };

  const handleForgetRecent = async (record: LibraryRecord) => {
    try {
      await rpc("forget_library", { path: record.path });
    } finally {
      await refreshRecent();
    }
  };

  const analyzedCount = useMemo(
    () => tracks.filter((t) => t.ml_analysis).length,
    [tracks],
  );
  const unanalyzedCount = tracks.length - analyzedCount;

  // Genre breakdown for the bulk-tag confirm modal: only count tracks whose
  // genre will actually be written (confidence >= threshold). Tracks without
  // a confidence are counted as zero (won't be written either).
  const tagPreview = useMemo(() => {
    if (!confirmBulkTag) return null;
    const targets = confirmBulkTag === "all"
      ? tracks
      : tracks.filter((t) => selectedIds.has(t.path));
    const threshold = taggingCfg.genre_confidence_threshold;

    const counts = new Map<string, number>();
    let belowThreshold = 0;
    for (const t of targets) {
      const ml = t.ml_analysis;
      const conf = ml?.ml_genre_confidence;
      if (!ml?.ml_genre || conf == null || conf < threshold) {
        belowThreshold += 1;
        continue;
      }
      // Prefer subgenre when present — that's what gets written.
      const label = ml.ml_subgenre || ml.ml_genre;
      counts.set(label, (counts.get(label) ?? 0) + 1);
    }
    const sorted = [...counts.entries()].sort((a, b) => b[1] - a[1]);
    const top = sorted.slice(0, 5);
    const others = sorted.slice(5);
    const otherCount = others.reduce((sum, [, n]) => sum + n, 0);
    const otherGenres = others.length;
    const willWrite = sorted.reduce((sum, [, n]) => sum + n, 0);
    return {
      total: targets.length,
      top,
      otherCount,
      otherGenres,
      willWrite,
      belowThreshold,
    };
  }, [confirmBulkTag, tracks, selectedIds, taggingCfg.genre_confidence_threshold]);

  // Tracks with either a top-level scan/decode error or an ML failure.
  const errorCount = useMemo(
    () => tracks.filter((t) => t.error || t.ml_analysis?.ml_error).length,
    [tracks],
  );

  const filtered = useMemo(() => {
    let result = showErrorsOnly
      ? tracks.filter((t) => t.error || t.ml_analysis?.ml_error)
      : applyFilters(tracks, filters);
    if (searchFilter) {
      const q = searchFilter.toLowerCase();
      result = result.filter((t) =>
        t.filename.toLowerCase().includes(q) ||
        (t.filename_artist ?? "").toLowerCase().includes(q) ||
        (t.filename_title ?? "").toLowerCase().includes(q) ||
        (t.ml_analysis?.ml_genre ?? "").toLowerCase().includes(q) ||
        (t.ml_analysis?.ml_subgenre ?? "").toLowerCase().includes(q),
      );
    }
    return result;
  }, [tracks, searchFilter, filters, showErrorsOnly]);

  const handleOpenFolder = async () => {
    const selected = await openDialog({ directory: true, multiple: false });
    if (typeof selected !== "string") return;

    setLibraryPath(selected);
    setTracks([]);
    begin("analyze");
    try {
      const result = await rpc<{ count: number }>("scan_directory", { path: selected });
      setScanCount(result.count);
      finish();
    } catch (e) {
      fail(String(e));
    }
  };

  // "Just show me my library" — instant, no ML. Reads filenames + existing tags
  // so the user can browse / dedupe / organize without committing to a long ML run.
  const runFastScan = async () => {
    if (!libraryPath) return;
    begin("analyze");
    try {
      const report = await rpc<AnalysisReport>("scan_only", { path: libraryPath });
      setTracks(report.tracks);
      finish();
    } catch (e) {
      fail(String(e));
    }
  };

  // Run ML analyze. If `incremental` is true, skip every file already in `tracks`
  // and merge the new results into what's there.
  const runAnalyze = async (incremental = false) => {
    if (!libraryPath) return;
    const alreadyAnalyzed = incremental
      ? tracks.filter((t) => t.ml_analysis).map((t) => t.path)
      : [];
    begin("analyze");
    try {
      const report = await rpc<AnalysisReport>("analyze_directory", {
        path: libraryPath,
        workers: analysisCfg.workers,
        use_gpu: analysisCfg.use_gpu,
        skip_paths: alreadyAnalyzed,
      });
      if (incremental) {
        // Merge: keep existing analyzed tracks, add the new ones, dedup by path
        const existing = new Map(tracks.map((t) => [t.path, t]));
        for (const t of report.tracks) existing.set(t.path, t);
        setTracks(Array.from(existing.values()));
      } else {
        setTracks(report.tracks);
      }
      finish();
    } catch (e) {
      fail(String(e));
    }
  };

  // Bulk apply ML tags to either selected tracks or all of them. The actual
  // RPC + loading/error handling is in useApplyTags; we just decide the scope
  // and surface a toast.
  const runBulkTag = async (scope: "selected" | "all") => {
    const targets = scope === "selected"
      ? tracks.filter((t) => selectedIds.has(t.path))
      : tracks;
    if (targets.length === 0) return;
    const result = await applyTags(targets);
    if (!result) return; // failure already surfaced via useOperationStore.error
    const threshold = Math.round(taggingCfg.genre_confidence_threshold * 100);
    const detail =
      `Genre (conf >= ${threshold}%): ${result.applied}\n` +
      `Skipped (low confidence): ${result.skipped}\n` +
      `Other tags (energy / mood / etc): ${result.other}` +
      (result.errors.length > 0 ? `\nErrors: ${result.errors.length}` : "");
    notify(`Tagged ${result.applied + result.other} files`, {
      detail,
      kind: result.errors.length > 0 ? "info" : "success",
    });
  };

  // Gate analyze behind preflight. If not ready, show the dialog; the user
  // can fix things and click Re-check, which auto-proceeds when ready.
  const handleAnalyze = async () => {
    if (!libraryPath) return;
    try {
      // Fast preflight first — opens the dialog immediately without waiting
      // for the (slow) per-distro WSL probe.
      const quick = await rpc<PreflightResult>("preflight", {});
      if (quick.ready) {
        runAnalyze();
        return;
      }
      setPreflightResult(quick);
      // Then upgrade with the detailed WSL probe in the background
      if (quick.wsl?.is_windows) {
        rpc<PreflightResult["wsl"]>("wsl_status", { quick: false })
          .then((wsl) => {
            setPreflightResult((prev) => {
              if (!prev) return prev;
              const wslReady = wsl?.can_run_vibechek ?? false;
              const ready =
                (prev.essentia.installed || wslReady) &&
                prev.models.missing.length === 0;
              const analyze_via: "native" | "wsl" | null = prev.essentia.installed
                ? "native"
                : wslReady
                ? "wsl"
                : null;
              return { ...prev, wsl, ready, analyze_via };
            });
          })
          .catch(() => {});
      }
    } catch (e) {
      fail(String(e));
    }
  };

  // Preflight dialog (rendered above whichever state is active)
  const preflightOverlay = (
    <AnimatePresence>
      {preflightResult && !preflightResult.ready && (
        <PreflightDialog
          preflight={preflightResult}
          onRefresh={setPreflightResult}
          onClose={() => setPreflightResult(null)}
          onReady={() => {
            setPreflightResult(null);
            runAnalyze();
          }}
        />
      )}
    </AnimatePresence>
  );

  // Empty state
  if (tracks.length === 0) {
    // If the user has recent libraries AND hasn't pre-selected a folder yet,
    // show those instead of just "Open folder". Clicking a card re-loads the
    // saved analysis straight into the store.
    const showRecents = !libraryPath && recentLibraries.length > 0;

    return (
      <div className="h-full flex flex-col">
        <Header onOpen={handleOpenFolder} libraryPath={libraryPath} />
        {preflightOverlay}
        <div className="flex-1 overflow-auto flex items-center justify-center px-8 py-8">
          {showRecents ? (
            <div className="w-full max-w-2xl">
              <div className="text-center mb-6">
                <div className="w-12 h-12 mx-auto mb-3 rounded-xl bg-white/5 flex items-center justify-center">
                  <Music className="w-6 h-6 text-white/30" />
                </div>
                <h2 className="text-xl font-display font-semibold mb-1">
                  Welcome back
                </h2>
                <p className="text-sm text-white/50">
                  Pick up where you left off, or open a different folder.
                </p>
              </div>

              <div className="space-y-2">
                {recentLibraries.map((rec) => (
                  <RecentLibraryCard
                    key={rec.path}
                    record={rec}
                    disabled={active !== null}
                    onOpen={() => handleOpenRecent(rec)}
                    onForget={() => handleForgetRecent(rec)}
                  />
                ))}
              </div>

              <div className="text-center mt-6">
                <button
                  className="btn-ghost"
                  onClick={handleOpenFolder}
                  disabled={active !== null}
                >
                  <FolderOpen className="w-4 h-4" />
                  Open a different folder
                </button>
              </div>

              {errorMsg && (
                <div className="mt-6 panel-pad text-left text-sm text-accent-red flex gap-2">
                  <AlertCircle className="w-4 h-4 flex-none mt-0.5" />
                  <div className="break-words">{errorMsg}</div>
                </div>
              )}
            </div>
          ) : (
            <div className="text-center max-w-md">
              <div className="w-16 h-16 mx-auto mb-4 rounded-2xl bg-white/5 flex items-center justify-center">
                <Music className="w-8 h-8 text-white/30" />
              </div>
              <h2 className="text-xl font-display font-semibold mb-2">
                {libraryPath ? "Ready to analyze" : "Open a folder to get started"}
              </h2>
              <p className="text-white/50 mb-6">
                {libraryPath
                  ? `Found ${scanCount ?? "?"} audio files in ${libraryPath}. Run analysis to detect genre, energy, mood, and more — or jump straight to dedup / organize using existing tags.`
                  : "Vibechek will scan your music folder, then let you analyze, dedupe, tag, or reorganize it."}
              </p>
              {libraryPath ? (
                <div className="space-y-3">
                  <div className="flex items-center justify-center gap-2">
                    <button
                      className="btn-primary"
                      onClick={runFastScan}
                      disabled={active !== null}
                      title="Read filenames + existing tags. Takes a few seconds."
                    >
                      <Eye className="w-4 h-4" />
                      Just show me my library
                    </button>
                    <button
                      className="btn-ghost"
                      onClick={handleAnalyze}
                      disabled={active !== null}
                      title="Full ML pass — detects genre, mood, energy, etc. Takes longer."
                    >
                      <Sparkles className="w-4 h-4" />
                      Analyze with ML
                    </button>
                  </div>
                  <div>
                    <button
                      className="text-xs text-white/40 hover:text-white"
                      onClick={handleOpenFolder}
                    >
                      Choose a different folder
                    </button>
                  </div>
                </div>
              ) : (
                <button className="btn-primary" onClick={handleOpenFolder}>
                  <FolderOpen className="w-4 h-4" />
                  Open folder
                </button>
              )}
              {errorMsg && (
                <div className="mt-6 panel-pad text-left text-sm text-accent-red flex gap-2">
                  <AlertCircle className="w-4 h-4 flex-none mt-0.5" />
                  <div className="break-words">{errorMsg}</div>
                </div>
              )}
            </div>
          )}
        </div>
      </div>
    );
  }

  // Populated state
  return (
    <div className="h-full flex flex-col">
      <Header onOpen={handleOpenFolder} libraryPath={libraryPath} />
      {preflightOverlay}

      {/* Toolbar */}
      <div className="flex items-center gap-3 px-4 py-3 border-b border-white/5">
        <button
          className="text-white/40 hover:text-white"
          onClick={() =>
            selectedIds.size === filtered.length ? clearSelection() : selectAll()
          }
          title={selectedIds.size === filtered.length ? "Clear selection" : "Select all"}
        >
          {selectedIds.size > 0 && selectedIds.size === filtered.length ? (
            <CheckSquare className="w-5 h-5" />
          ) : (
            <Square className="w-5 h-5" />
          )}
        </button>

        <div className="relative flex-1 max-w-md">
          <Search className="w-4 h-4 absolute left-3 top-1/2 -translate-y-1/2 text-white/40" />
          <input
            type="text"
            className="input w-full pl-9"
            placeholder="Filter tracks..."
            value={searchFilter}
            onChange={(e) => setSearchFilter(e.target.value)}
          />
        </div>

        <div className="text-xs text-white/40 font-mono">
          {selectedIds.size > 0 ? `${selectedIds.size} selected • ` : ""}
          {filtered.length} / {tracks.length}
        </div>

        {selectedIds.size > 0 ? (
          <>
            <button
              className="btn-primary"
              onClick={() => setConfirmBulkTag("selected")}
              disabled={active !== null}
            >
              <Tag className="w-4 h-4" />
              Apply ML tags to {selectedIds.size}
            </button>
            <button className="btn-ghost" onClick={() => clearSelection()}>
              Clear
            </button>
          </>
        ) : (
          <>
            {analyzedCount > 0 && (
              <button
                className="btn-ghost"
                onClick={() => setConfirmBulkTag("all")}
                disabled={active !== null}
                title="Write ML tags to every analyzed track"
              >
                <Tag className="w-4 h-4" />
                Apply ML tags to all
              </button>
            )}
            {unanalyzedCount > 0 && libraryPath && (
              <button
                className="btn-ghost"
                onClick={() => runAnalyze(true)}
                disabled={active !== null}
                title={`Run ML on the ${unanalyzedCount} tracks that haven't been analyzed yet`}
              >
                <Sparkles className="w-4 h-4" />
                Analyze new ({unanalyzedCount})
              </button>
            )}
            <button
              className="btn-ghost"
              onClick={handleAnalyze}
              disabled={active !== null || !libraryPath}
              title="Re-run ML on the whole library"
            >
              <RefreshCw className="w-4 h-4" />
              Re-analyze all
            </button>
          </>
        )}
      </div>

      {/* Filter chips (only render when there are analyzed tracks to filter) */}
      {(analyzedCount > 0 || errorCount > 0) && (
        <div className="px-4 py-2 border-b border-white/5 flex items-center gap-3 flex-wrap">
          {analyzedCount > 0 && !showErrorsOnly && (
            <FilterChips tracks={tracks} filters={filters} setFilters={setFilters} />
          )}
          {errorCount > 0 && (
            <button
              onClick={() => setShowErrorsOnly((v) => !v)}
              className={cx(
                "ml-auto flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs border transition-colors",
                showErrorsOnly
                  ? "bg-accent-yellow/20 text-accent-yellow border-accent-yellow/40"
                  : "bg-accent-yellow/10 text-accent-yellow border-accent-yellow/30 hover:bg-accent-yellow/20",
              )}
              title={
                showErrorsOnly
                  ? "Showing only tracks with errors — click to clear"
                  : "Show only tracks with errors"
              }
            >
              <AlertCircle className="w-3.5 h-3.5" />
              {errorCount} error{errorCount === 1 ? "" : "s"}
              {showErrorsOnly && <span className="text-accent-yellow/70">· showing</span>}
            </button>
          )}
        </div>
      )}

      {/* Track list */}
      <div className="flex-1 min-h-0">
        <Virtuoso
          data={filtered}
          itemContent={(_, track) => (
            <TrackRow
              key={track.path}
              track={track}
              checked={selectedIds.has(track.path)}
              onCheck={() => toggleSelect(track.path)}
              selected={track.path === selectedTrackPath}
              onClick={() => setSelectedTrack(track.path)}
            />
          )}
        />
      </div>

      <ConfirmModal
        open={confirmBulkTag !== null}
        title={`Apply ML tags to ${confirmBulkTag === "all" ? "all" : selectedIds.size} tracks?`}
        message={
          <div className="space-y-3">
            {tagPreview && (
              <div className="panel-pad bg-white/[0.02]">
                <div className="text-xs uppercase tracking-wider text-white/40 mb-2">
                  Will write
                </div>
                {tagPreview.willWrite === 0 ? (
                  <div className="text-sm text-accent-yellow">
                    No tracks above the {Math.round(taggingCfg.genre_confidence_threshold * 100)}% confidence threshold —
                    nothing will be written.
                  </div>
                ) : (
                  <div className="flex flex-wrap gap-x-3 gap-y-1 text-sm">
                    {tagPreview.top.map(([genre, count]) => (
                      <span key={genre} className="text-white">
                        <span className="font-mono text-accent">
                          {count.toLocaleString()}
                        </span>{" "}
                        <span className="text-white/70">{genre}</span>
                      </span>
                    ))}
                    {tagPreview.otherGenres > 0 && (
                      <span className="text-white/50">
                        <span className="font-mono">{tagPreview.otherCount.toLocaleString()}</span>{" "}
                        across {tagPreview.otherGenres} other{" "}
                        {tagPreview.otherGenres === 1 ? "genre" : "genres"}
                      </span>
                    )}
                  </div>
                )}
                {tagPreview.belowThreshold > 0 && (
                  <div className="mt-2 text-xs text-white/50">
                    {tagPreview.belowThreshold.toLocaleString()} track{tagPreview.belowThreshold === 1 ? "" : "s"} will be
                    skipped (below {Math.round(taggingCfg.genre_confidence_threshold * 100)}% genre confidence,
                    or not yet analyzed). Energy / mood / timeslot tags will still be written for analyzed files.
                  </div>
                )}
              </div>
            )}
            <ul className="list-disc list-inside text-xs text-white/60 space-y-1">
              <li>Rekordbox cue points and beat grids are preserved.</li>
              <li>BPM and key {taggingCfg.skip_bpm_and_key ? "are NOT touched" : "will be overwritten by ML values"}.</li>
              <li>This cannot be undone — back up your tags first (Tags tab).</li>
            </ul>
          </div>
        }
        confirmLabel="Yes, apply tags"
        cancelLabel="Cancel"
        variant="default"
        onConfirm={() => {
          const scope = confirmBulkTag!;
          setConfirmBulkTag(null);
          runBulkTag(scope);
        }}
        onCancel={() => setConfirmBulkTag(null)}
      />
    </div>
  );
}

function Header({ onOpen, libraryPath }: { onOpen: () => void; libraryPath: string | null }) {
  return (
    <div className="flex items-center justify-between px-4 py-3 border-b border-white/5">
      <div>
        <h1 className="font-display font-semibold text-white">Library</h1>
        <p className="text-xs text-white/40 truncate max-w-md">
          {libraryPath ?? "no folder open"}
        </p>
      </div>
      <button className="btn-ghost" onClick={onOpen}>
        <FolderOpen className="w-4 h-4" />
        Open folder
      </button>
    </div>
  );
}

interface TrackRowProps {
  track: TrackAnalysis;
  selected: boolean;
  checked: boolean;
  onCheck: () => void;
  onClick: () => void;
}

function TrackRow({ track, selected, checked, onCheck, onClick }: TrackRowProps) {
  const ml = track.ml_analysis;
  return (
    <div
      onClick={onClick}
      className={cx("track-row", selected && "selected")}
    >
      <button
        className="text-white/30 hover:text-white p-1 mr-2 flex-none"
        onClick={(e) => { e.stopPropagation(); onCheck(); }}
        title={checked ? "Deselect" : "Select"}
      >
        {checked ? (
          <CheckSquare className="w-4 h-4 text-accent" />
        ) : (
          <Square className="w-4 h-4" />
        )}
      </button>

      <div className="flex-1 min-w-0 mr-4">
        <div className="text-sm text-white truncate">
          {track.filename_title ?? track.filename}
        </div>
        <div className="text-xs text-white/40 truncate">
          {track.filename_artist ?? ""}
        </div>
      </div>

      <div className="hidden sm:flex items-center gap-2 mr-4">
        {ml?.ml_genre && (
          <TagBadge color="purple">{ml.ml_subgenre || ml.ml_genre}</TagBadge>
        )}
        {ml?.ml_bpm && (
          <TagBadge color="cyan">{Math.round(ml.ml_bpm)} BPM</TagBadge>
        )}
        {ml?.ml_key && <TagBadge color="green">{ml.ml_key}</TagBadge>}
      </div>

      <div className="w-20 hidden md:block">
        {ml?.ml_energy != null && <EnergyBar level={ml.ml_energy} />}
      </div>

      <div className="w-16 text-right text-xs text-white/40 font-mono">
        {track.size_mb.toFixed(1)}M
      </div>
    </div>
  );
}

interface RecentLibraryCardProps {
  record: LibraryRecord;
  disabled: boolean;
  onOpen: () => void;
  onForget: () => void;
}

function RecentLibraryCard({ record, disabled, onOpen, onForget }: RecentLibraryCardProps) {
  // Right-click → forget. Single-button context menus are overkill; just run
  // the action straight from the menu event.
  const handleContextMenu = (e: React.MouseEvent) => {
    e.preventDefault();
    if (disabled) return;
    if (window.confirm(`Remove "${basename(record.path)}" from recent libraries?`)) {
      onForget();
    }
  };

  return (
    <div
      role="button"
      tabIndex={0}
      aria-disabled={disabled}
      onClick={() => !disabled && onOpen()}
      onKeyDown={(e) => {
        if ((e.key === "Enter" || e.key === " ") && !disabled) {
          e.preventDefault();
          onOpen();
        }
      }}
      onContextMenu={handleContextMenu}
      className={cx(
        "group panel-pad flex items-center gap-4 text-left transition-colors",
        disabled
          ? "opacity-50 cursor-not-allowed"
          : "cursor-pointer hover:bg-white/[0.04] hover:border-accent/30",
      )}
    >
      <div className="w-10 h-10 rounded-lg bg-accent/15 text-accent flex items-center justify-center flex-none">
        <Music className="w-5 h-5" />
      </div>
      <div className="flex-1 min-w-0">
        <div className="font-display font-medium text-base text-white truncate">
          {basename(record.path)}
        </div>
        <div className="text-xs text-white/40 truncate font-mono" title={record.path}>
          {record.path}
        </div>
        <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-0.5 text-xs text-white/60">
          <span>
            <span className="text-white/80 font-mono">
              {compactFmt.format(record.track_count)}
            </span>{" "}
            tracks
          </span>
          <span>
            <span className="text-white/80 font-mono">
              {compactFmt.format(record.analyzed_count)}
            </span>{" "}
            analyzed
          </span>
          <span className="flex items-center gap-1 text-white/40">
            <Clock className="w-3 h-3" />
            Opened {relativeTime(record.last_opened)}
          </span>
        </div>
      </div>
      <button
        className="opacity-0 group-hover:opacity-100 focus:opacity-100 text-white/40 hover:text-accent-red p-1.5 rounded transition-opacity"
        onClick={(e) => {
          e.stopPropagation();
          if (!disabled) onForget();
        }}
        title="Forget this library"
        aria-label={`Forget ${basename(record.path)}`}
        disabled={disabled}
      >
        <X className="w-4 h-4" />
      </button>
    </div>
  );
}

