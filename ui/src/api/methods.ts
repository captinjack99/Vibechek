/**
 * RPC method registry — parameter types and the canonical method-name list.
 *
 * Every method in `vibechek/rpc.py:METHODS` MUST have:
 *   (a) an entry in {@link RPC_METHODS} below, and
 *   (b) a typed wrapper exported from {@link ./rpc}.
 *
 * The `RPC_METHODS` const array doubles as a runtime/typo guard — tests use it
 * to verify the Python and TS sides stay in sync, and future audits can use it
 * to confirm "no component is calling an unknown method string".
 *
 * When adding a new RPC:
 *   1. Add the Python handler + register it in `vibechek/rpc.py:METHODS`.
 *   2. Append the method name to `RPC_METHODS` here.
 *   3. Add (or reuse) a param interface in this file.
 *   4. Export a typed wrapper from `./rpc.ts`.
 *
 * Param interfaces deliberately mirror the keys read by the Python handlers
 * (see the `params.get(...)` calls in each handler). Optional keys are
 * marked `?` so call sites can omit them; required keys must be present.
 */

import type {
  AnalysisReport,
  DuplicateReport,
  VibechekConfig,
} from "../types";

// Re-export generated payload types so callers can `import { ... } from "../api/methods"`
// for both the params and the result shape in one place.
export type {
  AnalysisReport,
  BackupHistory,
  BackupRecord,
  DuplicateReport,
  EngineGpuInfo,
  LibraryState,
  NativeVenvStatus,
  OrganizePlan,
  OrganizeStats,
  PreflightResult,
  SystemResources,
  VibechekConfig,
  WSLStatus,
} from "../types";

// ---------------------------------------------------------------------------
// Canonical RPC method list — KEEP IN SYNC with vibechek/rpc.py:METHODS.
// Tests assert this array contains every Python-side handler name.
// ---------------------------------------------------------------------------

export const RPC_METHODS = [
  // diagnostics
  "ping",
  "version",
  "system_info",
  "engine_gpu_status",
  "preflight",
  "wsl_status",
  "native_venv_status",
  // install
  "install_wsl",
  "install_vibechek_in_wsl",
  "upgrade_vibechek_in_wsl",
  "install_cuda_libs_in_wsl",
  "install_essentia_native",
  "repair_wsl_shim",
  // analysis
  "scan_directory",
  "scan_only",
  "analyze_directory",
  // duplicates
  "find_duplicates",
  "handle_duplicates",
  // organize
  "plan_organization",
  "organize",
  // tags
  "apply_ml_tags",
  "backup_tags",
  "restore_tags",
  "restore_tags_with_remap",
  // models
  "download_models",
  "verify_models",
  // config
  "get_config",
  "save_config",
  "restore_default_config",
  // profiles
  "list_profiles",
  "load_profile",
  // ops
  "cancel_operation",
  // library state
  "library_state",
  "forget_library",
  "load_recent_analysis",
  "rename_library",
  "tag_library",
  "count_new_tracks",
  // logging / history
  "get_log_tail",
  "backup_history",
  "forget_backup",
  // undo journals
  "list_journals",
  "revert_journal",
  // diagnostics
  "doctor",
] as const;

export type RpcMethodName = (typeof RPC_METHODS)[number];

/** True iff `name` is a known RPC method. Useful for typo checks at boundaries. */
export function isKnownRpcMethod(name: string): name is RpcMethodName {
  return (RPC_METHODS as readonly string[]).includes(name);
}

// ---------------------------------------------------------------------------
// Param shapes
//
// Each interface mirrors the keys read by the corresponding Python handler in
// vibechek/rpc.py. Required vs optional follows the handler's own
// `params["..."]` (required) vs `params.get("...", default)` (optional).
// ---------------------------------------------------------------------------

// --- diagnostics ---

export interface EngineGpuStatusRequest {
  distro?: string | null;
  /** Force a fresh probe, bypassing the 5-min cache. */
  force?: boolean;
}

export interface PreflightRequest {
  models_dir?: string | null;
  /** Default true. Pass false after an install for an accurate per-distro probe. */
  quick?: boolean;
}

export interface WSLStatusRequest {
  /** Default false. Set true to skip per-distro vibechek/essentia probes. */
  quick?: boolean;
}

// --- install ---

export interface InstallWSLRequest {
  /** Defaults to "Ubuntu-24.04" on the server. */
  distro?: string;
}

export interface InstallVibechekInWSLRequest {
  distro: string;
}

export interface UpgradeVibechekInWSLRequest {
  distro: string;
}

export interface InstallCudaLibsInWSLRequest {
  distro: string;
  /** Subset of `engine_gpu_status().missing_cuda_libs` to install. */
  missing_libs?: string[];
}

export interface RepairWSLShimRequest {
  distro: string;
}

// --- analysis ---

export interface ScanDirectoryRequest {
  path: string;
  /** Default true. */
  recursive?: boolean;
}

export interface ScanDirectoryResult {
  count: number;
  files: Array<{
    path: string;
    filename: string;
    extension: string;
    size_mb: number;
  }>;
}

export interface ScanOnlyRequest {
  path: string;
  /** Default true. */
  recursive?: boolean;
}

export interface AnalyzeDirectoryRequest {
  path: string;
  /** 0 means "auto" — the server picks based on CPU count. */
  workers?: number;
  /** "auto" | "always" | "never". */
  use_gpu?: string;
  /** Run GPU + CPU workers together (default true; ignored when use_gpu=off). */
  hybrid_cpu_gpu?: boolean;
  /** Override the configured models directory. */
  models_dir?: string;
  /** Where to persist the resulting JSON; defaults to the recents folder. */
  output_path?: string;
  /** Skip the first N files (CLI debugging knob). */
  skip?: number;
  /** Cap the run at N files (CLI debugging knob). */
  limit?: number | null;
  /** Absolute paths already analyzed — for incremental "analyze new only" runs. */
  skip_paths?: string[];
  /** Default true. Set false for one-off CLI runs. */
  auto_save?: boolean;
}

// --- duplicates ---

export interface FindDuplicatesRequest {
  path: string;
  use_md5?: boolean;
  use_chromaprint?: boolean;
  /** Chromaprint similarity threshold, 0..1. */
  threshold?: number;
  /** "report" | "move" | "trash". */
  action?: string;
  review_folder?: string | null;
  /** Default true. Pass false to skip the per-file mutagen probe. */
  read_metadata?: boolean;
}

export interface HandleDuplicatesRequest {
  /** Full DuplicateReport object as returned by `find_duplicates`. */
  report: DuplicateReport;
  /** "report" | "move" | "trash". */
  action?: string;
  review_folder?: string | null;
}

// --- organize ---

/**
 * The plan/organize/apply_ml_tags handlers all take the analysis payload one
 * of two ways: inline as `analysis` or by path as `analysis_path`. Exactly
 * one must be present.
 */
export type AnalysisPayloadParam =
  | { analysis: Pick<AnalysisReport, "tracks"> | AnalysisReport | { tracks: unknown[] } }
  | { analysis_path: string };

export type PlanOrganizationRequest = AnalysisPayloadParam & {
  use_subgenres?: boolean;
  min_genre_size?: number;
  target_root?: string | null;
};

export type OrganizeRequest = PlanOrganizationRequest & {
  /** Default false. */
  dry_run?: boolean;
};

// --- tags ---

export type ApplyMlTagsRequest = AnalysisPayloadParam & {
  /** Genre confidence threshold, 0..1. */
  confidence?: number;
  parent_genre_confidence_threshold?: number;
  /** Per-field write toggles. `skip_bpm_and_key` is still accepted by the
   *  server for back-compat but the GUI sends the explicit toggles. */
  write_genre?: boolean;
  write_bpm?: boolean;
  write_key?: boolean;
  write_energy?: boolean;
  write_mood?: boolean;
  write_timeslot?: boolean;
  write_direction?: boolean;
  write_vocal?: boolean;
  skip_bpm_and_key?: boolean;
  /** Vocal classification cutoffs (voice probability 0..1). */
  vocal_instrumental_max?: number;
  vocal_full_min?: number;
  preserve_rekordbox_frames?: boolean;
  /** 3 = UTF-8 (ID3v2.4 default), 1 = UTF-16, 0 = ISO-8859-1. */
  id3_text_encoding?: number;
  /** Default false. */
  dry_run?: boolean;
};

export interface BackupTagsRequest {
  path: string;
  output_path: string;
}

export interface RestoreTagsRequest {
  backup_path: string;
}

export interface RestoreTagsWithRemapRequest {
  backup_path: string;
  library_root: string;
}

/** Shared shape for the four tag-related result payloads (dataclass.asdict). */
export interface TagApplyStats {
  total: number;
  genre_applied: number;
  genre_skipped_low_confidence: number;
  other_tags_applied: number;
  errors: string[];
}

export interface TagBackupStats {
  total: number;
  backed_up: number;
  errors: string[];
}

export interface TagRestoreStats {
  total: number;
  restored: number;
  skipped_missing: number;
  errors: string[];
}

export interface TagRemapRestoreStats {
  total: number;
  restored: number;
  skipped_missing: number;
  skipped_size_mismatch: number;
  matched_exact: number;
  matched_filename_size: number;
  matched_filename: number;
  errors: string[];
  matches: Array<{
    original: string;
    matched: string | null;
    strategy: string | null;
    error?: string | null;
    substrategy?: string;
  }>;
}

// --- models ---

export interface DownloadModelsRequest {
  models_dir?: string;
}

export interface DownloadModelsResult {
  models_dir: string;
  models: string[];
}

// --- config ---

export interface SaveConfigRequest {
  config: VibechekConfig;
}

export interface SaveConfigResult {
  saved_to: string;
}

export interface RestoreDefaultConfigResult {
  saved_to: string;
  config: VibechekConfig;
}

// --- ops ---

/** `cancel_operation` returns the kind that was running, or null. */
export interface CancelOperationResult {
  cancelled: string | null;
}

// --- library state ---

export interface ForgetLibraryRequest {
  path: string;
}

export interface ForgetLibraryResult {
  removed: boolean;
}

export type LoadRecentAnalysisRequest =
  | { library_path: string }
  | { analysis_path: string };

export type LoadRecentAnalysisResult =
  | { loaded: true; report: AnalysisReport }
  | { loaded: false; reason: string };

// --- logging / history ---

export interface GetLogTailRequest {
  /** Default 200. */
  n?: number;
}

export interface GetLogTailResult {
  log_file: string;
  lines: string[];
}

export interface ForgetBackupRequest {
  backup_path: string;
}

export interface ForgetBackupResult {
  removed: boolean;
}

// --- multi-library management ---

export interface RenameLibraryRequest {
  path: string;
  name: string;
}

export interface TagLibraryRequest {
  path: string;
  tags: string[];
}

/** Both rename_library and tag_library return this shape. */
export interface LibraryMutationResult {
  /** renamed/tagged true iff the path was found in the recent list. */
  renamed?: boolean;
  tagged?: boolean;
  record?: unknown;
}

export interface CountNewTracksRequest {
  library_path: string;
}

export interface CountNewTracksResult {
  new_count: number;
  total_count: number;
  analyzed_count: number;
}

// --- profiles ---

export interface LoadProfileRequest {
  name: string;
}

export interface ListProfilesResult {
  profiles: Array<{
    name: string;
    label?: string;
    description?: string;
    [k: string]: unknown;
  }>;
}

export interface LoadProfileResult {
  loaded?: boolean;
  saved_to?: string;
  applied?: unknown;
  config?: VibechekConfig;
  [k: string]: unknown;
}

// --- undo journals ---

export interface JournalSummary {
  path: string;
  /** "organize" | "dedupe_move" | "dedupe_trash". */
  kind: string;
  started_at: number | null;
  root: string | null;
  move_count: number;
  trash_count: number;
}

export interface ListJournalsRequest {
  /** Max journals to return (default 50). */
  limit?: number;
}

export interface ListJournalsResult {
  journals: JournalSummary[];
}

export interface RevertJournalRequest {
  journal_path: string;
}

export interface RevertJournalResult {
  reverted: number;
  skipped: number;
  errors: number;
  /** Trash entries can't be auto-restored — count of files the user must
   * restore manually from the OS recycle bin. */
  trashed_not_reverted: number;
  error_messages: string[];
}

// --- diagnostics / models ---

export interface DoctorResult {
  markdown: string;
}

export interface VerifyModelsResult {
  results: Array<{
    name: string;
    suffix: string;
    /** true=match, false=mismatch/missing, null=no pin yet. */
    ok: boolean | null;
    expected?: string;
    computed?: string;
    reason?: string;
  }>;
}

// --- install result shape (shared by all install_* handlers) ---

export interface InstallResultPayload {
  ok: boolean;
  error?: string;
  distro?: string;
  note?: string;
  tail?: string;
  stderr?: string;
  /** Per-handler extras (e.g. version strings) — pass through untouched. */
  [k: string]: unknown;
}

// --- ping / version ---

export interface PingResult {
  pong: boolean;
  version: string;
}

export interface VersionResult {
  version: string;
}
