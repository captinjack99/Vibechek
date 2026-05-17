import { useMemo, useState } from "react";
import { Virtuoso } from "react-virtuoso";
import { AnimatePresence } from "framer-motion";
import {
  FolderOpen, Sparkles, Search, Music, AlertCircle, CheckSquare, Square, Tag,
  Eye, RefreshCw,
} from "lucide-react";
import { clsx as cx } from "clsx";
import { open as openDialog } from "@tauri-apps/plugin-dialog";

import { useLibraryStore, useOperationStore, useConfigStore, useUIStore } from "../stores";
import { rpc } from "../hooks/useSidecar";
import type { AnalysisReport, PreflightResult, TrackAnalysis } from "../types";
import { TagBadge, EnergyBar } from "./TagBadges";
import { PreflightDialog } from "./PreflightDialog";
import { ConfirmModal } from "./ConfirmModal";
import { FilterChips, applyFilters, emptyFilters, type LibraryFilters } from "./LibraryFilters";

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

  const [scanCount, setScanCount] = useState<number | null>(null);
  const [preflightResult, setPreflightResult] = useState<PreflightResult | null>(null);
  const [confirmBulkTag, setConfirmBulkTag] = useState<"selected" | "all" | null>(null);
  const [filters, setFilters] = useState<LibraryFilters>(emptyFilters());

  const analyzedCount = useMemo(
    () => tracks.filter((t) => t.ml_analysis).length,
    [tracks],
  );
  const unanalyzedCount = tracks.length - analyzedCount;

  const filtered = useMemo(() => {
    let result = applyFilters(tracks, filters);
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
  }, [tracks, searchFilter, filters]);

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

  // Bulk apply ML tags to either selected tracks or all of them.
  const runBulkTag = async (scope: "selected" | "all") => {
    const targets = scope === "selected"
      ? tracks.filter((t) => selectedIds.has(t.path))
      : tracks;
    if (targets.length === 0) return;
    begin("tag");
    try {
      const stats = await rpc<{
        total: number;
        genre_applied: number;
        genre_skipped_low_confidence: number;
        other_tags_applied: number;
        errors: string[];
      }>("apply_ml_tags", {
        analysis: { tracks: targets },
        confidence: taggingCfg.genre_confidence_threshold,
        skip_bpm_and_key: taggingCfg.skip_bpm_and_key,
        preserve_rekordbox_frames: taggingCfg.preserve_rekordbox_frames,
      });
      finish();
      window.alert(
        `Tag write complete.\n\n` +
        `Genre written (confidence ≥ ${Math.round(taggingCfg.genre_confidence_threshold * 100)}%): ${stats.genre_applied}\n` +
        `Skipped (low confidence): ${stats.genre_skipped_low_confidence}\n` +
        `Other tags written (energy / mood / timeslot / etc): ${stats.other_tags_applied}\n` +
        `Errors: ${stats.errors.length}`,
      );
    } catch (e) {
      fail(String(e));
    }
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
    return (
      <div className="h-full flex flex-col">
        <Header onOpen={handleOpenFolder} libraryPath={libraryPath} />
        {preflightOverlay}
        <div className="flex-1 flex items-center justify-center px-8">
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
      {analyzedCount > 0 && (
        <div className="px-4 py-2 border-b border-white/5">
          <FilterChips tracks={tracks} filters={filters} setFilters={setFilters} />
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
          <div className="space-y-2">
            <p>
              Vibechek will write the ML genre, mood, energy, timeslot, direction, and vocal tags
              to every selected file{" "}
              <strong>where the genre confidence is at least{" "}
                {Math.round(taggingCfg.genre_confidence_threshold * 100)}%</strong>.
            </p>
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

