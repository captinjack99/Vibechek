import { useEffect } from "react";
import { AnimatePresence, MotionConfig } from "framer-motion";

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
import { OperationsHistory } from "./components/OperationsHistory";
import { GlobalAudioPlayer } from "./components/GlobalAudioPlayer";

import { useUIStore, useOperationStore, useConfigStore, useLibraryStore, useNotificationStore } from "./stores";
import { useSidecarProgress, useSidecarEvent } from "./hooks/useSidecar";
import { useConfigPersistence } from "./hooks/useConfigPersistence";
import type { TrackAnalysis } from "./types";

interface TrackAnalyzedPayload {
  current: number;
  total: number;
  track: TrackAnalysis;
}

/**
 * Payload of the sidecar's `notify` notification — emitted at startup for
 * known-problematic install locations (Google Drive "My Drive", OneDrive,
 * iCloud, Dropbox, very long / space-heavy paths) that can make the
 * PyInstaller bootloader hang or behave erratically.
 */
interface NotifyPayload {
  level?: string;
  message: string;
  detail?: string;
}

/**
 * True if `trackPath` lives under `root`. Normalizes separators + case so
 * Windows `C:\Foo\bar.flac` matches a root of `C:/Foo` regardless of slash
 * direction or casing.
 */
function isUnderLibrary(trackPath: string, root: string | null): boolean {
  if (!root) return false;
  const norm = (p: string) => p.replace(/\\/g, "/").replace(/\/+$/, "").toLowerCase();
  const t = norm(trackPath);
  const lib = norm(root);
  return t === lib || t.startsWith(lib + "/");
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
  //
  // Stale-event guard: a cancelled/superseded analyze keeps streaming events
  // that are already queued in the sidecar's thread pool + Tauri's event
  // channel. A path-prefix-only check ("is this track under the current
  // library?") is NOT enough — switching from `D:/Music/House` to its parent
  // `D:/Music` leaves the old run's events still "under" the new library, so
  // they phantom-merge (mergeAnalyzedTrack appends on path-not-found).
  //
  // Instead we anchor live-merge to an explicit run token (LibraryBrowser
  // bumps it per analyze; any library switch invalidates it). We accept an
  // event only when (a) a run is currently authorized and (b) the track lives
  // under THAT run's launch root — not merely under whatever library is open
  // now. State is read fresh from the store on each event (not via a
  // subscription) so this closure never goes stale.
  //
  // Note: the sidecar's `track_analyzed` notification carries no run id of its
  // own, so this is the strongest guard available frontend-side; once the run
  // is invalidated, every still-queued event from it is dropped.
  useSidecarEvent<TrackAnalyzedPayload>("track_analyzed", (payload) => {
    const path = payload?.track?.path;
    if (!path) return;
    const { analyzeRun } = useLibraryStore.getState();
    if (!analyzeRun) return;
    if (!isUnderLibrary(path, analyzeRun.root)) return;
    mergeAnalyzedTrack(payload.track);
  });

  // Surface the sidecar's startup install-path warning. The sidecar emits a
  // `notify` notification (re-broadcast by the Rust shell as `sidecar:notify`)
  // when it detects a risky install location — without a listener this was the
  // single mechanism warning users about hang-prone paths, and it was silently
  // dropped. Warnings render amber (the store's "warning" kind) instead of
  // being shoehorned into cheerful cyan "info".
  useSidecarEvent<NotifyPayload>("notify", (payload) => {
    if (!payload?.message) return;
    useNotificationStore.getState().notify(payload.message, {
      kind: payload.level === "warning" ? "warning" : "info",
      detail: payload.detail,
    });
  });

  // Load config from disk on startup, then auto-save (debounced) on change.
  useConfigPersistence();

  // First-launch tour. Don't render it until config is loaded — otherwise we'd
  // flash the overlay over an already-onboarded user before disk catches up.
  const configLoaded = useConfigStore((s) => s.loaded);
  const seenOnboarding = useConfigStore((s) => s.config.ui.seen_onboarding);
  const showOnboarding = configLoaded && !seenOnboarding;

  // Esc clears the track-inspector selection — but NOT while a modal dialog
  // is open: Esc inside ConfirmModal/etc. should only dismiss the dialog, not
  // also silently close TrackDetails behind it.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !document.querySelector('[role="dialog"]')) {
        useUIStore.getState().setSelectedTrack(null);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  // Desktop-app feel: suppress the browser context menu ("Reload", "Inspect",
  // "Back") everywhere except editable fields and explicit opt-ins.
  useEffect(() => {
    const onContextMenu = (e: MouseEvent) => {
      const t = e.target as HTMLElement | null;
      if (t?.closest("input, textarea, [contenteditable], [data-allow-context]")) return;
      e.preventDefault();
    };
    window.addEventListener("contextmenu", onContextMenu);
    return () => window.removeEventListener("contextmenu", onContextMenu);
  }, []);

  return (
    // reducedMotion="user": every framer-motion animation respects the OS
    // prefers-reduced-motion setting (the CSS keyframes are handled by the
    // media query in globals.css).
    <MotionConfig reducedMotion="user">
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
          {/* TrackDetails is library-tab only (it's the track inspector for the
              library list). Audio playback no longer lives here — the global
              <GlobalAudioPlayer/> bar owns it and persists across tabs. */}
          {viewMode === "library" && <TrackDetails />}
        </div>

        {/* The single global audio player — persists across every tab so
            playback is always controllable and only one track ever sounds. */}
        <GlobalAudioPlayer />

        <AnimatePresence>
          <AnalysisProgress />
          <ErrorToast />
        </AnimatePresence>
        <Toast />

        <AnimatePresence>
          <OperationsHistory />
        </AnimatePresence>

        <AnimatePresence>
          {showOnboarding && <Onboarding />}
        </AnimatePresence>
      </div>
    </MotionConfig>
  );
}
