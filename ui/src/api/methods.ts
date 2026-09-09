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
  ApplyStats,
  BackupStats,
  DuplicateReport,
  RemapRestoreStats,
  RestoreStats,
  VibechekConfig,
} from "../types";

// Re-export generated payload types so callers can `import { ... } from "../api/methods"`
// for both the params and the result shape in one place.
export type {
  AnalysisReport,
  ApplyStats,
  BackupHistory,
  BackupRecord,
  BackupStats,
  DuplicateReport,
  EngineGpuInfo,
  LibraryState,
  NativeVenvStatus,
  OrganizePlan,
  OrganizeStats,
  PreflightResult,
  RemapRestoreStats,
  RestoreStats,
  SystemResources,
  VibechekConfig,
  WorkerBudget,
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
  "worker_budget",
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
  "increase_wsl_memory",
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
  "prune_empty_folders",
  // tags
  "apply_ml_tags",
  "backup_tags",
  "restore_tags",
  "restore_tags_with_remap",
  // models
  "download_models",
  "setup_onnx_engine",
  "setup_clap_engine",
  "setup_genre_resolver",
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
  "resolve_genre_conflicts",
  "import_tag_priors",
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

/** Compute the worker plan (slider max + per-worker RAM) for the given engine +
 *  genre classifier. For WSL-routed engines pass the usable `distro` so the VM's
 *  RAM (the real ceiling) is measured instead of the host total. */
export interface WorkerBudgetRequest {
  /** "essentia_tf" | "onnx" | "native". */
  engine?: string;
  /** "discogs" | "clap" — CLAP's ~4.5 GB/worker is what shrinks the max. */
  genre_classifier?: string;
  /** The user's current slider value; 0 = auto. */
  workers?: number;
  /** Usable WSL distro (from preflight) so the VM RAM pool is measured. */
  distro?: string | null;
  /**
   * The loaded library's root, when there is one. The run sizes per-worker RAM
   * from the longest track it finds under this path (a 90-minute set holds far
   * more decoded audio than a library of singles), so without it the slider
   * reports the flat-budget maximum while the run plans fewer workers — the
   * exact slider/run divergence this shared budget model exists to prevent.
   * Omitted when no library is loaded; the backend then falls back to the flat
   * budget.
   */
  library_path?: string;
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
  /** Which engine's venv to repair ("essentia_tf" | "onnx"). Defaults to
   *  essentia_tf server-side; the GUI passes the selected engine so an
   *  out-of-date ONNX install can be fixed from the button too. */
  inference_engine?: string;
}

export interface InstallCudaLibsInWSLRequest {
  distro: string;
  /** Subset of `engine_gpu_status().missing_cuda_libs` to install. */
  missing_libs?: string[];
}

/** Approve (accept the reconciled genre) or revert (back to the file tag) a set
 *  of reviewed genre conflicts; clears `ml_genre_conflict` and persists. */
export interface ResolveGenreConflictsRequest {
  library_path: string;
  items: { path: string; action: "approve" | "revert" }[];
}

/** Import a Rekordbox collection XML as tag-tier priors: genre supersedes at
 *  the tag tier (marked `genre_origin="rekordbox"`), key/MIK-energy only fill
 *  gaps, then the matched records re-reconcile and persist. Never writes file
 *  tags; idempotent (re-importing the same XML is safe). */
export interface ImportTagPriorsRequest {
  library_path: string;
  xml_path: string;
  /** Existing-tag vs ML genre reconciliation policy (server default if omitted). */
  genre_source_policy?: string;
  /** prefer_tag: min ML confidence to override a disagreeing specific tag. */
  genre_ml_override_confidence?: number;
}

export interface RepairWSLShimRequest {
  distro: string;
}

/** Result of `increase_wsl_memory` — the `.wslconfig` memory self-heal (WP-D2).
 *  The RPC reads and bumps the `memory=` line of the user's `%USERPROFILE%\
 *  .wslconfig` (preserving all other content, never shrinking an already-larger
 *  value). It NEVER restarts WSL — a running analysis or the user's other WSL
 *  work could die — so `restart_required` tells the GUI to offer the restart
 *  knowingly. On failure it carries the shared error-envelope fields
 *  (`headline`/`detail`/`kind`). Takes no params. */
export interface IncreaseWslMemoryResult {
  ok: boolean;
  /** True only when the file was actually rewritten to a higher limit. */
  changed: boolean;
  /** Previous / new `memory=` values (e.g. "8GB" → "24GB"). Absent on the
   *  earliest failure paths (couldn't measure host RAM). */
  old?: string | null;
  new?: string | null;
  old_mb?: number | null;
  new_mb?: number | null;
  /** True after a real bump — the raised limit only takes effect once the Linux
   *  analysis environment (or Windows) restarts. */
  restart_required: boolean;
  /** The `.wslconfig` path that was read/written. */
  path?: string;
  /** Set when nothing changed because the limit was already high enough. */
  message?: string;
  /** Shared error-envelope fields, present only on the failure paths. */
  headline?: string;
  detail?: string;
  kind?: string;
  error?: string;
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
    /** Set when the file was found by the walk but could not be stat'd (moved
     *  or locked between the walk and the size pass, permissions). The entry is
     *  still counted, with `size_mb` left at 0 — see `_scan_directory` in
     *  vibechek/rpc.py. Callers that summarise a scan must surface it; a
     *  silently-zero size reads as a 0-byte file. */
    error?: string;
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
  /** Inference engine: "essentia_tf" | "onnx". */
  inference_engine?: string;
  /** Existing-tag vs ML genre reconciliation policy. */
  genre_source_policy?: string;
  /** Audio genre model: "discogs" | "clap". */
  genre_classifier?: string;
  /** Look the genre up online (reads catalog pages' genre field). */
  genre_web_lookup?: boolean;
  /** Deprecated, no effect: the online lookup uses no model. Still sent so
   *  configs written by older versions round-trip unchanged. */
  genre_llm_backend?: string;
  /** prefer_tag: min ML confidence to override a disagreeing specific tag. */
  genre_ml_override_confidence?: number;
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
  /** Keep Extended/Radio/Remix etc. as distinct versions (default true). */
  keep_distinct_versions?: boolean;
  /** Keep the best file of EVERY format within a version (default false). */
  keep_all_formats?: boolean;
  /** Duration tolerance for the same-version check, 0..1 (default 0.12). */
  version_duration_tolerance?: number;
}

export interface HandleDuplicatesRequest {
  /** Full DuplicateReport object as returned by `find_duplicates`. */
  report: DuplicateReport;
  /** "report" | "move" | "trash". */
  action?: string;
  review_folder?: string | null;
  /**
   * The library whose SAVED analysis the sidecar should sync after acting
   * (`vibechek/rpc.py::_prune_analysis_after_dedupe`). Send the loaded library
   * path whenever there is one: without it the sidecar has to INFER the library
   * by testing whether a recents entry is an ancestor of the acted-on files,
   * which is a guess — it misses a library recorded under a different spelling
   * of the same folder, and can touch a second, nested library's analysis too.
   */
  library_path?: string;
}

/**
 * What `handle_duplicates` returns.
 *
 * Typed as a bare `Record<string, number>` until now, which structurally HID
 * every non-numeric key the handler has always sent: the per-file failure list,
 * the journal path, the cancellation flag. The index signature stays (the
 * counters are open-ended: `moved`, `deleted`, `errors`, `skipped`…) but every
 * key the UI actually reads is declared.
 */
export interface HandleDuplicatesResult extends Record<string, unknown> {
  /** Files moved to the review folder (0 for a trash run). */
  moved?: number;
  /** Files sent to the OS trash (0 for a move run). */
  deleted?: number;
  /** Count of files the sidecar could not act on. */
  errors?: number;
  /** One line per failure, formatted "<path>: reason". */
  error_messages?: string[];
  /** Undo journal for a move run (absent for trash — the OS bin is the undo). */
  journal_path?: string | null;
  /** `_handle_duplicates` RESOLVES rather than rejects on cancellation, so a
   *  user-cancelled partial batch arrives as a normal payload with this set. */
  cancelled?: boolean;
  /** The sidecar could not record every action in the journal, so an undo puts
   *  back only part of the run. */
  journal_incomplete?: boolean;
  /** The files actually deleted / moved, by path — authoritative, where the
   *  counts above only allow the caller to GUESS which files were touched.
   *  Optional: a sidecar that predates them sends neither. */
  deleted_paths?: string[];
  moved_pairs?: [string, string][];
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

/**
 * Remove folders an organize emptied. Separate from `organize` on purpose:
 * removing a directory is the most destructive thing the organize flow does,
 * so it only ever runs after the user confirms the list.
 */
export type PruneEmptyFoldersRequest = {
  /** Library root. Nothing outside it is ever removed, and it is never removed. */
  root: string;
  /** Normally `emptied_dirs` straight off the organize result. */
  dirs: string[];
};

export interface PruneEmptyFoldersResponse {
  removed: string[];
  /** Folders left in place, each with the reason (not empty / gone / outside root). */
  skipped: { path: string; reason: string }[];
  errors: string[];
}

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

/**
 * The four tag-related result payloads.
 *
 * These are ALIASES of the codegen'd dataclass types (types/generated.ts), not
 * hand copies. They used to be re-declared field-by-field here, which is how
 * `genre_applied_parent_only` and `not_fully_backed_up` sat on the wire for
 * releases without the UI ever reading them. `vibechek.tagger` is discovered by
 * the TS codegen now (scripts/generate_ts_types.py `all_dataclass_modules()`),
 * so drift is a compile error rather than something only review could catch.
 *
 * The `Tag*` names are kept because every call site imports them.
 */
export type TagApplyStats = ApplyStats;
export type TagBackupStats = BackupStats;
export type TagRestoreStats = RestoreStats;

/** One row of `RemapRestoreStats.matches`. The Python field is `list[dict]`, so
 *  the generator can only emit `Record<string, unknown>[]` — this is the only
 *  part of the payload the codegen cannot describe, and the renderer needs the
 *  field names. Everything else comes from the generated type. */
export interface RemapMatch {
  original: string;
  matched: string | null;
  strategy: string | null;
  error?: string | null;
  substrategy?: string;
}

export type TagRemapRestoreStats = Omit<RemapRestoreStats, "matches"> & {
  matches: RemapMatch[];
};

// --- models ---

export interface DownloadModelsRequest {
  models_dir?: string;
}

export interface DownloadModelsResult {
  models_dir: string;
  models: string[];
}

/** Optional WSL distro override for `setup_onnx_engine` (Windows only). */
export interface SetupOnnxEngineRequest {
  distro?: string;
}

/** Result of the one-click, self-healing ONNX engine setup. */
export interface SetupOnnxEngineResult {
  ok: boolean;
  ready: boolean;
  /** Bundled head files copied into <models>/onnx (e.g. "danceability.onnx"). */
  staged: string[];
  /** Where the bundled heads were staged from (a _MEIPASS dir when frozen). */
  bundle_source: string | null;
  /** Preflight reasons the engine still isn't ready (empty when ready). */
  reasons_not_ready: string[];
  /** "wsl" | "native" — how analyze will route once ready. */
  analyze_via: string;
  /** The real failure reason when a setup step (install/fetch) failed, else null. */
  error?: string | null;
  /** True when the setup was cancelled mid-run. */
  cancelled?: boolean;
}

/** Optional overrides for the opt-in genre engine setups. */
export interface SetupGenreEngineRequest {
  distro?: string;
  /** The LIVE engine selection ("essentia_tf" | "onnx") — the setup installs
   * into this engine's venv. The UI sends it because the saved config can lag
   * the selector by the autosave debounce. */
  inference_engine?: string;
}

/** Result of a one-click opt-in genre engine setup (CLAP / online lookup). */
export interface SetupGenreEngineResult {
  ok: boolean;
  ready: boolean;
  /** The real failure reason when a setup step failed, else null. */
  error?: string | null;
  /** True when the setup was cancelled mid-run. */
  cancelled?: boolean;
  /** The WSL distro the engine was set up in (Windows). */
  distro?: string;
  /** Last lines of the install log (for diagnostics). */
  tail?: string;
}

// --- config ---

export interface SaveConfigRequest {
  config: VibechekConfig;
  /**
   * Overwrite a settings file the sidecar could not READ. Without it
   * `save_config` refuses with INVALID_PARAMS rather than replace bytes it
   * couldn't parse (`vibechek/rpc.py::_save_config`); with it, `save()`
   * quarantines the unreadable file as `<name>.corrupt-<timestamp>` first.
   * `restore_default_config` is the same deliberate path with defaults as the
   * payload — prefer it unless the user explicitly wants what's on screen kept.
   */
  force?: boolean;
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
  /** [(current, restored)] — i.e. (dst, src) — for every move actually
   * undone. Feed to useLibraryStore.updateTrackPaths so the in-memory
   * library follows the files back (mirrors organize's moved_pairs). */
  reverted_pairs: Array<[string, string]>;
  /** `revert_journal` RESOLVES rather than rejects on cancellation, so a
   *  user-cancelled undo arrives as a normal payload with this set and the
   *  counts covering only the entries processed before the stop. Treat it as
   *  a PARTIAL undo: never latch "Undone", never claim success. */
  cancelled?: boolean;
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
