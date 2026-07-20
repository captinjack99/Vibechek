/**
 * Types mirroring the Python-side payloads emitted by the JSON-RPC sidecar.
 *
 * Most interfaces are auto-generated from the Python dataclasses — see
 * `generated.ts` and `scripts/generate_ts_types.py`. The hand-written
 * declarations below cover the bits that don't come from a dataclass:
 *   - wire-only shapes the sidecar assembles from dicts (AnalysisReport,
 *     ExistingTags, DuplicateGroup, DuplicateReport, TrackAnalysis)
 *   - view-only state (ViewMode, ProgressEvent, ReadyEvent, InstallResult)
 *
 * The generated re-export comes first so existing `from "../types"` imports
 * keep working.
 */

export * from "./generated";

import type { FileInfo, MLResult } from "./generated";

// ---------------------------------------------------------------------------
// Track analysis — TrackAnalysis dataclass exists in Python but the wire form
// types `existing_tags` / `ml_analysis` more precisely than the dataclass does
// (the dataclass uses dict[str, Any]). Keep these hand-written.
// ---------------------------------------------------------------------------

export interface ExistingTags {
  artist?: string | null;
  title?: string | null;
  album?: string | null;
  genre?: string | null;
  bpm?: number | null;
  key?: string | null;
  subgenre?: string | null;
  energy?: number | string | null;
  mood?: string | null;
  timeslot?: string | null;
  direction?: string | null;
  vocal?: string | null;
  /** Mixed In Key's 1-10 energy read from comments/grouping. A DIFFERENT scale
   *  from Vibechek's 0-5 `energy` — display as "7/10", never compare or merge. */
  energy_mik?: number | null;
  /** "rekordbox" when the genre tag came from an XML import (tag priors). */
  genre_origin?: string | null;
}

export interface TrackAnalysis {
  path: string;
  filename: string;
  extension: string;
  size_mb: number;
  filename_artist?: string | null;
  filename_title?: string | null;
  filename_bpm?: number | null;
  filename_key?: string | null;
  filename_mix?: string | null;
  existing_tags: ExistingTags;
  ml_analysis?: MLResult | null;
  error?: string | null;
}

export interface AnalysisReport {
  status: "in_progress" | "complete";
  summary: {
    total_files: number;
    analyzed: number;
    errors: number;
  };
  statistics: {
    genres: Record<string, number>;
    energy_distribution: Record<string, number>;
    timeslot_distribution: Record<string, number>;
    mood_distribution: Record<string, number>;
  };
  tracks: TrackAnalysis[];
  /** Set by the sidecar when the analyze SUCCEEDED but the report could NOT be
   *  written to disk (disk full, a cloud-sync/antivirus lock). The results are
   *  correct on screen but will vanish on next launch — the GUI raises a
   *  persistent warning so the user re-runs or exports before closing. */
  persist_error?: string | null;
  /** Where the sidecar tried to save the report — shown alongside persist_error. */
  persist_path?: string | null;
  /** Set when a Rekordbox tag-priors import existed but was dropped this run —
   *  an unreadable sidecar, or 0 tracks matched because the library moved. The
   *  curated genre edits weren't applied; the GUI warns the DJ to re-import. */
  priors_warning?: string | null;
  /** Set when CLAP was the selected genre classifier but N tracks silently fell
   *  back to the weaker Discogs model (per-track embed failure or CLAP never
   *  loaded). The GUI banners it so the user knows those genres are lower-fidelity. */
  genre_fallback_warning?: string | null;
  /** Set when one or more mood/vocal model heads failed to LOAD this run, so
   *  energy/mood/vocal ran on a fallback for every track. Names the models. */
  model_degradation_warning?: string | null;
  /** Set when the WSL engine environment was auto-repaired before this run
   *  (a version-drift reinstall / ML-stack fix). Informational — the run then
   *  proceeded on the now-working engine. */
  runtime_healed?: string | null;
  /** Set when GPU libraries couldn't be restored during that auto-repair, so
   *  the run fell back to CPU. A caution — GPU acceleration needs a re-setup. */
  runtime_heal_warning?: string | null;
  /** Set when online genre lookup was requested but its backend wasn't reachable
   *  this run, so genres used tags + audio only. A post-run degradation flag (WP-G5)
   *  the GUI banners like the sibling warnings, since the transient progress line
   *  is easy to miss. */
  genre_web_unavailable_warning?: string | null;
}

// ---------------------------------------------------------------------------
// Duplicates — wire form (from DuplicateReport.to_dict() / DuplicateGroup.to_dict())
// renames `keeper` -> `keep` and flattens to {summary, exact_duplicates,
// audio_duplicates}. The Python dataclass shape is different; keep the wire
// form hand-written.
// ---------------------------------------------------------------------------

export interface DuplicateGroup {
  method: "md5" | "chromaprint";
  key: string;
  keep: FileInfo;
  duplicates: FileInfo[];
  recoverable_mb: number;
}

export interface DuplicateReport {
  summary: {
    total_files: number;
    exact_duplicate_groups: number;
    exact_duplicate_files: number;
    audio_duplicate_groups: number;
    audio_duplicate_files: number;
    total_duplicates: number;
    space_recoverable_mb: number;
    /** Detection phases that actually ran ("md5", "chromaprint"). */
    phases_run?: string[];
    /** False only when audio fingerprinting was requested but the fingerprint
     *  tool couldn't be made available (PATH/staged missed AND auto-provision
     *  failed or was skipped), so that phase never ran — the view banners it. */
    fpcalc_available?: boolean;
    /** When `fpcalc_available` is false, a short plain-user reason the audio
     *  fingerprint tool couldn't be set up (e.g. "the download didn't
     *  complete…"). Drives the failure banner; the retry is automatic next scan. */
    fpcalc_error?: string | null;
  };
  exact_duplicates: DuplicateGroup[];
  audio_duplicates: DuplicateGroup[];
}

// ---------------------------------------------------------------------------
// View state — pure UI types, no Python equivalent.
// ---------------------------------------------------------------------------

export type ViewMode = "library" | "duplicates" | "organize" | "tags" | "settings";

export interface ProgressEvent {
  current: number;
  total: number;
  message: string;
  /** Operation kind stamped by the sidecar while a cancellable op runs
   *  (e.g. "analyze", "install-essentia"). Absent on legacy frames. */
  kind?: string;
  /** Client-generated correlation id echoed back by the sidecar — the value
   *  returned by useOperationStore.begin() and passed to the api wrapper.
   *  Absent when the op wasn't started with one (CLI, legacy sidecar). */
  op_id?: string;
}

export interface ReadyEvent {
  version: string;
  methods: string[];
}

export interface InstallResult {
  ok: boolean;
  error?: string;
  distro?: string;
  note?: string;
  tail?: string;
  stderr?: string;
}
