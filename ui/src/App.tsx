import { useEffect } from "react";
import { AnimatePresence } from "framer-motion";

import { Sidebar } from "./components/Sidebar";
import { LibraryBrowser } from "./components/LibraryBrowser";
import { DuplicatesView } from "./components/DuplicatesView";
import { OrganizeView } from "./components/OrganizeView";
import { TagsView } from "./components/TagsView";
import { Settings } from "./components/Settings";
import { AnalysisProgress } from "./components/AnalysisProgress";
import { TrackDetails } from "./components/TrackDetails";
import { ErrorToast } from "./components/ErrorToast";
import { Toast } from "./components/Toast";
import { Onboarding } from "./components/Onboarding";

import { useUIStore, useOperationStore, useConfigStore, useLibraryStore } from "./stores";
import { useSidecarProgress, useSidecarEvent } from "./hooks/useSidecar";
import { useConfigPersistence } from "./hooks/useConfigPersistence";
import type { TrackAnalysis } from "./types";

interface TrackAnalyzedPayload {
  current: number;
  total: number;
  track: TrackAnalysis;
}

export default function App() {
  const viewMode = useUIStore((s) => s.viewMode);
  const setProgress = useOperationStore((s) => s.setProgress);
  const mergeAnalyzedTrack = useLibraryStore((s) => s.mergeAnalyzedTrack);

  // Pipe every sidecar progress notification into the operation store. Any
  // component can read it; the progress overlay does so.
  useSidecarProgress((evt) => setProgress(evt));

  // Live-merge per-track results as the sidecar streams them during analyze.
  // The user sees tracks appear in the library table in real-time instead of
  // waiting for the whole batch to finish. The sidecar emits one
  // `track_analyzed` notification per track once the structured event
  // channel in vibechek/analyzer.py is active (which happens automatically
  // for WSL- and managed-venv-routed analyzes — both set
  // VIBECHEK_STREAM_PROGRESS=1 in the subprocess env).
  useSidecarEvent<TrackAnalyzedPayload>("track_analyzed", (payload) => {
    if (payload?.track?.path) {
      mergeAnalyzedTrack(payload.track);
    }
  });

  // Load config from disk on startup, then auto-save (debounced) on change.
  useConfigPersistence();

  // First-launch tour. Don't render it until config is loaded — otherwise we'd
  // flash the overlay over an already-onboarded user before disk catches up.
  const configLoaded = useConfigStore((s) => s.loaded);
  const seenOnboarding = useConfigStore((s) => s.config.ui.seen_onboarding);
  const showOnboarding = configLoaded && !seenOnboarding;

  // Esc closes overlays / clears selection
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        useUIStore.getState().setSelectedTrack(null);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  return (
    <div className="h-full flex flex-col bg-surface-200">
      <div className="flex-1 flex min-h-0">
        <Sidebar />
        <main className="flex-1 min-w-0 overflow-hidden">
          {viewMode === "library" && <LibraryBrowser />}
          {viewMode === "duplicates" && <DuplicatesView />}
          {viewMode === "organize" && <OrganizeView />}
          {viewMode === "tags" && <TagsView />}
          {viewMode === "settings" && <Settings />}
        </main>
        {/* TrackDetails is library-tab only. Mounting it everywhere meant
            the embedded AudioPreview's WaveSurfer instance kept playing
            after the user navigated to Duplicates / Organize / etc — there
            was no UI to control or even see the player. Unmounting on tab
            change destroys WaveSurfer (its useEffect cleanup), which
            stops playback. The previously-selected track stays in the
            store, so flipping back to Library shows the same panel again
            (just without auto-resuming playback). */}
        {viewMode === "library" && <TrackDetails />}
      </div>

      <AnimatePresence>
        <AnalysisProgress />
        <ErrorToast />
      </AnimatePresence>
      <Toast />

      <AnimatePresence>
        {showOnboarding && <Onboarding />}
      </AnimatePresence>
    </div>
  );
}
