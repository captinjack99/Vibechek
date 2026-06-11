/**
 * Typed wrappers for every JSON-RPC method exposed by the Python sidecar.
 *
 * --- Why this exists ---
 * Before this module, every component called the untyped transport directly:
 *
 *     await rpc<AnalysisReport>("analyze_directory", { path, workers, ... });
 *
 * Two failure modes kept showing up in audits:
 *   1. Method name typos / wrong-shaped params — the second arg is `object`,
 *      so anything compiles and the error only shows up at runtime.
 *   2. Drifting between the Python handler and the TS caller — Python adds
 *      a new param, TS forgets to send it; or Python returns a renamed field
 *      and TS keeps reading the old name.
 *
 * Wrapping each method in a typed function gives us:
 *   - Compile-time checking on the param shape.
 *   - A single grep target ("api.analyzeDirectory") when a method's contract
 *     changes — no more hunting through string literals.
 *   - A canonical list (RPC_METHODS in ./methods) we can audit against the
 *     Python METHODS dict to catch missed wrappers.
 *
 * --- Adding a new RPC method ---
 *   (a) Add the handler in vibechek/rpc.py and register in METHODS.
 *   (b) Add a param interface (+ append the name to RPC_METHODS) in ./methods.
 *   (c) Add a wrapper function here.
 *
 * Components MUST NOT call the low-level `rpc(string, ...)` directly.
 * (Existing call sites will be migrated in a follow-up pass.)
 */

import { rpc } from "../hooks/useSidecar";
import type {
  AnalysisReport,
  AnalyzeDirectoryRequest,
  ApplyMlTagsRequest,
  BackupHistory,
  BackupTagsRequest,
  CancelOperationResult,
  DownloadModelsRequest,
  DownloadModelsResult,
  DuplicateReport,
  EngineGpuInfo,
  EngineGpuStatusRequest,
  FindDuplicatesRequest,
  ForgetBackupRequest,
  ForgetBackupResult,
  ForgetLibraryRequest,
  ForgetLibraryResult,
  GetLogTailRequest,
  GetLogTailResult,
  HandleDuplicatesRequest,
  InstallCudaLibsInWSLRequest,
  InstallResultPayload,
  InstallVibechekInWSLRequest,
  InstallWSLRequest,
  UpgradeVibechekInWSLRequest,
  LibraryState,
  LoadRecentAnalysisRequest,
  LoadRecentAnalysisResult,
  RenameLibraryRequest,
  TagLibraryRequest,
  LibraryMutationResult,
  CountNewTracksRequest,
  CountNewTracksResult,
  ListProfilesResult,
  LoadProfileRequest,
  LoadProfileResult,
  ListJournalsRequest,
  ListJournalsResult,
  RevertJournalRequest,
  RevertJournalResult,
  DoctorResult,
  VerifyModelsResult,
  NativeVenvStatus,
  OrganizeRequest,
  OrganizeStats,
  OrganizePlan,
  PingResult,
  PlanOrganizationRequest,
  PreflightRequest,
  PreflightResult,
  RepairWSLShimRequest,
  RestoreDefaultConfigResult,
  RestoreTagsRequest,
  RestoreTagsWithRemapRequest,
  SaveConfigRequest,
  SaveConfigResult,
  ScanDirectoryRequest,
  ScanDirectoryResult,
  ScanOnlyRequest,
  SetupOnnxEngineRequest,
  SetupOnnxEngineResult,
  SetupGenreEngineRequest,
  SetupGenreEngineResult,
  SystemResources,
  TagApplyStats,
  TagBackupStats,
  TagRemapRestoreStats,
  TagRestoreStats,
  VersionResult,
  VibechekConfig,
  WSLStatus,
  WSLStatusRequest,
} from "./methods";

/**
 * Spread a client-generated correlation id into a long-op param object.
 *
 * The sidecar strips `op_id` before the handler runs and echoes it (plus the
 * op's kind) on every progress notification the op emits, so the GUI can
 * attribute events on the shared `sidecar:progress` stream to the exact op
 * instance — see `useOperationStore.begin()` / `progressMatches()` in
 * stores/operation.ts. Cancellable wrappers below take the id as an optional
 * trailing arg; omitting it keeps the legacy unstamped behavior.
 */
function withOpId<T extends object>(params: T, opId?: string): object {
  return opId ? { ...params, op_id: opId } : params;
}

// ---------------------------------------------------------------------------
// Diagnostics
// ---------------------------------------------------------------------------

/** Liveness check. Round-trips through the sidecar and returns its version. */
export function ping(): Promise<PingResult> {
  return rpc<PingResult>("ping");
}

/** Sidecar version (matches `vibechek.__version__`). */
export function version(): Promise<VersionResult> {
  return rpc<VersionResult>("version");
}

/** Host-side CPU / memory / GPU detection. Fast (~ms). */
export function systemInfo(): Promise<SystemResources> {
  return rpc<SystemResources>("system_info");
}

/**
 * Probe the analyze engine for GPU visibility. On Windows this runs inside
 * WSL and can take ~10s the first time (cached for 5 min server-side).
 */
export function engineGpuStatus(
  params: EngineGpuStatusRequest = {},
): Promise<EngineGpuInfo> {
  return rpc<EngineGpuInfo>("engine_gpu_status", params);
}

/**
 * Check whether essentia + models are ready for `analyze_directory`.
 * Default `quick=true` is sub-second. Pass `quick:false` after an install.
 */
export function preflight(
  params: PreflightRequest = {},
): Promise<PreflightResult> {
  return rpc<PreflightResult>("preflight", params);
}

/** Detect WSL availability and per-distro vibechek/essentia install state. */
export function wslStatus(
  params: WSLStatusRequest = {},
): Promise<WSLStatus> {
  return rpc<WSLStatus>("wsl_status", params);
}

/** Detect what's installed in the managed `~/.vibechek/venv/`. */
export function nativeVenvStatus(): Promise<NativeVenvStatus> {
  return rpc<NativeVenvStatus>("native_venv_status");
}

// ---------------------------------------------------------------------------
// Install — all cancellable; all return an InstallResultPayload variant.
// ---------------------------------------------------------------------------

/** Install WSL + a distro via elevated PowerShell (triggers UAC). Cancellable. */
export function installWSL(
  params: InstallWSLRequest = {},
  opId?: string,
): Promise<InstallResultPayload> {
  return rpc<InstallResultPayload>("install_wsl", withOpId(params, opId));
}

/** Install vibechek + essentia-tensorflow + chromaprint inside a WSL distro. Cancellable. */
export function installVibechekInWSL(
  params: InstallVibechekInWSLRequest,
  opId?: string,
): Promise<InstallResultPayload> {
  return rpc<InstallResultPayload>("install_vibechek_in_wsl", withOpId(params, opId));
}

/**
 * Fast-path upgrade for the WSL vibechek package only (skips apt + essentia).
 * Use this to repair version drift surfaced by the analyzer's drift guard —
 * full re-install via installVibechekInWSL is the cold-install path.
 */
export function upgradeVibechekInWSL(
  params: UpgradeVibechekInWSLRequest,
  opId?: string,
): Promise<InstallResultPayload> {
  return rpc<InstallResultPayload>("upgrade_vibechek_in_wsl", withOpId(params, opId));
}

/** Install the CUDA runtime libs essentia's bundled TF needs. Cancellable. */
export function installCudaLibsInWSL(
  params: InstallCudaLibsInWSLRequest,
  opId?: string,
): Promise<InstallResultPayload> {
  return rpc<InstallResultPayload>("install_cuda_libs_in_wsl", withOpId(params, opId));
}

/** Install essentia + vibechek into the managed native venv (Linux/macOS). Cancellable. */
export function installEssentiaNative(opId?: string): Promise<InstallResultPayload> {
  return rpc<InstallResultPayload>("install_essentia_native", withOpId({}, opId));
}

/** One-shot diagnostic repair of a broken venv shim left by older installers. */
export function repairWSLShim(
  params: RepairWSLShimRequest,
): Promise<InstallResultPayload> {
  return rpc<InstallResultPayload>("repair_wsl_shim", params);
}

// ---------------------------------------------------------------------------
// Analysis
// ---------------------------------------------------------------------------

/** Enumerate audio files under `path`. No ML; very fast. */
export function scanDirectory(
  params: ScanDirectoryRequest,
): Promise<ScanDirectoryResult> {
  return rpc<ScanDirectoryResult>("scan_directory", params);
}

/**
 * Cheap library load — filename-parsed hints + existing tags, no ML.
 * Cancellable. Returns the same AnalysisReport shape as `analyze_directory`.
 */
export function scanOnly(
  params: ScanOnlyRequest,
  opId?: string,
): Promise<AnalysisReport> {
  return rpc<AnalysisReport>("scan_only", withOpId(params, opId));
}

/**
 * Full ML analysis. Emits `sidecar:progress` notifications while running.
 * Cancellable via {@link cancelOperation}.
 */
export function analyzeDirectory(
  params: AnalyzeDirectoryRequest,
  opId?: string,
): Promise<AnalysisReport> {
  return rpc<AnalysisReport>("analyze_directory", withOpId(params, opId));
}

// ---------------------------------------------------------------------------
// Duplicates
// ---------------------------------------------------------------------------

/**
 * Find exact + audio-fingerprint duplicates under `path`. Cancellable.
 * Returns the wire-form `DuplicateReport` (see types/index.ts).
 */
export function findDuplicates(
  params: FindDuplicatesRequest,
  opId?: string,
): Promise<DuplicateReport> {
  return rpc<DuplicateReport>("find_duplicates", withOpId(params, opId));
}

/**
 * Apply the configured action (report/delete/move) to a previously-computed
 * `DuplicateReport`. Returns a per-action stats dict.
 */
export function handleDuplicates(
  params: HandleDuplicatesRequest,
  opId?: string,
): Promise<Record<string, number>> {
  return rpc<Record<string, number>>("handle_duplicates", withOpId(params, opId));
}

// ---------------------------------------------------------------------------
// Organize
// ---------------------------------------------------------------------------

/** Compute (but don't execute) an organize plan from an analysis. */
export function planOrganization(
  params: PlanOrganizationRequest,
): Promise<OrganizePlan> {
  return rpc<OrganizePlan>("plan_organization", params);
}

/** Execute the moves for an organize plan. Cancellable. */
export function organize(
  params: OrganizeRequest,
  opId?: string,
): Promise<OrganizeStats> {
  return rpc<OrganizeStats>("organize", withOpId(params, opId));
}

// ---------------------------------------------------------------------------
// Tags
// ---------------------------------------------------------------------------

/** Apply ML-derived tags to the on-disk files. Cancellable. */
export function applyMlTags(
  params: ApplyMlTagsRequest,
  opId?: string,
): Promise<TagApplyStats> {
  return rpc<TagApplyStats>("apply_ml_tags", withOpId(params, opId));
}

/** Snapshot all on-disk tags under `path` into a JSON backup file. Cancellable. */
export function backupTags(
  params: BackupTagsRequest,
  opId?: string,
): Promise<TagBackupStats> {
  return rpc<TagBackupStats>("backup_tags", withOpId(params, opId));
}

/** Replay a tag backup verbatim. Cancellable. */
export function restoreTags(
  params: RestoreTagsRequest,
  opId?: string,
): Promise<TagRestoreStats> {
  return rpc<TagRestoreStats>("restore_tags", withOpId(params, opId));
}

/**
 * Restore a backup against a possibly-moved library — the tagger re-matches
 * each backup entry to its current on-disk file. Cancellable.
 */
export function restoreTagsWithRemap(
  params: RestoreTagsWithRemapRequest,
  opId?: string,
): Promise<TagRemapRestoreStats> {
  return rpc<TagRemapRestoreStats>("restore_tags_with_remap", withOpId(params, opId));
}

// ---------------------------------------------------------------------------
// Models
// ---------------------------------------------------------------------------

/** Download the essentia model weights (~200 MB). Cancellable. */
export function downloadModels(
  params: DownloadModelsRequest = {},
  opId?: string,
): Promise<DownloadModelsResult> {
  return rpc<DownloadModelsResult>("download_models", withOpId(params, opId));
}

/** SHA256-verify every downloaded model against the pinned table. */
export function verifyModels(): Promise<VerifyModelsResult> {
  return rpc<VerifyModelsResult>("verify_models");
}

/**
 * One-click, self-healing ONNX engine setup. Stages the bundled converted
 * heads, ensures the engine env (WSL/native) is installed, fetches the EffNet
 * backbone, and verifies — emitting `progress` notifications throughout so the
 * GUI can show a live dialog. Cancellable. Idempotent (a re-click is a no-op).
 */
export function setupOnnxEngine(
  params: SetupOnnxEngineRequest = {},
  opId?: string,
): Promise<SetupOnnxEngineResult> {
  return rpc<SetupOnnxEngineResult>("setup_onnx_engine", withOpId(params, opId));
}

/**
 * One-click setup for the pure-audio CLAP genre student: installs torch +
 * laion-clap into the analysis venv and downloads the ~2.2 GB checkpoint.
 * Cancellable; emits `progress`.
 */
export function setupClapEngine(
  params: SetupGenreEngineRequest = {},
  opId?: string,
): Promise<SetupGenreEngineResult> {
  return rpc<SetupGenreEngineResult>("setup_clap_engine", withOpId(params, opId));
}

/**
 * One-click setup for the online web-synthesis genre resolver: installs ddgs +
 * a local Ollama and pulls the model. Cancellable; emits `progress`.
 */
export function setupGenreResolver(
  params: SetupGenreEngineRequest = {},
  opId?: string,
): Promise<SetupGenreEngineResult> {
  return rpc<SetupGenreEngineResult>("setup_genre_resolver", withOpId(params, opId));
}

// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------

/** Load the on-disk VibechekConfig (or the defaults if no file exists yet). */
export function getConfig(): Promise<VibechekConfig> {
  return rpc<VibechekConfig>("get_config");
}

/** Persist a VibechekConfig to disk. */
export function saveConfig(
  params: SaveConfigRequest,
): Promise<SaveConfigResult> {
  return rpc<SaveConfigResult>("save_config", params);
}

/** Reset config to defaults and persist. */
export function restoreDefaultConfig(): Promise<RestoreDefaultConfigResult> {
  return rpc<RestoreDefaultConfigResult>("restore_default_config");
}

// ---------------------------------------------------------------------------
// Profiles
// ---------------------------------------------------------------------------

/** List the built-in DJ profiles (house-dj, edm-festival, ...). */
export function listProfiles(): Promise<ListProfilesResult> {
  return rpc<ListProfilesResult>("list_profiles");
}

/** Apply a built-in profile to the config + persist. */
export function loadProfile(
  params: LoadProfileRequest,
): Promise<LoadProfileResult> {
  return rpc<LoadProfileResult>("load_profile", params);
}

// ---------------------------------------------------------------------------
// Operation control
// ---------------------------------------------------------------------------

/** Request cancellation of the currently-running long operation. */
export function cancelOperation(): Promise<CancelOperationResult> {
  return rpc<CancelOperationResult>("cancel_operation");
}

// ---------------------------------------------------------------------------
// Library state
// ---------------------------------------------------------------------------

/** List the recent-libraries the user has opened. */
export function libraryState(): Promise<LibraryState> {
  return rpc<LibraryState>("library_state");
}

/** Drop a library from the recent list. */
export function forgetLibrary(
  params: ForgetLibraryRequest,
): Promise<ForgetLibraryResult> {
  return rpc<ForgetLibraryResult>("forget_library", params);
}

/** Load a saved analysis JSON, either by library_path or analysis_path. */
export function loadRecentAnalysis(
  params: LoadRecentAnalysisRequest,
): Promise<LoadRecentAnalysisResult> {
  return rpc<LoadRecentAnalysisResult>("load_recent_analysis", params);
}

/** Set a friendly display name on a recent library. */
export function renameLibrary(
  params: RenameLibraryRequest,
): Promise<LibraryMutationResult> {
  return rpc<LibraryMutationResult>("rename_library", params);
}

/** Replace the user-assigned tag list on a recent library. */
export function tagLibrary(
  params: TagLibraryRequest,
): Promise<LibraryMutationResult> {
  return rpc<LibraryMutationResult>("tag_library", params);
}

/** Count audio files on disk not yet in the saved analysis (cheap; no ML). */
export function countNewTracks(
  params: CountNewTracksRequest,
): Promise<CountNewTracksResult> {
  return rpc<CountNewTracksResult>("count_new_tracks", params);
}

// ---------------------------------------------------------------------------
// Logging / history
// ---------------------------------------------------------------------------

/** Fetch the last `n` lines of the sidecar log (default 200). */
export function getLogTail(
  params: GetLogTailRequest = {},
): Promise<GetLogTailResult> {
  return rpc<GetLogTailResult>("get_log_tail", params);
}

/** List every tag backup the user has made via Vibechek. */
export function backupHistory(): Promise<BackupHistory> {
  return rpc<BackupHistory>("backup_history");
}

/** Drop a backup from the history (does not delete the file). */
export function forgetBackup(
  params: ForgetBackupRequest,
): Promise<ForgetBackupResult> {
  return rpc<ForgetBackupResult>("forget_backup", params);
}

// ---------------------------------------------------------------------------
// Undo journals
// ---------------------------------------------------------------------------

/** List recent operation journals (organize / dedupe) for the Undo UI. */
export function listJournals(
  params: ListJournalsRequest = {},
): Promise<ListJournalsResult> {
  return rpc<ListJournalsResult>("list_journals", params);
}

/** Reverse the moves recorded in a journal. Cancellable. */
export function revertJournal(
  params: RevertJournalRequest,
  opId?: string,
): Promise<RevertJournalResult> {
  return rpc<RevertJournalResult>("revert_journal", withOpId(params, opId));
}

// ---------------------------------------------------------------------------
// Diagnostics
// ---------------------------------------------------------------------------

/** Render the `vibechek doctor` diagnostic report as paste-friendly markdown. */
export function doctor(): Promise<DoctorResult> {
  return rpc<DoctorResult>("doctor");
}

// ---------------------------------------------------------------------------
// Convenience: namespace import for call sites that prefer `api.xxx`.
// ---------------------------------------------------------------------------

/**
 * Default namespace export — lets call sites write:
 *
 *     import api from "../api/rpc";
 *     await api.analyzeDirectory({ path });
 *
 * Equivalent to named imports; pick whichever style suits the file.
 */
const api = {
  // diagnostics
  ping,
  version,
  systemInfo,
  engineGpuStatus,
  preflight,
  wslStatus,
  nativeVenvStatus,
  // install
  installWSL,
  installVibechekInWSL,
  upgradeVibechekInWSL,
  installCudaLibsInWSL,
  installEssentiaNative,
  repairWSLShim,
  // analysis
  scanDirectory,
  scanOnly,
  analyzeDirectory,
  // duplicates
  findDuplicates,
  handleDuplicates,
  // organize
  planOrganization,
  organize,
  // tags
  applyMlTags,
  backupTags,
  restoreTags,
  restoreTagsWithRemap,
  // models
  downloadModels,
  verifyModels,
  setupOnnxEngine,
  setupClapEngine,
  setupGenreResolver,
  // config
  getConfig,
  saveConfig,
  restoreDefaultConfig,
  // profiles
  listProfiles,
  loadProfile,
  // ops
  cancelOperation,
  // library state
  libraryState,
  forgetLibrary,
  loadRecentAnalysis,
  renameLibrary,
  tagLibrary,
  countNewTracks,
  // logging / history
  getLogTail,
  backupHistory,
  forgetBackup,
  // undo journals
  listJournals,
  revertJournal,
  // diagnostics
  doctor,
};

export default api;
