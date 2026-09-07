"""Tests for vibechek.config primitive type coercion.

A corrupted or hand-edited config file can hand us a string where
we expect an int. Without coercion, that crashes deep inside the analyze path
with `TypeError: '>' not supported between instances of 'str' and 'int'`.
We coerce primitives based on the declared dataclass field type and fall back
to the default on a bad value (with a warning, not an exception).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from vibechek.config import (
    AnalysisConfig,
    TaggingConfig,
    VibechekConfig,
    _coerce,
    _subset,
)

# ---------------------------------------------------------------------------
# _coerce — primitive level
# ---------------------------------------------------------------------------


def test_coerce_int_from_string() -> None:
    assert _coerce("int", "42") == 42


def test_coerce_int_from_int_passthrough() -> None:
    assert _coerce("int", 42) == 42


def test_coerce_int_rejects_bool() -> None:
    # `True` is technically an `int` subclass; we treat it as a category
    # mistake when the field is an int (config files shouldn't conflate them).
    with pytest.raises(TypeError):
        _coerce("int", True)


def test_coerce_int_raises_on_garbage_string() -> None:
    with pytest.raises(ValueError):
        _coerce("int", "lots")


def test_coerce_float_from_string() -> None:
    assert _coerce("float", "0.85") == pytest.approx(0.85)


def test_coerce_float_from_int() -> None:
    assert _coerce("float", 1) == 1.0


def test_coerce_float_raises_on_garbage() -> None:
    with pytest.raises(ValueError):
        _coerce("float", "kinda high")


@pytest.mark.parametrize(
    "raw,expected",
    [
        (True, True), (False, False),
        ("true", True), ("True", True), ("TRUE", True), ("yes", True),
        ("1", True), ("on", True),
        ("false", False), ("False", False), ("no", False), ("0", False),
        ("off", False),
        (1, True), (0, False),
    ],
)
def test_coerce_bool_accepts_common_forms(raw: object, expected: bool) -> None:
    assert _coerce("bool", raw) is expected


def test_coerce_bool_raises_on_unknown_string() -> None:
    with pytest.raises(ValueError):
        _coerce("bool", "maybe")


def test_coerce_str_from_int() -> None:
    assert _coerce("str", 42) == "42"


def test_coerce_path_from_string() -> None:
    out = _coerce("Path | None", "/tmp/x")
    assert isinstance(out, Path)
    assert out == Path("/tmp/x")


def test_coerce_none_passthrough_for_optional_path() -> None:
    assert _coerce("Path | None", None) is None


def test_coerce_none_passthrough_for_optional_int() -> None:
    # No int field is currently Optional, but the helper must still handle it.
    assert _coerce("int | None", None) is None


# ---------------------------------------------------------------------------
# _subset — dataclass-level fallback to defaults on bad values
# ---------------------------------------------------------------------------


def test_subset_falls_back_to_default_on_bad_int(caplog: pytest.LogCaptureFixture) -> None:
    """`workers = "lots"` should not crash — fall back to the default (0)."""
    caplog.set_level(logging.WARNING, logger="vibechek.config")
    cfg = _subset(AnalysisConfig, {"workers": "lots"})
    assert cfg.workers == AnalysisConfig().workers  # default
    assert any("workers" in r.message and "lots" in r.message for r in caplog.records)


def test_subset_falls_back_to_default_on_bad_float() -> None:
    cfg = _subset(TaggingConfig, {"genre_confidence_threshold": "kinda high"})
    assert cfg.genre_confidence_threshold == TaggingConfig().genre_confidence_threshold


def test_subset_falls_back_to_default_on_bad_bool() -> None:
    cfg = _subset(TaggingConfig, {"write_bpm": "maybe"})
    assert cfg.write_bpm == TaggingConfig().write_bpm


def test_subset_coerces_string_to_int() -> None:
    """Numeric-string ints in a hand-edited config still work."""
    cfg = _subset(AnalysisConfig, {"workers": "4"})
    assert cfg.workers == 4


def test_subset_coerces_string_to_float() -> None:
    cfg = _subset(TaggingConfig, {"genre_confidence_threshold": "0.5"})
    assert cfg.genre_confidence_threshold == pytest.approx(0.5)


def test_subset_drops_unknown_fields() -> None:
    cfg = _subset(AnalysisConfig, {"workers": 2, "made_up_field": "x"})
    assert cfg.workers == 2
    assert not hasattr(cfg, "made_up_field")


# ---------------------------------------------------------------------------
# id3_text_encoding range validation (audit MEDIUM)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("good", [0, 1, 2, 3])
def test_subset_keeps_valid_id3_encoding(good: int) -> None:
    cfg = _subset(TaggingConfig, {"id3_text_encoding": good})
    assert cfg.id3_text_encoding == good


@pytest.mark.parametrize("bad", [4, 99, -1, 7])
def test_subset_rejects_out_of_range_id3_encoding(
    bad: int, caplog: pytest.LogCaptureFixture
) -> None:
    """A stale/hand-edited encoding outside {0,1,2,3} would write a corrupt
    encoding byte into MP3 frames — snap it back to the UTF-8 default (3)."""
    caplog.set_level(logging.WARNING, logger="vibechek.config")
    cfg = _subset(TaggingConfig, {"id3_text_encoding": bad})
    assert cfg.id3_text_encoding == TaggingConfig().id3_text_encoding == 3
    assert any("id3_text_encoding" in r.message for r in caplog.records)


@pytest.mark.parametrize(("field", "bad", "default"), [
    ("genre_classifier", "CLAP", "discogs"),
    ("genre_classifier", "ml", "discogs"),
    ("genre_source_policy", "ml", "prefer_tag"),
    ("genre_source_policy", "PREFER_TAG", "prefer_tag"),
    ("genre_llm_backend", "openai", "ollama"),
])
def test_subset_snaps_back_unknown_genre_enums(
    field: str, bad: str, default: str, caplog: pytest.LogCaptureFixture
) -> None:
    """The genre enums steer model loading + reconciliation; a hand-edited
    value would render Settings with no option selected and silently run
    default behavior. Snap back loudly like inference_engine."""
    from vibechek.config import AnalysisConfig  # noqa: PLC0415
    caplog.set_level(logging.WARNING, logger="vibechek.config")
    cfg = _subset(AnalysisConfig, {field: bad})
    assert getattr(cfg, field) == default
    assert any(field in r.message for r in caplog.records)
    # valid values pass through untouched
    good = _subset(AnalysisConfig, {field: default})
    assert getattr(good, field) == default


# ---------------------------------------------------------------------------
# End-to-end: corrupted config doesn't crash the loader
# ---------------------------------------------------------------------------


def test_load_with_corrupted_types_uses_defaults(tmp_path: Path) -> None:
    """A hand-edited config with the wrong type loads to defaults for the bad
    field, not an exception. Other valid fields still apply."""
    target = tmp_path / "config.json"
    target.write_text(
        json.dumps({
            "analysis": {"workers": "lots", "use_gpu": "off"},  # workers bad, use_gpu good
            "tagging": {"genre_confidence_threshold": "??"},     # bad
        }),
        encoding="utf-8",
    )

    loaded = VibechekConfig.load(target)
    # Bad fields use defaults
    assert loaded.analysis.workers == AnalysisConfig().workers
    assert loaded.tagging.genre_confidence_threshold == TaggingConfig().genre_confidence_threshold
    # Good fields still apply
    assert loaded.analysis.use_gpu == "off"


# ---------------------------------------------------------------------------
# Surfacing the snap-back: a silently-reverted setting must not masquerade as
# the user's own choice. `_subset` collects notes; `get_config` ships them.
# ---------------------------------------------------------------------------


def test_subset_collects_warning_note_for_snapped_value() -> None:
    warnings: list[str] = []
    _subset(AnalysisConfig, {"genre_classifier": "CLAP"}, warnings)
    assert len(warnings) == 1
    assert "genre_classifier" in warnings[0]
    assert "CLAP" in warnings[0]


def test_subset_collects_note_on_coercion_failure() -> None:
    warnings: list[str] = []
    _subset(AnalysisConfig, {"workers": "lots"}, warnings)
    assert warnings and "workers" in warnings[0]


def test_subset_no_notes_for_clean_values() -> None:
    warnings: list[str] = []
    _subset(AnalysisConfig, {"workers": 4, "genre_classifier": "clap"}, warnings)
    assert warnings == []


def test_from_dict_attaches_load_warnings() -> None:
    cfg = VibechekConfig._from_dict({"analysis": {"genre_source_policy": "ml"}})
    assert any("genre_source_policy" in w for w in cfg.load_warnings)


def test_get_config_rpc_surfaces_reset_warnings() -> None:
    """The GUI's config-load path reads `config_warnings` off get_config to show
    a one-time 'some saved settings were invalid and reset' toast."""
    from vibechek import config as cfg_module
    from vibechek.rpc import _get_config

    # conftest isolates CONFIG_FILE to a per-test tmp path; load() reads it as
    # JSON regardless of the .toml suffix.
    cfg_module.CONFIG_FILE.write_text(
        json.dumps({"analysis": {"inference_engine": "TF", "genre_classifier": "CLAP"}}),
        encoding="utf-8",
    )
    payload = _get_config({})
    warnings = payload.get("config_warnings")
    assert warnings
    assert any("inference_engine" in w for w in warnings)
    assert any("genre_classifier" in w for w in warnings)


def test_get_config_rpc_no_warnings_key_on_clean_config() -> None:
    from vibechek import config as cfg_module
    from vibechek.rpc import _get_config

    cfg_module.CONFIG_FILE.write_text(
        json.dumps({"analysis": {"workers": 4}}), encoding="utf-8"
    )
    payload = _get_config({})
    assert "config_warnings" not in payload


# ---------------------------------------------------------------------------
# Blank path fields (audit F037). `Path("")` is `Path(".")` — the process CWD —
# so a CLEARED Settings field used to round-trip to disk as "." and read back
# as a real, set folder that then dead-ends Organize and dedupe→Move.
# ---------------------------------------------------------------------------


def test_coerce_blank_optional_path_is_none() -> None:
    assert _coerce("Path | None", "") is None
    assert _coerce("Path | None", "   ") is None


def test_coerce_required_path_rejects_blank() -> None:
    """No "unset" exists for a required Path — snap back to the default."""
    with pytest.raises(ValueError):
        _coerce("Path", "")


def test_coerce_path_strips_surrounding_whitespace() -> None:
    assert _coerce("Path | None", "  D:/DJ/Sorted  ") == Path("D:/DJ/Sorted")


def test_cleared_target_root_persists_as_null_not_dot(tmp_path: Path) -> None:
    """End-to-end: clearing the field in Settings must not save '.'."""
    cfg = VibechekConfig._from_dict({
        "organization": {"target_root": ""},
        "duplicates": {"review_folder": "   "},
    })
    assert cfg.organization.target_root is None
    assert cfg.duplicates.review_folder is None

    target = tmp_path / "config.json"
    cfg.save(target)
    raw = json.loads(target.read_text(encoding="utf-8"))
    assert raw["organization"]["target_root"] is None
    assert raw["duplicates"]["review_folder"] is None


def test_blank_required_path_falls_back_to_default_with_a_note() -> None:
    warnings: list[str] = []
    cfg = _subset(AnalysisConfig, {"models_dir": ""}, warnings)
    assert cfg.models_dir == AnalysisConfig().models_dir
    assert warnings and "models_dir" in warnings[0]


# ---------------------------------------------------------------------------
# Enum snap-back parity (audit F084): `use_gpu` and `duplicates.action` skipped
# the validation every other enum in this file gets. An out-of-set `use_gpu`
# is neither "on" nor "off" downstream — the GPU block is skipped entirely and
# `gpu_reason`, the field that exists to explain zero GPU workers, stays None.
# ---------------------------------------------------------------------------


def test_use_gpu_out_of_set_snaps_back_with_a_note(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="vibechek.config")
    warnings: list[str] = []
    cfg = _subset(AnalysisConfig, {"use_gpu": "ON"}, warnings)
    assert cfg.use_gpu == "auto"
    assert warnings and "use_gpu" in warnings[0] and "ON" in warnings[0]
    assert any("use_gpu" in r.message for r in caplog.records)


@pytest.mark.parametrize("value", ["auto", "on", "off"])
def test_use_gpu_valid_values_pass_through(value: str) -> None:
    warnings: list[str] = []
    cfg = _subset(AnalysisConfig, {"use_gpu": value}, warnings)
    assert cfg.use_gpu == value
    assert warnings == []


def test_duplicate_action_out_of_set_snaps_back_to_report() -> None:
    """`DuplicateAction("DELETE")` raises mid-run; the user gets a note first,
    and the fallback is the non-destructive action."""
    from vibechek.config import DuplicateConfig

    warnings: list[str] = []
    cfg = _subset(DuplicateConfig, {"action": "DELETE"}, warnings)
    assert cfg.action == "report"
    assert warnings and "action" in warnings[0]


@pytest.mark.parametrize("value", ["report", "move", "trash"])
def test_duplicate_action_valid_values_pass_through(value: str) -> None:
    from vibechek.config import DuplicateConfig

    warnings: list[str] = []
    cfg = _subset(DuplicateConfig, {"action": value}, warnings)
    assert cfg.action == value
    assert warnings == []


# ---------------------------------------------------------------------------
# Cross-field fix-up: the vocal band. `apply_ml_tags` HARD-REJECTS
# `vocal_instrumental_max >= vocal_full_min`, so a hand-edited config.json with
# an inverted band used to load clean and then fail every tagging run. The
# Settings sliders cross-clamp, but nothing protects a file edited by hand.
# ---------------------------------------------------------------------------


def test_inverted_vocal_band_snaps_both_back_with_a_note(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="vibechek.config")
    warnings: list[str] = []
    cfg = _subset(
        TaggingConfig,
        {"vocal_instrumental_max": 0.9, "vocal_full_min": 0.5},
        warnings,
    )
    assert cfg.vocal_instrumental_max == TaggingConfig().vocal_instrumental_max
    assert cfg.vocal_full_min == TaggingConfig().vocal_full_min
    assert warnings and "vocal_instrumental_max" in warnings[0]
    assert "vocal_full_min" in warnings[0]
    assert any("vocal" in r.message for r in caplog.records)


def test_collapsed_vocal_band_is_rejected_too() -> None:
    """Equal thresholds leave no Light Vocal band AND trip the same `>=`
    rejection in `apply_ml_tags` — snap them back, not just the inverted case."""
    warnings: list[str] = []
    cfg = _subset(
        TaggingConfig,
        {"vocal_instrumental_max": 0.8, "vocal_full_min": 0.8},
        warnings,
    )
    assert cfg.vocal_instrumental_max == TaggingConfig().vocal_instrumental_max
    assert cfg.vocal_full_min == TaggingConfig().vocal_full_min
    assert warnings


def test_one_sided_vocal_edit_is_checked_against_the_default() -> None:
    """Only one threshold in the file: the other is the dataclass default, and
    the pair still has to make sense (0.95 sits above the 0.88 default min)."""
    warnings: list[str] = []
    cfg = _subset(TaggingConfig, {"vocal_instrumental_max": 0.95}, warnings)
    assert cfg.vocal_instrumental_max == TaggingConfig().vocal_instrumental_max
    assert warnings


def test_valid_vocal_band_passes_through_untouched() -> None:
    warnings: list[str] = []
    cfg = _subset(
        TaggingConfig,
        {"vocal_instrumental_max": 0.60, "vocal_full_min": 0.95},
        warnings,
    )
    assert cfg.vocal_instrumental_max == pytest.approx(0.60)
    assert cfg.vocal_full_min == pytest.approx(0.95)
    assert warnings == []


def test_vocal_band_fixup_survives_a_coercion_failure_on_one_side() -> None:
    """A garbage `vocal_full_min` already falls back to the 0.88 default; the
    band check must then run against THAT, not against the garbage."""
    warnings: list[str] = []
    cfg = _subset(
        TaggingConfig,
        {"vocal_instrumental_max": 0.93, "vocal_full_min": "loud"},
        warnings,
    )
    assert cfg.vocal_instrumental_max == TaggingConfig().vocal_instrumental_max
    assert cfg.vocal_full_min == TaggingConfig().vocal_full_min
    # Two notes: the coercion failure, then the band snap-back.
    assert len(warnings) == 2
