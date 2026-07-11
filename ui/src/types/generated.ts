// AUTO-GENERATED — do not edit. Run scripts/generate_ts_types.py to regenerate.
//
// Source of truth: the Python dataclasses in vibechek/. Field types come from
// dataclasses.fields() + typing.get_type_hints(); @property fields are listed
// explicitly in the generator. Re-run the script after touching any source
// dataclass.


// External types — declared in the hand-written shim (./index.ts).
// These permissive stubs let generated.ts type-check standalone; the
// shim re-declares each with its real shape and shadows the re-export.
export interface ExistingTags { [key: string]: unknown; }

export interface GpuDevice {
  name: string;
  backend: string;
  memory_mb: number | null;
  vendor: string;
  device_kind: string;
  accelerated_by_vibechek: boolean;
  unsupported_reason: string | null;
}

export interface SystemResources {
  platform: string;
  cpu_count: number;
  memory_total_mb: number | null;
  memory_available_mb: number | null;
  gpu_available: boolean;
  gpu_devices: GpuDevice[];
  cuda_runtime: string | null;
  accelerated_gpu_count: number;
  unsupported_gpu_count: number;
  readonly recommended_workers: number;
}

export interface WorkerBudget {
  max_workers: number;
  effective_workers: number;
  per_worker_mb: number;
  ram_seen_mb: number;
  reserve_mb: number;
  gpu_workers: number;
  cpu_workers: number;
  cap_reason: string | null;
  gpu_reason: string | null;
  refusal_reason: string | null;
  ram_pool: string;
  requested_workers: number;
  ram_measured: boolean;
}

export interface AnalysisConfig {
  workers: number;
  models_dir: string;
  use_gpu: string;
  hybrid_cpu_gpu: boolean;
  inference_engine: string;
  genre_source_policy: string;
  genre_ml_override_confidence: number;
  genre_classifier: string;
  genre_web_lookup: boolean;
  genre_llm_backend: string;
}

export interface DuplicateConfig {
  use_md5: boolean;
  use_chromaprint: boolean;
  chromaprint_similarity_threshold: number;
  action: string;
  review_folder: string | null;
  keep_distinct_versions: boolean;
  keep_all_formats: boolean;
  version_duration_tolerance: number;
}

export interface OrganizationConfig {
  use_subgenres: boolean;
  min_genre_size: number;
  target_root: string | null;
}

export interface TaggingConfig {
  genre_confidence_threshold: number;
  parent_genre_confidence_threshold: number;
  write_subgenre_as_main_genre: boolean;
  preserve_rekordbox_frames: boolean;
  backup_before_write: boolean;
  write_genre: boolean;
  write_bpm: boolean;
  write_key: boolean;
  write_energy: boolean;
  write_mood: boolean;
  write_timeslot: boolean;
  write_direction: boolean;
  write_vocal: boolean;
  vocal_instrumental_max: number;
  vocal_full_min: number;
  id3_text_encoding: number;
}

export interface UIConfig {
  seen_onboarding: boolean;
}

export interface VibechekConfig {
  analysis: AnalysisConfig;
  tagging: TaggingConfig;
  duplicates: DuplicateConfig;
  organization: OrganizationConfig;
  ui: UIConfig;
}

export interface DistroInfo {
  name: string;
  version: string | null;
  state: string;
  is_default: boolean;
  vibechek_installed: boolean;
  essentia_installed: boolean;
  vibechek_path: string | null;
  vibechek_version: string | null;
}

export interface EngineGpuDevice {
  name: string;
  backend: string;
  compute_capability: string | null;
  memory_mb: number | null;
  vendor: string;
}

export interface EngineGpuInfo {
  engine: string;
  distro: string | null;
  ok: boolean;
  gpu_available: boolean;
  gpu_count: number;
  devices: EngineGpuDevice[];
  gpu_hardware_visible: boolean;
  missing_cuda_libs: string[];
  tf_version: string | null;
  tf_built_with_cuda: boolean | null;
  nvidia_driver: string | null;
  nvidia_smi_available: boolean;
  error: string | null;
  probed_at: number;
  provider: string | null;
  runtime: string | null;
  note: string | null;
}

export interface WSLStatus {
  is_windows: boolean;
  wsl_available: boolean;
  wsl_feature_enabled: boolean;
  distros: DistroInfo[];
  default_distro: string | null;
  recommended_distro: string | null;
  error: string | null;
  readonly can_run_vibechek: boolean;
  readonly usable_distro: string | null;
}

export interface NativeVenvStatus {
  supported: boolean;
  venv_dir: string;
  venv_python: string | null;
  venv_vibechek: string | null;
  essentia_installed: boolean;
  essentia_version: string | null;
  vibechek_installed: boolean;
  vibechek_version: string | null;
  error: string | null;
}

export interface EssentiaCheck {
  installed: boolean;
  version: string | null;
  error: string | null;
}

export interface ModelCheck {
  name: string;
  present: boolean;
  weights_path: string;
  metadata_path: string;
  size_mb: number;
}

export interface ModelsCheck {
  models_dir: string;
  found: string[];
  missing: string[];
  total_size_mb: number;
  per_model: ModelCheck[];
}

export interface PreflightResult {
  ready: boolean;
  essentia: EssentiaCheck;
  models: ModelsCheck;
  platform: string;
  wsl: WSLStatus | null;
  native_venv: NativeVenvStatus | null;
  analyze_via: string | null;
  engine: string;
  essentia_usable: boolean;
  readonly reasons_not_ready: string[];
}

export interface MLResult {
  ml_genre: string | null;
  ml_subgenre: string | null;
  ml_genre_confidence: number | null;
  ml_genre_raw_confidence: number | null;
  ml_bpm: number | null;
  ml_key: string | null;
  ml_energy: number | null;
  ml_mood: string | null;
  ml_timeslot: string | null;
  ml_direction: string | null;
  ml_vocal: string | null;
  ml_vocal_score: number | null;
  ml_danceability: number | null;
  ml_mood_scores: Record<string, number> | null;
  ml_error: string | null;
  ml_genre_audio: string | null;
  ml_subgenre_audio: string | null;
  ml_genre_web: string | null;
  ml_genre_web_grounded: boolean | null;
  ml_genre_source: string | null;
  ml_genre_conflict: boolean | null;
  ml_vocal_audio: string | null;
  ml_vocal_source: string | null;
  ml_key_tag: string | null;
  ml_key_conflict: boolean | null;
}

export interface TrackAnalysis {
  path: string;
  filename: string;
  extension: string;
  size_mb: number;
  filename_artist: string | null;
  filename_title: string | null;
  filename_bpm: number | null;
  filename_key: string | null;
  filename_mix: string | null;
  existing_tags: ExistingTags;
  ml_analysis: MLResult | null;
  error: string | null;
}

export interface DuplicateGroup {
  method: string;
  key: string;
  keep: FileInfo;
  duplicates: FileInfo[];
  recoverable_mb: number;
}

export interface DuplicateReport {
  summary: DuplicateSummary;
  exact_duplicates: DuplicateGroup[];
  audio_duplicates: DuplicateGroup[];
}

export interface DuplicateSummary {
  total_files: number;
  exact_duplicate_groups: number;
  exact_duplicate_files: number;
  audio_duplicate_groups: number;
  audio_duplicate_files: number;
  total_duplicates: number;
  space_recoverable_mb: number;
}

export interface FileInfo {
  path: string;
  filename: string;
  size_bytes: number;
  size_mb: number;
  file_hash: string | null;
  audio_fingerprint: string | null;
  codec: string | null;
  bitrate_kbps: number | null;
  duration_s: number | null;
  modified_time: number | null;
}

export interface OrganizePlan {
  base_dir: string;
  moves: PlannedMove[];
  small_genres: string[];
  genre_counts: Record<string, number>;
  errors: string[];
}

export interface OrganizeStats {
  planned: number;
  moved: number;
  errors: string[];
  journal_path: string | null;
  moved_pairs: string[][];
}

export interface PlannedMove {
  source: string;
  destination: string;
  genre: string;
  subgenre: string;
  reason: string;
  relative_destination: string;
}

export interface LibraryRecord {
  path: string;
  analysis_path: string;
  track_count: number;
  analyzed_count: number;
  last_opened: number;
  last_analyzed: number;
  name: string;
  tags: string[];
}

export interface LibraryState {
  recent: LibraryRecord[];
}

export interface BackupHistory {
  records: BackupRecord[];
}

export interface BackupRecord {
  backup_path: string;
  library_path: string;
  file_count: number;
  created_at: number;
  size_bytes: number;
  missing: boolean;
}
