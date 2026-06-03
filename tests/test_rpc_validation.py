"""Regression tests for write-path parameter validation at the RPC boundary.

The CLI clamps numeric inputs via click ranges; these tests pin the equivalent
guarantees on the RPC seam (audit 2026-06-01, rpc-cli-config MEDIUM findings):

  - id3_text_encoding out of {0,1,2,3} → falls back to 3 (UTF-8)
  - confidence thresholds clamped to [0, 1]
  - worker / skip counts clamped to >= 0
  - inverted vocal bands (instrumental_max >= full_min) rejected
  - save_config on a non-dict payload → INVALID_PARAMS (not APP_ERROR)
  - install_wsl distro validated against the safe charset
"""

from __future__ import annotations

import pytest

from vibechek import rpc

# ---------------------------------------------------------------------------
# The tiny helpers (unit level)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        (0, 0), (1, 1), (2, 2), (3, 3),
        (4, 3), (99, 3), (-1, 3),       # out of range → default
        ("1", 1), ("3", 3),             # numeric strings accepted
        ("nonsense", 3), (None, 3),     # garbage → default
        (1.0, 1),                       # float that's an exact int
    ],
)
def test_valid_id3_encoding(value, expected) -> None:
    assert rpc._valid_id3_encoding(value, 3) == expected


@pytest.mark.parametrize(
    "value,expected",
    [
        (0.0, 0.0), (1.0, 1.0), (0.5, 0.5),
        (-0.5, 0.0), (2.0, 1.0),        # clamped into range
        ("0.85", 0.85),                 # numeric string
        ("bad", 0.42), (None, 0.42),    # garbage → default
        (float("nan"), 0.42),           # NaN → default
    ],
)
def test_clamp01(value, expected) -> None:
    assert rpc._clamp01(value, 0.42) == pytest.approx(expected)


@pytest.mark.parametrize(
    "value,expected",
    [
        (0, 0), (4, 4), (-1, 0), (-100, 0),
        ("4", 4), ("-2", 0),
        ("bad", 0), (None, 0),
    ],
)
def test_nonneg_int(value, expected) -> None:
    assert rpc._nonneg_int(value, 0) == expected


def test_validate_distro_accepts_safe_names() -> None:
    assert rpc._validate_distro("Ubuntu-24.04") == "Ubuntu-24.04"
    assert rpc._validate_distro("Debian") == "Debian"
    assert rpc._validate_distro("openSUSE-Tumbleweed") == "openSUSE-Tumbleweed"


def test_validate_distro_falls_back_on_empty() -> None:
    assert rpc._validate_distro("", "Ubuntu-24.04") == "Ubuntu-24.04"
    assert rpc._validate_distro(None, "Ubuntu-24.04") == "Ubuntu-24.04"


@pytest.mark.parametrize(
    "bad",
    [
        "Ubuntu; rm -rf /",
        "Ubuntu' ; Start-Process calc",
        "Ubuntu`whoami`",
        "Ubuntu 24.04",          # space
        "../escape",
        "$(evil)",
    ],
)
def test_validate_distro_rejects_injection_shapes(bad) -> None:
    with pytest.raises(ValueError):
        rpc._validate_distro(bad)


# ---------------------------------------------------------------------------
# _apply_ml_tags builds a clamped/validated TaggingConfig
# ---------------------------------------------------------------------------


def _capture_apply_config(monkeypatch, params):
    """Run rpc._apply_ml_tags with apply_ml_tags + _load_analysis_payload
    stubbed, and return the TaggingConfig the handler built."""
    captured = {}

    def fake_apply(analysis, config, on_progress=None, dry_run=False):
        captured["config"] = config
        # Minimal stats-like object: asdict() needs a dataclass.
        from dataclasses import dataclass

        @dataclass
        class _Stats:
            ok: bool = True

        return _Stats()

    monkeypatch.setattr("vibechek.tagger.apply_ml_tags", fake_apply)
    monkeypatch.setattr(rpc, "_load_analysis_payload", lambda p: {"tracks": []})
    rpc._apply_ml_tags(params)
    return captured["config"]


def test_apply_ml_tags_clamps_confidence(monkeypatch) -> None:
    cfg = _capture_apply_config(monkeypatch, {"confidence": -0.3})
    assert cfg.genre_confidence_threshold == 0.0
    cfg = _capture_apply_config(monkeypatch, {"confidence": 5.0})
    assert cfg.genre_confidence_threshold == 1.0


def test_apply_ml_tags_validates_id3_encoding(monkeypatch) -> None:
    cfg = _capture_apply_config(monkeypatch, {"id3_text_encoding": 99})
    assert cfg.id3_text_encoding == 3  # fallback to UTF-8
    cfg = _capture_apply_config(monkeypatch, {"id3_text_encoding": 1})
    assert cfg.id3_text_encoding == 1  # valid value preserved


def test_apply_ml_tags_rejects_inverted_vocal_bands(monkeypatch) -> None:
    # instrumental_max >= full_min is logically inverted and would mislabel
    # every track — must raise (→ INVALID_PARAMS) rather than mangle the lib.
    with pytest.raises(ValueError, match="must be <"):
        _capture_apply_config(
            monkeypatch,
            {"vocal_instrumental_max": 0.9, "vocal_full_min": 0.5},
        )


def test_apply_ml_tags_accepts_ordered_vocal_bands(monkeypatch) -> None:
    cfg = _capture_apply_config(
        monkeypatch,
        {"vocal_instrumental_max": 0.6, "vocal_full_min": 0.9},
    )
    assert cfg.vocal_instrumental_max == 0.6
    assert cfg.vocal_full_min == 0.9


def test_apply_ml_tags_forwards_settings_toggles(monkeypatch) -> None:
    cfg = _capture_apply_config(
        monkeypatch,
        {"write_subgenre_as_main_genre": False, "backup_before_write": False},
    )
    assert cfg.write_subgenre_as_main_genre is False
    assert cfg.backup_before_write is False


# ---------------------------------------------------------------------------
# _analyze_directory clamps workers/skip/limit
# ---------------------------------------------------------------------------


def test_analyze_directory_clamps_negative_workers(monkeypatch) -> None:
    captured = {}

    def fake_analyze(library_path, config=None, on_progress=None, on_track=None,
                     output_path=None, skip=0, limit=None, skip_paths=None):
        captured["workers"] = config.workers
        captured["skip"] = skip
        captured["limit"] = limit
        return {"summary": {}, "tracks": []}

    monkeypatch.setattr("vibechek.analyzer.analyze_directory", fake_analyze)
    # auto_save False so we don't touch library_state on disk.
    rpc._analyze_directory({
        "path": "/tmp/whatever",
        "workers": -4,
        "skip": -10,
        "limit": -1,
        "auto_save": False,
    })
    assert captured["workers"] == 0   # negative → 0 (auto)
    assert captured["skip"] == 0
    assert captured["limit"] is None  # negative limit → None (no cap)


# ---------------------------------------------------------------------------
# _save_config payload-type guard
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [["a", "list"], "a string", 42, 3.14])
def test_save_config_non_dict_raises_valueerror(bad) -> None:
    # ValueError is what the dispatcher maps to INVALID_PARAMS — NOT the
    # AttributeError-from-.get that previously became APP_ERROR + traceback.
    with pytest.raises(ValueError):
        rpc._save_config({"config": bad})


def test_save_config_dict_payload_ok(monkeypatch, tmp_path) -> None:
    from vibechek import config as cfg_mod
    monkeypatch.setattr(cfg_mod, "CONFIG_FILE", tmp_path / "config.json")
    out = rpc._save_config({"config": {"tagging": {"id3_text_encoding": 1}}})
    assert "saved_to" in out


# ---------------------------------------------------------------------------
# _rebuild_report payload-type guard (handle_duplicates)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["notadict", ["a", "list"], 42, None])
def test_rebuild_report_rejects_non_dict(bad) -> None:
    # A non-dict report previously crashed deep inside with an opaque
    # AttributeError ('str' object has no attribute 'get') → APP_ERROR +
    # traceback. The seam guard raises ValueError → INVALID_PARAMS. The guard
    # runs BEFORE the vibechek.duplicates import, so this stays CI-clean even
    # without the dedupe deps installed.
    with pytest.raises(ValueError, match="must be an object"):
        rpc._rebuild_report(bad)
