/**
 * Library store — analyzed tracks + the current selection.
 *
 * Split out of stores/index.ts so each domain has its own file. The store
 * itself is re-exported from `../stores` so existing imports keep working
 * (`import { useLibraryStore } from "../stores"`).
 */

import { create } from "zustand";

import type { TrackAnalysis } from "../types";
import { useUIStore } from "./ui";
import { usePlayerStore } from "./player";

/**
 * Identity of the analyze run currently authorized to live-merge streamed
 * `track_analyzed` events. `id` is a monotonic token bumped on every new run
 * (and on every library switch, which invalidates any in-flight run); `root`
 * is the exact library path that run was launched against.
 *
 * The `track_analyzed` listener uses this to drop "phantom" events from a
 * superseded run: a cancelled/replaced analyze keeps streaming events that are
 * already queued in the sidecar + Tauri's event channel, and a path-prefix-only
 * guard merges them into the wrong library after a switch (e.g. switching from
 * `D:/Music/House` to its parent `D:/Music` — the old folder's events are still
 * "under" the new library). Anchoring the merge to an explicit run token that a
 * switch invalidates closes that hole.
 */
export interface AnalyzeRun {
  id: number;
  root: string;
}

interface LibraryState {
  libraryPath: string | null;
  tracks: TrackAnalysis[];
  selectedIds: Set<string>;
  searchFilter: string;
  /** The run allowed to live-merge streamed events, or null when none. */
  analyzeRun: AnalyzeRun | null;

  setLibraryPath: (path: string | null) => void;
  setTracks: (tracks: TrackAnalysis[]) => void;
  /**
   * Open a new live-merge run rooted at `root`, returning its token. Any
   * previously-active run is implicitly superseded (its token no longer
   * matches), so its still-streaming events are dropped by the listener.
   */
  beginAnalyzeRun: (root: string) => number;
  /**
   * Close the run with token `id` if it's still the active one. A no-op if a
   * newer run has already superseded it (so a slow run finishing late can't
   * clear a fresh run's authorization).
   */
  endAnalyzeRun: (id: number) => void;
  /**
   * Invalidate any active live-merge run without starting a new one. Called on
   * library switches so a stale run's queued events stop merging immediately.
   */
  invalidateAnalyzeRun: () => void;
  /**
   * Merge one analyzed-track record into the library, by path. Used by the
   * `sidecar:track_analyzed` event handler so the user sees tracks populate
   * live during an analyze pass instead of waiting for the whole batch.
   *
   * Semantics: replace the existing entry that matches `record.path`. If no
   * such entry exists (e.g. analyzing a folder that wasn't pre-scanned), the
   * record is appended. selectedIds is untouched.
   */
  mergeAnalyzedTrack: (record: TrackAnalysis) => void;
  /**
   * Merge many analyzed-track records at once (by path), in a single state
   * update. Used by batch mutations — e.g. resolving a queue of reviewed genre
   * conflicts — where calling `mergeAnalyzedTrack` per record would fire N
   * separate setStates (and N re-renders). Records whose path isn't present are
   * appended; selectedIds is untouched. A no-op for an empty list.
   */
  mergeAnalyzedTracks: (records: TrackAnalysis[]) => void;
  /**
   * Rewrite track paths in-place after a filesystem move (e.g. organize).
   * Each entry of `pathMap` maps an old source path to its new destination.
   * Tracks not in the map are left untouched. Also rewrites the matching
   * `filename` field, and migrates any matching selectedIds across so the
   * selection survives the move. ML analysis fields are preserved.
   */
  updateTrackPaths: (pathMap: Map<string, string> | Record<string, string>) => void;
  toggleSelect: (path: string) => void;
  selectAll: () => void;
  /**
   * Select exactly the given paths (used to "select all" within an active
   * filter — selecting the *filtered* set rather than the whole library, so
   * the toolbar count and any bulk action operate on what the user can see).
   */
  selectPaths: (paths: string[]) => void;
  clearSelection: () => void;
  setSearchFilter: (s: string) => void;
}

// Monotonic source for analyze-run tokens. Module-level so it survives the
// store closure and never collides across runs within a session.
let nextAnalyzeRunId = 1;

export const useLibraryStore = create<LibraryState>((set, get) => ({
  libraryPath: null,
  tracks: [],
  selectedIds: new Set(),
  searchFilter: "",
  analyzeRun: null,

  setLibraryPath: (path) => set({ libraryPath: path }),
  setTracks: (tracks) => set({ tracks, selectedIds: new Set() }),

  beginAnalyzeRun: (root) => {
    const id = nextAnalyzeRunId++;
    set({ analyzeRun: { id, root } });
    return id;
  },
  endAnalyzeRun: (id) => {
    const current = get().analyzeRun;
    // Only the run that's still active may clear itself — a late-finishing
    // older run must not wipe a newer run's authorization.
    if (current && current.id === id) set({ analyzeRun: null });
  },
  invalidateAnalyzeRun: () => {
    if (get().analyzeRun) set({ analyzeRun: null });
  },

  mergeAnalyzedTrack: (record) => {
    const { tracks } = get();
    // Path-based identity. The sidecar always sends a path (Windows-side
    // path post-translation via wsl_to_win_path). We do a linear scan
    // because tracks lists are typically <50k entries and analyze events
    // stream at ~5/sec — well below the threshold where Map-based lookup
    // would matter. Bumping the list to a Map keyed by path would also
    // force every selector that re-derives from `tracks` to recompute.
    const idx = tracks.findIndex((t) => t.path === record.path);
    if (idx === -1) {
      set({ tracks: [...tracks, record] });
      return;
    }
    const next = tracks.slice();
    next[idx] = record;
    set({ tracks: next });
  },

  mergeAnalyzedTracks: (records) => {
    if (records.length === 0) return;
    // Build a path→record overlay once, then a single pass over the existing
    // list. Existing entries are replaced in place (preserving order); any
    // record with a path not already present is appended. One setState total.
    const overlay = new Map(records.map((r) => [r.path, r]));
    const next = get().tracks.map((t) => overlay.get(t.path) ?? t);
    const known = new Set(next.map((t) => t.path));
    for (const r of records) {
      if (!known.has(r.path)) next.push(r);
    }
    set({ tracks: next });
  },

  updateTrackPaths: (pathMap) => {
    // Normalize Record → Map for a single lookup path. A Map is cheaper to
    // iterate and supports any string key (incl. ones with weird characters).
    const lookup = pathMap instanceof Map ? pathMap : new Map(Object.entries(pathMap));
    if (lookup.size === 0) return;

    const { tracks, selectedIds } = get();
    let changed = false;
    const nextTracks = tracks.map((t) => {
      const newPath = lookup.get(t.path);
      if (!newPath || newPath === t.path) return t;
      changed = true;
      // Recompute filename from the new path's last segment so the library
      // table doesn't show the old name. Handles both Windows and POSIX seps.
      const lastSep = Math.max(newPath.lastIndexOf("/"), newPath.lastIndexOf("\\"));
      const filename = lastSep >= 0 ? newPath.slice(lastSep + 1) : newPath;
      return { ...t, path: newPath, filename };
    });
    if (!changed) return;

    // Migrate selection: any selected id that was just moved gets rewritten
    // to the new path so the user's selection survives.
    let nextSelected = selectedIds;
    if (selectedIds.size > 0) {
      const migrated = new Set<string>();
      let selChanged = false;
      for (const id of selectedIds) {
        const newPath = lookup.get(id);
        if (newPath && newPath !== id) {
          migrated.add(newPath);
          selChanged = true;
        } else {
          migrated.add(id);
        }
      }
      if (selChanged) nextSelected = migrated;
    }

    set({ tracks: nextTracks, selectedIds: nextSelected });

    // Follow the moved track in the right-rail inspector too. That selection
    // lives in the separate UI store; without this it keeps pointing at the
    // pre-move path, so TrackDetails finds no match and silently closes after
    // an organize moved the open track. (selectedIds above is migrated; this
    // completes the same guarantee for the single inspected track.)
    const ui = useUIStore.getState();
    const movedSel = ui.selectedTrackPath ? lookup.get(ui.selectedTrackPath) : undefined;
    if (movedSel && movedSel !== ui.selectedTrackPath) ui.setSelectedTrack(movedSel);

    // Follow the moved track in the global audio player too. The player keeps
    // its own `path` (set by play()), and nothing else touches it on a move —
    // so after an organize relocates the loaded track, Play/Restart would call
    // WaveSurfer.load() against the now-nonexistent source and error/time out.
    // Rewrite it to the new location, preserving title + playToken so the bar's
    // identity (and the isCurrent check) stays in sync.
    const player = usePlayerStore.getState();
    const movedPlay = player.path ? lookup.get(player.path) : undefined;
    if (movedPlay && movedPlay !== player.path) usePlayerStore.setState({ path: movedPlay });
  },

  toggleSelect: (path) => {
    const next = new Set(get().selectedIds);
    if (next.has(path)) next.delete(path);
    else next.add(path);
    set({ selectedIds: next });
  },
  selectAll: () => {
    const all = new Set(get().tracks.map((t) => t.path));
    set({ selectedIds: all });
  },
  selectPaths: (paths) => set({ selectedIds: new Set(paths) }),
  clearSelection: () => set({ selectedIds: new Set() }),
  setSearchFilter: (s) => set({ searchFilter: s }),
}));
