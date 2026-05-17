/**
 * Global UI state stores.
 *
 * Four stores keep concerns separate:
 *   - useLibraryStore — analyzed tracks + selection
 *   - useOperationStore — running long operations (analyze / dedupe / organize) + progress
 *   - useUIStore — current view, sidebar collapsed state, selected track for the detail pane
 *   - useConfigStore — user-tunable settings, mirroring vibechek.config.VibechekConfig
 *   - useNotificationStore — transient success/info toasts (auto-dismiss)
 */

import { create } from "zustand";

import type {
  DuplicateReport,
  OrganizePlan,
  ProgressEvent,
  TrackAnalysis,
  VibechekConfig,
  ViewMode,
} from "../types";

// ---------------------------------------------------------------------------
// Library
// ---------------------------------------------------------------------------

interface LibraryState {
  libraryPath: string | null;
  tracks: TrackAnalysis[];
  selectedIds: Set<string>;
  searchFilter: string;

  setLibraryPath: (path: string | null) => void;
  setTracks: (tracks: TrackAnalysis[]) => void;
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

// ---------------------------------------------------------------------------
// Long-running operations
// ---------------------------------------------------------------------------

type OperationKind = "analyze" | "dedupe" | "organize" | "tag" | "backup" | "download-models" | null;

interface OperationState {
  active: OperationKind;
  progress: ProgressEvent | null;
  startedAt: number | null;
  error: string | null;

  duplicateReport: DuplicateReport | null;
  organizePlan: OrganizePlan | null;

  begin: (kind: Exclude<OperationKind, null>) => void;
  setProgress: (p: ProgressEvent) => void;
  finish: () => void;
  fail: (error: string) => void;
  clearError: () => void;

  setDuplicateReport: (r: DuplicateReport | null) => void;
  setOrganizePlan: (p: OrganizePlan | null) => void;
}

export const useOperationStore = create<OperationState>((set) => ({
  active: null,
  progress: null,
  startedAt: null,
  error: null,

  duplicateReport: null,
  organizePlan: null,

  begin: (kind) =>
    set({ active: kind, progress: null, error: null, startedAt: Date.now() }),
  setProgress: (p) => set({ progress: p }),
  finish: () => set({ active: null, progress: null, startedAt: null }),
  fail: (error) => set({ active: null, progress: null, startedAt: null, error }),
  clearError: () => set({ error: null }),

  setDuplicateReport: (r) => set({ duplicateReport: r }),
  setOrganizePlan: (p) => set({ organizePlan: p }),
}));

// ---------------------------------------------------------------------------
// UI
// ---------------------------------------------------------------------------

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

// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------

interface ConfigState {
  config: VibechekConfig;
  /** True after first successful load from disk. Until then we don't auto-save. */
  loaded: boolean;
  setConfig: (c: VibechekConfig, markLoaded?: boolean) => void;
  updateAnalysis: (patch: Partial<VibechekConfig["analysis"]>) => void;
  updateTagging: (patch: Partial<VibechekConfig["tagging"]>) => void;
  updateDuplicates: (patch: Partial<VibechekConfig["duplicates"]>) => void;
  updateOrganization: (patch: Partial<VibechekConfig["organization"]>) => void;
  updateUI: (patch: Partial<VibechekConfig["ui"]>) => void;
}

const DEFAULT_CONFIG: VibechekConfig = {
  analysis: { workers: 0, models_dir: "", use_gpu: "auto" },
  tagging: {
    genre_confidence_threshold: 0.85,
    write_subgenre_as_main_genre: true,
    preserve_rekordbox_frames: true,
    backup_before_write: true,
    skip_bpm_and_key: true,
  },
  duplicates: {
    use_md5: true,
    use_chromaprint: true,
    chromaprint_similarity_threshold: 0.95,
    action: "report",
    review_folder: null,
  },
  organization: {
    use_subgenres: true,
    min_genre_size: 10,
    target_root: null,
  },
  ui: {
    seen_onboarding: false,
  },
};

// ---------------------------------------------------------------------------
// Notifications — transient, auto-dismissing success/info toasts.
//
// Different from useOperationStore.error (which is sticky, scary, and reserved
// for operation failures). Notifications are the "✓ done" pat-on-the-back.
// ---------------------------------------------------------------------------

export type NotificationKind = "success" | "info";

export interface Notification {
  id: number;
  kind: NotificationKind;
  message: string;
  /** Optional secondary line — shows under the main message. */
  detail?: string;
}

interface NotificationState {
  items: Notification[];
  notify: (message: string, opts?: { kind?: NotificationKind; detail?: string }) => void;
  dismiss: (id: number) => void;
}

let nextNotificationId = 1;

export const useNotificationStore = create<NotificationState>((set) => ({
  items: [],
  notify: (message, opts) => {
    const id = nextNotificationId++;
    const item: Notification = {
      id,
      kind: opts?.kind ?? "success",
      message,
      detail: opts?.detail,
    };
    set((s) => ({ items: [...s.items, item] }));
  },
  dismiss: (id) =>
    set((s) => ({ items: s.items.filter((n) => n.id !== id) })),
}));

export const useConfigStore = create<ConfigState>((set) => ({
  config: DEFAULT_CONFIG,
  loaded: false,
  setConfig: (c, markLoaded = false) => set({ config: c, ...(markLoaded ? { loaded: true } : {}) }),
  updateAnalysis: (patch) =>
    set((s) => ({ config: { ...s.config, analysis: { ...s.config.analysis, ...patch } } })),
  updateTagging: (patch) =>
    set((s) => ({ config: { ...s.config, tagging: { ...s.config.tagging, ...patch } } })),
  updateDuplicates: (patch) =>
    set((s) => ({ config: { ...s.config, duplicates: { ...s.config.duplicates, ...patch } } })),
  updateOrganization: (patch) =>
    set((s) => ({ config: { ...s.config, organization: { ...s.config.organization, ...patch } } })),
  updateUI: (patch) =>
    set((s) => ({ config: { ...s.config, ui: { ...s.config.ui, ...patch } } })),
}));
