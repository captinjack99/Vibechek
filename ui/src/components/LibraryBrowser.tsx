import { useMemo, useState } from "react";
import { Virtuoso } from "react-virtuoso";
import { AnimatePresence } from "framer-motion";
import { FolderOpen, Sparkles, Search, Music, AlertCircle } from "lucide-react";
import { clsx as cx } from "clsx";
import { open as openDialog } from "@tauri-apps/plugin-dialog";

import { useLibraryStore, useOperationStore, useConfigStore, useUIStore } from "../stores";
import { rpc } from "../hooks/useSidecar";
import type { AnalysisReport, PreflightResult, TrackAnalysis } from "../types";
import { TagBadge, EnergyBar } from "./TagBadges";
import { PreflightDialog } from "./PreflightDialog";

export function LibraryBrowser() {
  const tracks = useLibraryStore((s) => s.tracks);
  const libraryPath = useLibraryStore((s) => s.libraryPath);
  const setLibraryPath = useLibraryStore((s) => s.setLibraryPath);
  const setTracks = useLibraryStore((s) => s.setTracks);
  const searchFilter = useLibraryStore((s) => s.searchFilter);
  const setSearchFilter = useLibraryStore((s) => s.setSearchFilter);

  const active = useOperationStore((s) => s.active);
  const begin = useOperationStore((s) => s.begin);
  const finish = useOperationStore((s) => s.finish);
  const fail = useOperationStore((s) => s.fail);
  const errorMsg = useOperationStore((s) => s.error);

  const analysisCfg = useConfigStore((s) => s.config.analysis);

  const setSelectedTrack = useUIStore((s) => s.setSelectedTrack);
  const selectedTrackPath = useUIStore((s) => s.selectedTrackPath);

  const [scanCount, setScanCount] = useState<number | null>(null);
  const [preflightResult, setPreflightResult] = useState<PreflightResult | null>(null);

  const filtered = useMemo(() => {
    if (!searchFilter) return tracks;
    const q = searchFilter.toLowerCase();
    return tracks.filter((t) =>
      t.filename.toLowerCase().includes(q) ||
      (t.filename_artist ?? "").toLowerCase().includes(q) ||
      (t.filename_title ?? "").toLowerCase().includes(q) ||
      (t.ml_analysis?.ml_genre ?? "").toLowerCase().includes(q) ||
      (t.ml_analysis?.ml_subgenre ?? "").toLowerCase().includes(q),
    );
  }, [tracks, searchFilter]);

  const handleOpenFolder = async () => {
    const selected = await openDialog({ directory: true, multiple: false });
    if (typeof selected !== "string") return;

    setLibraryPath(selected);
    begin("analyze");
    try {
      const result = await rpc<{ count: number }>("scan_directory", { path: selected });
      setScanCount(result.count);
      finish();
    } catch (e) {
      fail(String(e));
    }
  };

  const runAnalyze = async () => {
    if (!libraryPath) return;
    begin("analyze");
    try {
      const report = await rpc<AnalysisReport>("analyze_directory", {
        path: libraryPath,
        workers: analysisCfg.workers,
        use_gpu: analysisCfg.use_gpu,
      });
      setTracks(report.tracks);
      finish();
    } catch (e) {
      fail(String(e));
    }
  };

  // Gate analyze behind preflight. If not ready, show the dialog; the user
  // can fix things and click Re-check, which auto-proceeds when ready.
  const handleAnalyze = async () => {
    if (!libraryPath) return;
    try {
      const result = await rpc<PreflightResult>("preflight", {});
      if (result.ready) {
        runAnalyze();
      } else {
        setPreflightResult(result);
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
              <div className="flex items-center justify-center gap-2">
                <button
                  className="btn-primary"
                  onClick={handleAnalyze}
                  disabled={active !== null}
                >
                  <Sparkles className="w-4 h-4" />
                  Analyze with ML
                </button>
                <button className="btn-ghost" onClick={handleOpenFolder}>
                  Choose a different folder
                </button>
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
          {filtered.length} / {tracks.length}
        </div>
        <button
          className="btn-ghost"
          onClick={handleAnalyze}
          disabled={active !== null || !libraryPath}
        >
          <Sparkles className="w-4 h-4" />
          Re-analyze
        </button>
      </div>

      {/* Track list */}
      <div className="flex-1 min-h-0">
        <Virtuoso
          data={filtered}
          itemContent={(_, track) => (
            <TrackRow
              key={track.path}
              track={track}
              selected={track.path === selectedTrackPath}
              onClick={() => setSelectedTrack(track.path)}
            />
          )}
        />
      </div>
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
  onClick: () => void;
}

function TrackRow({ track, selected, onClick }: TrackRowProps) {
  const ml = track.ml_analysis;
  return (
    <div
      onClick={onClick}
      className={cx("track-row", selected && "selected")}
    >
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

