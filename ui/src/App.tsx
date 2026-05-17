import { useEffect } from "react";
import { AnimatePresence } from "framer-motion";

import { Sidebar } from "./components/Sidebar";
import { LibraryBrowser } from "./components/LibraryBrowser";
import { DuplicatesView } from "./components/DuplicatesView";
import { OrganizeView } from "./components/OrganizeView";
import { Settings } from "./components/Settings";
import { AnalysisProgress } from "./components/AnalysisProgress";
import { TrackDetails } from "./components/TrackDetails";

import { useUIStore, useOperationStore } from "./stores";
import { useSidecarProgress } from "./hooks/useSidecar";

export default function App() {
  const viewMode = useUIStore((s) => s.viewMode);
  const setProgress = useOperationStore((s) => s.setProgress);

  // Pipe every sidecar progress notification into the operation store. Any
  // component can read it; the progress overlay does so.
  useSidecarProgress((evt) => setProgress(evt));

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
          {viewMode === "settings" && <Settings />}
        </main>
        <TrackDetails />
      </div>

      <AnimatePresence>
        <AnalysisProgress />
      </AnimatePresence>
    </div>
  );
}
