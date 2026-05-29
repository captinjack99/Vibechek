/**
 * Config store — mirrors `vibechek.config.VibechekConfig`.
 *
 * Split out of stores/index.ts. Re-exported from `../stores` for backwards
 * compatibility.
 */

import { create } from "zustand";

import type { VibechekConfig } from "../types";

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
  analysis: { workers: 0, models_dir: "", use_gpu: "auto", hybrid_cpu_gpu: true },
  tagging: {
    genre_confidence_threshold: 0.85,
    // Two-stage confidence floor — if the subgenre is below the strict 85%
    // threshold but the parent-genre family score clears 50%, we still write
    // the parent genre. Matches vibechek/config.py:TaggingConfig.
    parent_genre_confidence_threshold: 0.5,
    write_subgenre_as_main_genre: true,
    preserve_rekordbox_frames: true,
    backup_before_write: true,
    // Per-field write toggles (replace the old single skip_bpm_and_key flag).
    // BPM/Key default off — Rekordbox's own detection is usually better.
    write_genre: true,
    write_bpm: false,
    write_key: false,
    write_energy: true,
    write_mood: true,
    write_timeslot: true,
    write_direction: true,
    write_vocal: true,
    // Vocal classification cutoffs (voice probability 0..1), applied at tag
    // time to ml_vocal_score so they can be tuned without re-analyzing.
    vocal_instrumental_max: 0.72,
    vocal_full_min: 0.88,
    // 3 = UTF-8 (the modern ID3v2.4 default). Settings exposes a picker for
    // users on legacy software (Rekordbox 5 only reads encoding=1 UTF-16).
    id3_text_encoding: 3,
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
