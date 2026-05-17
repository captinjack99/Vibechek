"""User-facing configuration for Vibechek.

Every knob the legacy scripts expose as a CLI flag should live here, with a
sane default, so the future GUI can render them as form fields without
needing to know which subcommand they belong to.

Persistence: TOML round-trip via tomllib (read) + tomli_w (write). The default
location is `<user_config_dir>/vibechek/config.toml`. Load is graceful — a
missing or unparseable file falls back to defaults rather than raising.
"""

from __future__ import annotations

import logging
import tomllib
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

import tomli_w
from platformdirs import user_config_dir, user_data_dir

log = logging.getLogger(__name__)

APP_NAME = "Vibechek"
CONFIG_DIR = Path(user_config_dir(APP_NAME))
DATA_DIR = Path(user_data_dir(APP_NAME))
MODELS_DIR = DATA_DIR / "models"
CONFIG_FILE = CONFIG_DIR / "config.toml"


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
    def load(cls, path: Path | None = None) -> "VibechekConfig":
        """Load config from disk, falling back to defaults on any error.

        Never raises — corrupted user config shouldn't break the app.
        """
        target = path or CONFIG_FILE
        if not target.exists():
            return cls()

        try:
            raw = tomllib.loads(target.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as e:
            log.warning("Could not load config from %s: %s — using defaults", target, e)
            return cls()

        try:
            return cls._from_dict(raw)
        except Exception as e:  # noqa: BLE001
            log.warning("Could not parse config from %s: %s — using defaults", target, e)
            return cls()

    def save(self, path: Path | None = None) -> Path:
        """Write the current config to disk as TOML.

        Returns the final destination path. Creates parent dirs as needed.
        """
        target = path or CONFIG_FILE
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = _to_toml_dict(self)
        target.write_bytes(tomli_w.dumps(payload).encode("utf-8"))
        return target

    @classmethod
    def _from_dict(cls, data: dict[str, Any]) -> "VibechekConfig":
        return cls(
            analysis=_subset(AnalysisConfig, data.get("analysis", {})),
            tagging=_subset(TaggingConfig, data.get("tagging", {})),
            duplicates=_subset(DuplicateConfig, data.get("duplicates", {})),
            organization=_subset(OrganizationConfig, data.get("organization", {})),
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _subset(cls: type, data: dict[str, Any]) -> Any:
    """Build `cls` from `data`, dropping unknown keys + coercing Paths.

    Lets us evolve the dataclass without breaking older config files.
    """
    valid_fields = {f.name: f.type for f in fields(cls)}
    kwargs: dict[str, Any] = {}
    for key, value in data.items():
        if key not in valid_fields:
            continue  # Unknown / removed field — ignore
        ftype = valid_fields[key]
        kwargs[key] = _coerce(ftype, value)
    return cls(**kwargs)


def _coerce(ftype: Any, value: Any) -> Any:
    """Bring TOML-decoded primitives back to their dataclass types."""
    type_str = str(ftype)
    if "Path" in type_str and value is not None and not isinstance(value, Path):
        return Path(str(value))
    return value


def _to_toml_dict(cfg: VibechekConfig) -> dict[str, Any]:
    """Convert config → JSON-safe dict that tomli_w can serialize."""
    out: dict[str, Any] = {}
    for section_name, section_value in asdict(cfg).items():
        out[section_name] = _stringify_paths(section_value)
    return out


def _stringify_paths(value: Any) -> Any:
    """Make a value safe for tomli_w: stringify Paths, drop None (TOML has no null)."""
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {k: _stringify_paths(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_stringify_paths(v) for v in value if v is not None]
    return value


__all__ = [
    "APP_NAME",
    "CONFIG_DIR",
    "DATA_DIR",
    "MODELS_DIR",
    "CONFIG_FILE",
    "AnalysisConfig",
    "TaggingConfig",
    "DuplicateConfig",
    "OrganizationConfig",
    "VibechekConfig",
]
