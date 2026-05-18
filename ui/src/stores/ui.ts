/**
 * UI store — view mode, sidebar collapsed flag, currently-selected track.
 *
 * Split out of stores/index.ts. Re-exported from `../stores` for backwards
 * compatibility.
 */

import { create } from "zustand";

import type { ViewMode } from "../types";

interface UIState {
  viewMode: ViewMode;
  sidebarCollapsed: boolean;
  selectedTrackPath: string | null;

  setViewMode: (m: ViewMode) => void;
  toggleSidebar: () => void;
  setSelectedTrack: (path: string | null) => void;
}

export const useUIStore = create<UIState>((set) => ({
  viewMode: "library",
  sidebarCollapsed: false,
  selectedTrackPath: null,

  setViewMode: (m) => set({ viewMode: m }),
  toggleSidebar: () => set((s) => ({ sidebarCollapsed: !s.sidebarCollapsed })),
  setSelectedTrack: (path) => set({ selectedTrackPath: path }),
}));
