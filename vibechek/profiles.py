"""Built-in DJ profiles — opinionated config presets for different gig styles.

Each profile is a small overlay on top of `VibechekConfig`. Applying a
profile mutates the relevant fields in place and saves the config; nothing
else changes. The user can freely tweak settings after loading a profile.

The profiles encode practical defaults different DJs want by default:

- A **disco DJ** with 800 tracks wants every micro-genre to get its own
  folder (min_genre_size=3), even if it's just a handful of edits.
- An **EDM festival DJ** with 12k tracks wants only the big-bucket folders
  (min_genre_size=30) — everything smaller is noise in their workflow.
- A **bar resident** wants relaxed confidence (0.70) because they're tagging
  for personal lookup, not for purity.

The `timeslot_bpm_bands` field is advisory metadata only for now — it's
emitted from `list_profiles()` so the UI can later highlight tracks whose
ML BPM matches the profile's preferred range per timeslot. No analyzer
code reads it today; we ship the data so the UI agent can build the
visualization without a round-trip on us.

Adding a new built-in profile is a one-line entry in `BUILT_IN_PROFILES`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from vibechek.config import VibechekConfig

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class DJProfile:
    """A named bundle of config overrides + advisory metadata.

    Fields are intentionally optional — a profile only specifies what it
    actually wants to change. `apply()` merges only the non-None fields.
    """

    name: str
    description: str
    genre_confidence_threshold: float | None = None
    min_genre_size: int | None = None
    use_gpu: str | None = None  # "auto" | "on" | "off" — user knob
    # Advisory: per-timeslot preferred BPM range. Format:
    #   {"Warm-Up": (110, 120), "Peak": (124, 130), ...}
    # The UI can show these as a band on the BPM histogram; the analyzer
    # ignores them.
    timeslot_bpm_bands: dict[str, tuple[int, int]] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Built-in profiles
# ---------------------------------------------------------------------------


# House DJ: standard 4/4 house workflow, default-ish thresholds, GPU agnostic.
# Wants timeslot bands centered on common house ranges.
_HOUSE_DJ = DJProfile(
    name="house-dj",
    description="Standard house workflow — strong genre buckets, GPU auto.",
    genre_confidence_threshold=0.85,
    min_genre_size=10,
    use_gpu="auto",
    timeslot_bpm_bands={
        "Warm-Up": (118, 122),
        "Peak": (123, 128),
        "Afterhours": (115, 122),
    },
)

# Disco DJ: small library, lots of long-tail subgenres (italo, nu-disco,
# boogie, edits…). Lower `min_genre_size` so every flavor gets its own folder.
_DISCO_DJ = DJProfile(
    name="disco-dj",
    description="Disco / nu-disco / boogie — keep every micro-genre folder.",
    genre_confidence_threshold=0.75,
    min_genre_size=3,
    use_gpu="auto",
    timeslot_bpm_bands={
        "Warm-Up": (100, 110),
        "Peak": (112, 122),
        "Afterhours": (98, 108),
    },
)

# Open-format: relaxed thresholds + middle-of-the-road bucketing. Best for
# weddings / corporate / mobile DJ work where genres are messy.
_OPEN_FORMAT = DJProfile(
    name="open-format",
    description="Mixed bag — relaxed confidence, medium genre buckets.",
    genre_confidence_threshold=0.70,
    min_genre_size=8,
    use_gpu="auto",
)

# EDM festival: huge libraries, big buckets only — DJ doesn't need 30 tiny
# subfolders. Tight confidence avoids mis-tagging.
_EDM_FESTIVAL = DJProfile(
    name="edm-festival",
    description="Festival sets — big genre buckets, strict confidence.",
    genre_confidence_threshold=0.90,
    min_genre_size=30,
    use_gpu="on",
    timeslot_bpm_bands={
        "Warm-Up": (124, 128),
        "Peak": (128, 138),
        "Afterhours": (118, 126),
    },
)

# Bar resident: tagging for personal lookup, not curated buckets. Generous
# threshold, small library so micro-genres are fine.
_BAR_RESIDENT = DJProfile(
    name="bar-resident",
    description="Bar/lounge — permissive tagging, small genre buckets.",
    genre_confidence_threshold=0.65,
    min_genre_size=5,
    use_gpu="auto",
)

# Afterhours / deep / underground: slow + steady, tight genre buckets.
_AFTERHOURS = DJProfile(
    name="afterhours",
    description="Afterhours/deep — slower BPM bands, balanced bucketing.",
    genre_confidence_threshold=0.80,
    min_genre_size=8,
    use_gpu="auto",
    timeslot_bpm_bands={
        "Warm-Up": (110, 118),
        "Peak": (118, 124),
        "Afterhours": (105, 118),
    },
)


BUILT_IN_PROFILES: dict[str, DJProfile] = {
    p.name: p for p in (_HOUSE_DJ, _DISCO_DJ, _OPEN_FORMAT, _EDM_FESTIVAL, _BAR_RESIDENT, _AFTERHOURS)
}


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------


def list_profiles() -> list[dict[str, Any]]:
    """Return all profiles as JSON-safe dicts. Used by the CLI and RPC.

    Timeslot tuples are serialized to lists so JSON round-trips cleanly.
    """
    out: list[dict[str, Any]] = []
    for p in BUILT_IN_PROFILES.values():
        out.append({
            "name": p.name,
            "description": p.description,
            "genre_confidence_threshold": p.genre_confidence_threshold,
            "min_genre_size": p.min_genre_size,
            "use_gpu": p.use_gpu,
            "timeslot_bpm_bands": {
                k: list(v) for k, v in p.timeslot_bpm_bands.items()
            },
        })
    return out


def get_profile(name: str) -> DJProfile | None:
    """Look up a profile by name. Case-insensitive."""
    if not name:
        return None
    return BUILT_IN_PROFILES.get(name.strip().lower())


def apply_profile(profile: DJProfile, config: VibechekConfig) -> VibechekConfig:
    """Apply the profile's overrides to `config` in place. Returns the same instance.

    Only non-None fields on the profile are written. Anything the profile
    doesn't mention is left as-is — this means loading two profiles in a row
    is *additive* on the fields they don't share, which is what a user
    layering "house-dj" then "edm-festival" would expect.
    """
    if profile.genre_confidence_threshold is not None:
        config.tagging.genre_confidence_threshold = profile.genre_confidence_threshold
    if profile.min_genre_size is not None:
        config.organization.min_genre_size = profile.min_genre_size
    if profile.use_gpu is not None:
        config.analysis.use_gpu = profile.use_gpu
    return config


def load_profile(name: str) -> dict[str, Any]:
    """End-to-end: look up profile by name, apply to disk config, save.

    Returns a small dict describing what happened — used by both the CLI
    and the eventual RPC method. Raises `KeyError` if the profile name is
    unknown (the CLI catches it and prints a friendly error).
    """
    profile = get_profile(name)
    if profile is None:
        raise KeyError(f"Unknown profile: {name!r}. Known: {sorted(BUILT_IN_PROFILES)}")
    config = VibechekConfig.load()
    apply_profile(profile, config)
    saved_to = config.save()
    log.info("Loaded profile %s; config saved to %s", profile.name, saved_to)
    return {
        "loaded": profile.name,
        "saved_to": str(saved_to),
        "applied": {
            "genre_confidence_threshold": config.tagging.genre_confidence_threshold,
            "min_genre_size": config.organization.min_genre_size,
            "use_gpu": config.analysis.use_gpu,
        },
    }


__all__ = [
    "DJProfile",
    "BUILT_IN_PROFILES",
    "list_profiles",
    "get_profile",
    "apply_profile",
    "load_profile",
]
