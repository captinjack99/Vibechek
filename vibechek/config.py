"""User-facing configuration for Vibechek.

Every knob the legacy scripts expose as a CLI flag should live here, with a
sane default, so the future GUI can render them as form fields without
needing to know which subcommand they belong to.

Persistence: JSON round-trip via the stdlib `json` module. The default location
is `<user_config_dir>/vibechek/config.json`. Older `config.toml` files (from
before 0.3.0) are read as a one-time migration fallback and rewritten as JSON
on the next save. Load is graceful — a missing or unparseable file falls back
to defaults rather than raising.

Why JSON instead of TOML: TOML has no null type, which forces an awkward "drop
the key" round-trip every time a field defaults to `None` — silently lossy and
a future-bug magnet. JSON has native null, primitive types, and is
stdlib-only.
"""

from __future__ import annotations

import json
import logging
import sys
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

from platformdirs import user_config_dir, user_data_dir

from vibechek.io import atomic_write_json

log = logging.getLogger(__name__)

APP_NAME = "Vibechek"
CONFIG_DIR = Path(user_config_dir(APP_NAME))
DATA_DIR = Path(user_data_dir(APP_NAME))
MODELS_DIR = DATA_DIR / "models"
CONFIG_FILE = CONFIG_DIR / "config.json"
# Pre-0.3.0 TOML file — read as one-time migration if config.json is absent.
LEGACY_CONFIG_FILE = CONFIG_DIR / "config.toml"

# Default inference engine. Windows ships the WSL-free native engine bundled in
# the installer (essentia + ONNX folded into the PyInstaller onefile), so default
# to it there; essentia_tf elsewhere. preflight() falls back to WSL / the managed
# venv when native isn't actually importable (a plain pip install, or a lean
# build whose wheel didn't bundle), so a non-bundled Windows install degrades
# gracefully instead of breaking. Existing users keep whatever their saved
# config already has; this only sets the default for a fresh/unset config.
_DEFAULT_INFERENCE_ENGINE = "native" if sys.platform == "win32" else "essentia_tf"


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
    # Hybrid CPU+GPU analysis: when a GPU is available AND `use_gpu` isn't
    # "off", run GPU workers (bounded by VRAM) AND extra CPU workers (filling
    # the remaining RAM budget) concurrently against a shared work queue. The
    # queue self-balances — whichever device finishes a track grabs the next —
    # so a GPU that's only ~3-workers-deep no longer caps total throughput when
    # there are 16 idle CPU cores. Set False to use the old single-device pool.
    hybrid_cpu_gpu: bool = True
    # Neural-net inference backend. "essentia_tf" (default) runs the
    # Discogs-EffNet models through essentia-tensorflow exactly as before.
    # "onnx" (experimental) runs the same models through ONNX Runtime
    # (vibechek/onnx_backend.py): cross-vendor GPU + no end-of-life TF runtime,
    # validated to parity in docs/ONNX_MIGRATION.md. essentia is still used for
    # the DSP (decode, melspec, BPM, key) under either engine.
    # "native" is the WSL-free Windows path: ONNX inference + the pure-NumPy mel
    # frontend + a DSP-only native essentia wheel (decode/BPM/key) run IN-PROCESS
    # (preflight routes analyze_via="native" when essentia imports locally). On
    # Linux/macOS essentia already ships wheels, so the onnx engine is native there.
    inference_engine: str = _DEFAULT_INFERENCE_ENGINE  # "essentia_tf" | "onnx" | "native"

    # ----- Existing-tag vs ML reconciliation (genre/BPM/key) ----------------
    # Many libraries already carry curated genre tags (e.g. Beatport downloads)
    # that are far more accurate than an audio-only guess — BUT some tags are
    # generic junk ("Dance/Pop", "Electronic") or plain wrong, so we must not
    # trust them blindly. `genre_source_policy` controls reconciliation:
    #   "prefer_tag" (default): use a SPECIFIC existing genre tag; fall back to
    #       ML when the tag is missing or a generic bucket; let ML override only
    #       when it both disagrees AND clears `genre_ml_override_confidence`.
    #   "prefer_ml": use ML; fall back to a specific tag only when ML is weak.
    #   "tag_only": always keep a specific existing tag (never let ML override).
    #   "ml_only": ignore existing tags entirely (pure audio — legacy behavior).
    genre_source_policy: str = "prefer_tag"
    genre_ml_override_confidence: float = 0.90

    # ----- Genre classifier + online lookup (opt-in accuracy upgrades) -------
    # `genre_classifier` picks the AUDIO genre source: "discogs" (default,
    # Discogs-EffNet head — bundled, ~28%) or "clap" (the pure-audio CLAP+kNN
    # student — ~2x better, needs the one-click CLAP setup; falls back to discogs
    # if unavailable). BPM/key/mood are unaffected either way.
    genre_classifier: str = "discogs"  # "discogs" | "clap"
    # When True, an online web-synthesis resolver (local LLM reads web results for
    # artist+title) supplies a grounded genre layered into reconciliation
    # (tag › web › audio) — ~60% on tagged libraries. Needs network + the local
    # LLM (one-click setup). Off by default. `genre_llm_backend` selects the LLM
    # ("ollama" = local/private, the shipped backend).
    genre_web_lookup: bool = False
    genre_llm_backend: str = "ollama"  # "ollama"


@dataclass
class TaggingConfig:
    """Controls for writing tags back to files."""

    genre_confidence_threshold: float = 0.85  # 85% — matches legacy default
    # Two-stage confidence: if subgenre confidence is below the strict 85%
    # threshold above but the PARENT GENRE confidence (i.e. the summed
    # family score) clears this floor, we write the parent genre into the
    # genre field instead of leaving the track tagless. Empirically lifts
    # coverage from ~53% to ~85% on the test library — a track whose model
    # is genuinely confused between Deep House and Tech House should still
    # get tagged "House" rather than dropped entirely.
    parent_genre_confidence_threshold: float = 0.50
    write_subgenre_as_main_genre: bool = True  # Rekordbox can only sort by main genre
    preserve_rekordbox_frames: bool = True  # GEOB / PRIV — cue points, beat grids
    backup_before_write: bool = True

    # ----- Per-field write toggles -----------------------------------------
    # Each ML field can be written independently. Genre is ADDITIONALLY gated
    # by the confidence thresholds above; the rest are deterministic outputs
    # that write whenever present AND their toggle is on. BPM/Key default OFF
    # because Rekordbox's own detection is usually more reliable (this replaces
    # the old single `skip_bpm_and_key` flag with granular control).
    write_genre: bool = True
    write_bpm: bool = False
    write_key: bool = False
    write_energy: bool = True
    write_mood: bool = True
    write_timeslot: bool = True
    write_direction: bool = True
    write_vocal: bool = True

    # ----- Vocal classification thresholds (voice probability 0..1) --------
    # Applied at tag time to the stored `ml_vocal_score`, so they can be tuned
    # WITHOUT re-analyzing. See analyzer._classify_vocal for the rationale:
    # instrumental dance tracks with prominent melodic leads score ~0.64-0.69,
    # so the cutoff for "Vocal" sits well above the old 0.6.
    vocal_instrumental_max: float = 0.72  # below → Instrumental
    vocal_full_min: float = 0.88          # at/above → Vocal; between → Light Vocal

    # ID3 text-frame encoding for MP3 writes. 0 = ISO-8859-1, 1 = UTF-16,
    # 3 = UTF-8 (mutagen's `Encoding.UTF8`). Rekordbox 5 and some older DJ
    # software only read encoding 0 or 1 — UTF-8 frames look empty to them.
    # Default stays UTF-8 (modern); users on old Rekordbox can switch to 1.
    id3_text_encoding: int = 3


@dataclass
class DuplicateConfig:
    """Controls for duplicate detection."""

    use_md5: bool = True
    use_chromaprint: bool = True
    chromaprint_similarity_threshold: float = 0.95
    action: str = "report"  # "report" | "move" | "trash"
    review_folder: Path | None = None  # Where to move dupes if action == "move"

    # ----- Variant awareness ------------------------------------------------
    # A DJ's library legitimately holds different *versions* of one song
    # (Extended vs Radio Edit, Original vs a Remix). Acoustic similarity alone
    # would lump them together and the keeper logic would delete one. These
    # control how aggressive de-duplication is across versions/formats.
    #
    # keep_distinct_versions: when True (default, SAFE), files that are clearly
    #   different versions (different remix, or Extended vs Radio length) are
    #   NEVER auto-removed — only redundant *encodings of the same version*
    #   collapse. Set False to dedupe ACROSS versions too (aggressive: keep one
    #   file per acoustic song regardless of version — the legacy behavior).
    # keep_all_formats: when True, keep the best file of EACH format within a
    #   version (e.g. a FLAC *and* an MP3 for a controller that can't read FLAC)
    #   instead of collapsing to the single best encoding.
    # version_duration_tolerance: two files with the SAME parsed version key are
    #   still treated as different versions if their durations differ by more
    #   than this fraction (catches mislabeled extended/radio pairs).
    keep_distinct_versions: bool = True
    keep_all_formats: bool = False
    version_duration_tolerance: float = 0.12


@dataclass
class OrganizationConfig:
    """Controls for folder organization."""

    use_subgenres: bool = True
    min_genre_size: int = 10  # Genres with fewer tracks get bucketed into Other/
    target_root: Path | None = None  # Where the organized tree lives


@dataclass
class UIConfig:
    """Persistent flags for the GUI itself (not the analysis pipeline).

    Lives alongside the pipeline config because everything the user can change
    should round-trip through one file. Add new flags here rather than spawning
    a second config file.
    """

    seen_onboarding: bool = False  # Becomes True after the first-launch tour


@dataclass
class VibechekConfig:
    """Top-level config — everything a GUI form needs to render."""

    analysis: AnalysisConfig = field(default_factory=AnalysisConfig)
    tagging: TaggingConfig = field(default_factory=TaggingConfig)
    duplicates: DuplicateConfig = field(default_factory=DuplicateConfig)
    organization: OrganizationConfig = field(default_factory=OrganizationConfig)
    ui: UIConfig = field(default_factory=UIConfig)

    @classmethod
    def load(cls, path: Path | None = None) -> VibechekConfig:
        """Load config from disk, falling back to defaults on any error.

        Never raises — corrupted user config shouldn't break the app.

        Resolution order:
          1. If `path` is given, load that file (JSON).
          2. Otherwise, try `CONFIG_FILE` (JSON).
          3. Otherwise, fall back to `LEGACY_CONFIG_FILE` (TOML) for a one-time
             migration. The next `save()` rewrites it as JSON.
        """
        if path is not None:
            return cls._load_json(path)

        if CONFIG_FILE.exists():
            return cls._load_json(CONFIG_FILE)

        if LEGACY_CONFIG_FILE.exists():
            log.info(
                "Migrating legacy TOML config from %s — next save will write JSON.",
                LEGACY_CONFIG_FILE,
            )
            return cls._load_toml(LEGACY_CONFIG_FILE)

        return cls()

    @classmethod
    def _load_json(cls, target: Path) -> VibechekConfig:
        try:
            raw = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            log.warning("Could not load config from %s: %s — using defaults", target, e)
            return cls()
        try:
            return cls._from_dict(raw)
        except Exception as e:  # noqa: BLE001
            log.warning("Could not parse config from %s: %s — using defaults", target, e)
            return cls()

    @classmethod
    def _load_toml(cls, target: Path) -> VibechekConfig:
        # Import lazily — tomllib is only needed for the rare migration path,
        # and we want module import to stay cheap.
        #
        # `tomllib` is stdlib from Python 3.11+. On 3.10 we fall back to `tomli`
        # (the third-party backport that became stdlib `tomllib`). Both expose
        # the same `loads()` + `TOMLDecodeError`. We depend on `tomli` via
        # pyproject.toml's `tomli; python_version < "3.11"` marker so 3.10
        # users always have it available.
        try:
            import tomllib  # Python 3.11+
        except ImportError:
            try:
                import tomli as tomllib  # Python 3.10 backport
            except ImportError:  # pragma: no cover
                log.warning(
                    "Neither tomllib nor tomli is available; cannot read legacy %s. "
                    "Install tomli: pip install tomli", target,
                )
                return cls()
        try:
            raw = tomllib.loads(target.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as e:
            log.warning("Could not load legacy TOML config from %s: %s — using defaults", target, e)
            return cls()
        try:
            return cls._from_dict(raw)
        except Exception as e:  # noqa: BLE001
            log.warning("Could not parse legacy TOML config from %s: %s — using defaults", target, e)
            return cls()

    def save(self, path: Path | None = None) -> Path:
        """Write the current config to disk as JSON.

        Returns the final destination path. Creates parent dirs as needed.
        """
        target = path or CONFIG_FILE
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = _to_jsonable(self)
        # Atomic write: a kill-during-write of config.json used to leave an
        # empty file that the next launch read as "use defaults", silently
        # erasing the user's settings.
        atomic_write_json(target, payload, indent=2, sort_keys=True)
        return target

    @classmethod
    def _from_dict(cls, data: dict[str, Any]) -> VibechekConfig:
        return cls(
            analysis=_subset(AnalysisConfig, data.get("analysis", {})),
            tagging=_subset(TaggingConfig, data.get("tagging", {})),
            duplicates=_subset(DuplicateConfig, data.get("duplicates", {})),
            organization=_subset(OrganizationConfig, data.get("organization", {})),
            ui=_subset(UIConfig, data.get("ui", {})),
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _subset(cls: type, data: dict[str, Any]) -> Any:
    """Build `cls` from `data`, dropping unknown keys + coercing field types.

    Lets us evolve the dataclass without breaking older config files. On a
    coercion failure we fall back to the dataclass default for that field
    rather than crashing — the user sees a warning naming the bad value.
    """
    valid_fields = {f.name: f for f in fields(cls)}
    kwargs: dict[str, Any] = {}
    for key, value in data.items():
        if key not in valid_fields:
            continue  # Unknown / removed field — ignore
        f = valid_fields[key]
        try:
            coerced = _coerce(f.type, value)
        except (TypeError, ValueError) as e:
            log.warning(
                "Config field %s.%s has invalid value %r (%s); using default",
                cls.__name__, key, value, e,
            )
            # Falling through without setting kwargs[key] uses the dataclass default.
            continue
        # id3_text_encoding feeds mutagen's ID3 frame constructors directly, so
        # a hand-edited / stale value outside {0,1,2,3} would write a corrupt
        # encoding byte. Snap it back to UTF-8 (3) rather than propagating it.
        if key == "id3_text_encoding" and coerced not in (0, 1, 2, 3):
            log.warning(
                "Config field %s.id3_text_encoding has out-of-range value %r; "
                "falling back to 3 (UTF-8)", cls.__name__, coerced,
            )
            continue  # use the dataclass default (3)
        # inference_engine steers every `if engine == "onnx"` branch; a typo or
        # wrong-case value ("ONNX", "tf") would silently run the essentia_tf
        # path. Snap anything unrecognized back to the default (fail-soft, loud).
        if key == "inference_engine" and coerced not in ("essentia_tf", "onnx", "native"):
            log.warning(
                "Config field %s.inference_engine has unknown value %r; "
                "falling back to the platform default", cls.__name__, coerced,
            )
            continue  # use the dataclass default (_DEFAULT_INFERENCE_ENGINE)
        # The genre enums steer model loading + reconciliation the same way —
        # a hand-edited value ("CLAP", "ml") would render Settings with neither
        # option selected and silently run default behavior. Snap back loudly.
        if key == "genre_classifier" and coerced not in ("discogs", "clap"):
            log.warning(
                "Config field %s.genre_classifier has unknown value %r; "
                "falling back to 'discogs'", cls.__name__, coerced,
            )
            continue
        if key == "genre_source_policy" and coerced not in (
            "prefer_tag", "prefer_ml", "tag_only", "ml_only",
        ):
            log.warning(
                "Config field %s.genre_source_policy has unknown value %r; "
                "falling back to 'prefer_tag'", cls.__name__, coerced,
            )
            continue
        if key == "genre_llm_backend" and coerced not in ("ollama",):
            log.warning(
                "Config field %s.genre_llm_backend has unknown value %r; "
                "falling back to 'ollama'", cls.__name__, coerced,
            )
            continue
        kwargs[key] = coerced
    return cls(**kwargs)


def _coerce(ftype: Any, value: Any) -> Any:
    """Bring a decoded primitive back to its dataclass type.

    Handles:
      - `None` passthrough (Optional fields).
      - `Path` (str → Path).
      - `bool` (accepts truthy/falsy strings, case-insensitive).
      - `int` / `float` (numeric strings coerced; non-numeric raises).
      - `str` (anything → str via `str()`).
      - Anything else → passthrough.

    Raises `TypeError` or `ValueError` on coercion failure; `_subset` catches
    those and falls back to the field default.
    """
    if value is None:
        return None

    type_str = str(ftype)

    # Path (or Optional[Path]) — the dataclass field annotation is a string
    # under `from __future__ import annotations`, so substring match is what
    # we have without `typing.get_type_hints` (which fails on lazy hints here).
    if "Path" in type_str:
        if isinstance(value, Path):
            return value
        return Path(str(value))

    # bool BEFORE int — `bool` is a subclass of `int` in Python, and `isinstance`
    # checks would treat True/False as ints. Match by type string.
    if "bool" in type_str:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in ("true", "yes", "1", "on"):
                return True
            if lowered in ("false", "no", "0", "off"):
                return False
            raise ValueError(f"cannot interpret {value!r} as bool")
        raise TypeError(f"cannot coerce {type(value).__name__} to bool")

    if "int" in type_str:
        if isinstance(value, bool):
            # `True`/`False` shouldn't sneak into an int field as 1/0; treat
            # as a config mistake.
            raise TypeError("bool given for int field")
        # Accept both `10` and a string/float `"10"` / `10.0` uniformly:
        # `int("10.0")` raises, but `int(float("10.0"))` works. Round so a
        # hand-edited `10.9` lands on 11 rather than silently truncating to 10
        # without the user noticing (and stays consistent with the float path).
        if isinstance(value, str):
            return int(round(float(value)))
        return int(round(value)) if isinstance(value, float) else int(value)

    if "float" in type_str:
        if isinstance(value, bool):
            raise TypeError("bool given for float field")
        return float(value)

    if "str" in type_str:
        if isinstance(value, str):
            return value
        return str(value)

    return value


def _to_jsonable(cfg: VibechekConfig) -> dict[str, Any]:
    """Convert config → JSON-safe dict.

    Paths become strings; None passes through (JSON has native null). Used for
    on-disk persistence AND for the RPC wire shape (via `_stringify_paths`).
    """
    out: dict[str, Any] = {}
    for section_name, section_value in asdict(cfg).items():
        out[section_name] = _stringify_paths(section_value)
    return out


def _stringify_paths(value: Any) -> Any:
    """Make a value JSON-safe by stringifying Paths.

    None passes through (JSON encodes it as `null`). Nested dicts/lists are
    walked recursively. This was previously stripping None from dicts to
    accommodate TOML — that's no longer needed under JSON.
    """
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {k: _stringify_paths(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_stringify_paths(v) for v in value]
    return value


__all__ = [
    "APP_NAME",
    "CONFIG_DIR",
    "DATA_DIR",
    "MODELS_DIR",
    "CONFIG_FILE",
    "LEGACY_CONFIG_FILE",
    "AnalysisConfig",
    "TaggingConfig",
    "DuplicateConfig",
    "OrganizationConfig",
    "UIConfig",
    "VibechekConfig",
]
