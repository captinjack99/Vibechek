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
