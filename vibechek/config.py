"""User-facing configuration for Vibechek.

Every knob the legacy scripts expose as a CLI flag should live here, with a
sane default, so the future GUI can render them as form fields without
needing to know which subcommand they belong to.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from platformdirs import user_config_dir, user_data_dir

APP_NAME = "Vibechek"
CONFIG_DIR = Path(user_config_dir(APP_NAME))
DATA_DIR = Path(user_data_dir(APP_NAME))
MODELS_DIR = DATA_DIR / "models"


@dataclass
class AnalysisConfig:
    """Controls for the ML analysis pass.

    The CLI default for `workers` is computed at invocation time as
    `max(1, cpu_count - 1)` so we leave one core for the OS. The dataclass
    default of 0 means "let the caller pick"; specific callers (CLI / RPC)
    fill it in. The GUI surfaces it as a slider.
    """

    workers: int = 0  # 0 → auto (cpu_count - 1); set explicitly otherwise
    models_dir: Path = MODELS_DIR
    # GPU usage. "auto" → TF picks GPU if available, falls back to CPU.
    # "on" → force GPU 0 visible (errors loudly if no GPU). "off" → CPU-only.
    use_gpu: str = "auto"


@dataclass
class TaggingConfig:
    """Controls for writing tags back to files."""

    genre_confidence_threshold: float = 0.85  # 85% — matches legacy default
    write_subgenre_as_main_genre: bool = True  # Rekordbox can only sort by main genre
    preserve_rekordbox_frames: bool = True  # GEOB / PRIV — cue points, beat grids
    backup_before_write: bool = True
    skip_bpm_and_key: bool = True  # Trust Rekordbox over the ML BPM/key models


@dataclass
class DuplicateConfig:
    """Controls for duplicate detection."""

    use_md5: bool = True
    use_chromaprint: bool = True
    chromaprint_similarity_threshold: float = 0.95
    action: str = "report"  # "report" | "move" | "trash"
    review_folder: Path | None = None  # Where to move dupes if action == "move"


@dataclass
class OrganizationConfig:
    """Controls for folder organization."""

    use_subgenres: bool = True
    min_genre_size: int = 10  # Genres with fewer tracks get bucketed into Other/
    target_root: Path | None = None  # Where the organized tree lives


@dataclass
class VibechekConfig:
    """Top-level config — everything a GUI form needs to render."""

    analysis: AnalysisConfig = field(default_factory=AnalysisConfig)
    tagging: TaggingConfig = field(default_factory=TaggingConfig)
    duplicates: DuplicateConfig = field(default_factory=DuplicateConfig)
    organization: OrganizationConfig = field(default_factory=OrganizationConfig)

    @classmethod
    def load(cls, path: Path | None = None) -> VibechekConfig:
        """Load config from disk, falling back to defaults."""
        # TODO(phase-1): persist/load TOML from CONFIG_DIR / "config.toml"
        return cls()

    def save(self, path: Path | None = None) -> None:
        """Persist config to disk."""
        # TODO(phase-1): write TOML to CONFIG_DIR / "config.toml"
        raise NotImplementedError
