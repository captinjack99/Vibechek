/**
 * Types mirroring the Python-side payloads emitted by the JSON-RPC sidecar.
 *
 * Keep these in sync with the dataclasses in `vibechek/`. Where a Python
 * field is Optional in the source, we mark it nullable here.
 */

// ---------------------------------------------------------------------------
// Audio file & analysis
// ---------------------------------------------------------------------------

export interface FileInfo {
  path: string;
  filename: string;
  extension: string;
  size_mb: number;
  file_hash?: string | null;
  audio_fingerprint?: string | null;
}

export interface MLResult {
  ml_genre?: string | null;
  ml_subgenre?: string | null;
  ml_genre_confidence?: number | null;
  ml_bpm?: number | null;
  ml_key?: string | null;
  ml_energy?: number | null;
  ml_mood?: "Dark" | "Neutral" | "Bright" | null;
  ml_timeslot?: "Opener" | "Warm-Up" | "Peak" | "Afterhours" | null;
  ml_direction?: "Up" | "Steady" | "Down" | null;
  ml_vocal?: "Instrumental" | "Light Vocal" | "Vocal" | null;
  ml_danceability?: number | null;
  ml_mood_scores?: Record<string, number> | null;
  ml_error?: string | null;
}

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
}

// ---------------------------------------------------------------------------
// Duplicates
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
  };
  exact_duplicates: DuplicateGroup[];
  audio_duplicates: DuplicateGroup[];
}

// ---------------------------------------------------------------------------
// Organize
// ---------------------------------------------------------------------------

export interface PlannedMove {
  source: string;
  destination: string;
  genre: string;
  subgenre: string;
  reason: string;
}

export interface OrganizePlan {
  base_dir: string;
  small_genres: string[];
  genre_counts: Record<string, number>;
  moves: PlannedMove[];
  errors: string[];
}

// ---------------------------------------------------------------------------
// Settings (mirrors VibechekConfig in vibechek/config.py)
// ---------------------------------------------------------------------------

export interface AnalysisConfig {
  workers: number;          // 0 = auto (cpu_count - 1)
  models_dir: string;
  use_gpu: "auto" | "on" | "off";
}

export interface GpuDevice {
  name: string;
  backend: "cuda" | "rocm" | "metal" | "unknown";
  memory_mb: number | null;
}

export interface SystemResources {
  platform: string;
  cpu_count: number;
  memory_total_mb: number | null;
  memory_available_mb: number | null;
  gpu_available: boolean;
  gpu_devices: GpuDevice[];
  cuda_runtime: string | null;
  recommended_workers: number;
}

export interface TaggingConfig {
  genre_confidence_threshold: number;
  write_subgenre_as_main_genre: boolean;
  preserve_rekordbox_frames: boolean;
  backup_before_write: boolean;
  skip_bpm_and_key: boolean;
}

export interface DuplicateConfig {
  use_md5: boolean;
  use_chromaprint: boolean;
  chromaprint_similarity_threshold: number;
  action: "report" | "move" | "trash";
  review_folder: string | null;
}

export interface OrganizationConfig {
  use_subgenres: boolean;
  min_genre_size: number;
  target_root: string | null;
}

export interface VibechekConfig {
  analysis: AnalysisConfig;
  tagging: TaggingConfig;
  duplicates: DuplicateConfig;
  organization: OrganizationConfig;
}

// ---------------------------------------------------------------------------
// View state
// ---------------------------------------------------------------------------

export type ViewMode = "library" | "duplicates" | "organize" | "settings";

export interface ProgressEvent {
  current: number;
  total: number;
  message: string;
}

export interface ReadyEvent {
  version: string;
  methods: string[];
}

// ---------------------------------------------------------------------------
// Preflight (vibechek/preflight.py)
// ---------------------------------------------------------------------------

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
  reasons_not_ready: string[];
}
