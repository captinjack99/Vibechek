/**
 * Library store — analyzed tracks + the current selection.
 *
 * Split out of stores/index.ts so each domain has its own file. The store
 * itself is re-exported from `../stores` so existing imports keep working
 * (`import { useLibraryStore } from "../stores"`).
 */

import { create } from "zustand";

import type { TrackAnalysis } from "../types";

interface LibraryState {
  libraryPath: string | null;
  tracks: TrackAnalysis[];
  selectedIds: Set<string>;
  searchFilter: string;

  setLibraryPath: (path: string | null) => void;
  setTracks: (tracks: TrackAnalysis[]) => void;
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
   * Rewrite track paths in-place after a filesystem move (e.g. organize).
   * Each entry of `pathMap` maps an old source path to its new destination.
   * Tracks not in the map are left untouched. Also rewrites the matching
   * `filename` field, and migrates any matching selectedIds across so the
   * selection survives the move. ML analysis fields are preserved.
   */
  updateTrackPaths: (pathMap: Map<string, string> | Record<string, string>) => void;
  toggleSelect: (path: string) => void;
  selectAll: () => void;
  clearSelection: () => void;
  setSearchFilter: (s: string) => void;
}

export const useLibraryStore = create<LibraryState>((set, get) => ({
  libraryPath: null,
  tracks: [],
  selectedIds: new Set(),
  searchFilter: "",

  setLibraryPath: (path) => set({ libraryPath: path }),
  setTracks: (tracks) => set({ tracks, selectedIds: new Set() }),

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
  clearSelection: () => set({ selectedIds: new Set() }),
  setSearchFilter: (s) => set({ searchFilter: s }),
}));
