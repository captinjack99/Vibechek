"""Tests for vibechek.genres (Discogs label aggregation)."""

from __future__ import annotations

from vibechek.genres import (
    GENRE_HIERARCHY,
    SUBGENRE_TO_PARENT,
    get_best_genre,
    parse_discogs_genre,
)


def test_parse_discogs_genre_split() -> None:
    assert parse_discogs_genre("Electronic---Tech House") == ("Electronic", "Tech House")


def test_parse_discogs_genre_no_delimiter() -> None:
    # No '---' means treat the whole label as both parent and subgenre
    assert parse_discogs_genre("Rock") == ("Rock", "Rock")


def test_parse_discogs_genre_empty() -> None:
    assert parse_discogs_genre("") == (None, None)
    assert parse_discogs_genre(None) == (None, None)


def test_subgenre_to_parent_is_built_correctly() -> None:
    # Spot-check a couple
    assert SUBGENRE_TO_PARENT["Deep House"] == "House"
    assert SUBGENRE_TO_PARENT["Minimal Techno"] == "Techno"
    assert SUBGENRE_TO_PARENT["Psy-Trance"] == "Trance"


def test_get_best_genre_aggregates_house_family() -> None:
    classes = [
        "Electronic---Deep House",
        "Electronic---Tech House",
        "Electronic---Acid House",
        "Electronic---Drum n Bass",
        "Rock---Indie Rock",
    ]
    # Heavily skewed toward House family — aggregation should give high confidence
    preds = [0.3, 0.25, 0.2, 0.05, 0.02]
    result = get_best_genre(preds, classes)

    assert result.genre == "House"
    # Combined confidence: 0.3 + 0.25 + 0.2 = 0.75
    assert result.confidence == 0.75
    # Most specific subgenre with highest score
    assert result.subgenre == "Deep House"


def test_get_best_genre_empty_returns_unknown() -> None:
    result = get_best_genre([], [])
    assert result.genre == "Unknown"
    assert result.subgenre == "Unknown"
    assert result.confidence == 0.0


def test_get_best_genre_low_confidence_filtered() -> None:
    classes = ["Electronic---Tech House"]
    preds = [0.01]  # Below default 0.05 floor
    result = get_best_genre(preds, classes)
    assert result.genre == "Unknown"


def test_genre_hierarchy_is_consistent() -> None:
    # Every subgenre in the hierarchy should reverse-look-up to its parent
    for parent, subs in GENRE_HIERARCHY.items():
        for sub in subs:
            assert SUBGENRE_TO_PARENT[sub] == parent


# --- Existing-tag vs ML reconciliation (configurable tag-trust) -------------

from vibechek.genres import is_specific_genre, reconcile_genre, split_tag_genre  # noqa: E402


def test_is_specific_genre_filters_generic_buckets() -> None:
    assert is_specific_genre("Tech House")
    assert is_specific_genre("Melodic House & Techno")
    # the "notoriously bad" generic Beatport buckets / junk:
    for junk in ("", "Dance/Pop", "Dance / Electronica", "Electronic", "Pop", "Unknown", "EDM"):
        assert not is_specific_genre(junk), junk


def test_split_tag_genre_uses_hierarchy() -> None:
    assert split_tag_genre("Tech House") == ("House", "Tech House")
    # a modern genre absent from the hierarchy keeps its specific label
    assert split_tag_genre("Melodic House & Techno") == (
        "Melodic House & Techno", "Melodic House & Techno")


def test_reconcile_prefer_tag_trusts_specific_tag() -> None:
    r = reconcile_genre("House", "Progressive House", 0.3, "Tech House")
    assert r.source == "tag" and r.subgenre == "Tech House" and r.genre == "House"


def test_reconcile_prefer_tag_ignores_generic_tag_uses_ml() -> None:
    r = reconcile_genre("House", "Bass House", 0.6, "Dance/Pop")
    assert r.source == "ml" and r.subgenre == "Bass House"


def test_reconcile_prefer_tag_ml_overrides_when_confident_and_disagrees() -> None:
    # A specific but WRONG tag is overridden only by a confident, disagreeing ML.
    r = reconcile_genre("Trance", "Trance", 0.95, "Tech House", "prefer_tag", 0.90)
    assert r.source == "ml_override" and r.genre == "Trance" and r.conflict
    # ...but a LOW-confidence ML does not override the specific tag.
    r2 = reconcile_genre("Trance", "Trance", 0.40, "Tech House", "prefer_tag", 0.90)
    assert r2.source == "tag" and r2.subgenre == "Tech House"


def test_reconcile_policies() -> None:
    # ml_only ignores even a specific tag
    assert reconcile_genre("Techno", "Techno", 0.5, "Tech House", "ml_only").source == "ml"
    # tag_only never lets ML override
    assert reconcile_genre("Trance", "Trance", 0.99, "Tech House", "tag_only").source == "tag"
    # prefer_ml uses ML unless weak
    assert reconcile_genre("Techno", "Techno", 0.7, "Tech House", "prefer_ml").source == "ml"
    assert reconcile_genre("Techno", "Techno", 0.2, "Tech House", "prefer_ml").source == "tag"
    # no tag -> ML regardless of policy
    assert reconcile_genre("Techno", "Techno", 0.5, "", "prefer_tag").source == "ml"


def test_build_report_applies_reconciliation() -> None:
    """The analyzer's final report reconciles ML genre against the existing tag."""
    from vibechek.analyzer import _build_report
    results = [{
        "path": "/m/x.flac", "filename": "x.flac",
        "existing_tags": {"genre": "Tech House"},
        "ml_analysis": {"ml_genre": "Trance", "ml_subgenre": "Trance",
                        "ml_genre_raw_confidence": 0.3, "ml_genre_confidence": 0.8},
    }]
    rep = _build_report(results, 1, in_progress=False,
                        genre_policy=("prefer_tag", 0.90))
    ml = rep["tracks"][0]["ml_analysis"]
    assert ml["ml_genre_source"] == "tag"
    assert ml["ml_subgenre"] == "Tech House"
    assert ml["ml_genre_audio"] == "Trance"   # pure-audio read preserved
    # a partial (in_progress) build must NOT reconcile
    results2 = [{"path": "/m/y.flac", "filename": "y.flac",
                 "existing_tags": {"genre": "Tech House"},
                 "ml_analysis": {"ml_genre": "Trance", "ml_subgenre": "Trance",
                                 "ml_genre_raw_confidence": 0.3}}]
    rep2 = _build_report(results2, 1, in_progress=True)
    assert "ml_genre_source" not in rep2["tracks"][0]["ml_analysis"]


def test_build_report_surfaces_override_conflict_for_review() -> None:
    """The final report must carry the provenance the library UI flags for
    review: a confident, disagreeing ML read overrides the tag, sets
    conflict=True, and preserves the pure-audio read so the panel can show
    'kept/changed your tag → X'."""
    from vibechek.analyzer import _build_report
    results = [{
        "path": "/m/x.flac", "filename": "x.flac",
        "existing_tags": {"genre": "Tech House"},
        "ml_analysis": {"ml_genre": "Trance", "ml_subgenre": "Trance",
                        "ml_genre_raw_confidence": 0.95, "ml_genre_confidence": 0.95},
    }]
    rep = _build_report(results, 1, in_progress=False,
                        genre_policy=("prefer_tag", 0.90))
    ml = rep["tracks"][0]["ml_analysis"]
    assert ml["ml_genre_source"] == "ml_override"
    assert ml["ml_genre_conflict"] is True
    assert ml["ml_genre_audio"] == "Trance"   # pre-reconcile read preserved
    assert ml["ml_genre"] == "Trance"          # the override won


def test_build_report_prefer_ml_records_discarded_tag_conflict() -> None:
    """Under prefer_ml a confident audio read WINS over a specific tag: the
    source is plain 'ml' (NOT 'ml_override') yet conflict is True and the tag was
    discarded. The library UI keys 'your tag was changed' off source != 'tag'
    (not the _override suffix) — this locks the backend contract that makes that
    framing correct, the exact gap an adversarial review caught."""
    from vibechek.analyzer import _build_report
    results = [{
        "path": "/m/x.flac", "filename": "x.flac",
        "existing_tags": {"genre": "Tech House"},
        "ml_analysis": {"ml_genre": "Trance", "ml_subgenre": "Trance",
                        "ml_genre_raw_confidence": 0.7},
    }]
    rep = _build_report(results, 1, in_progress=False,
                        genre_policy=("prefer_ml", 0.90))
    ml = rep["tracks"][0]["ml_analysis"]
    assert ml["ml_genre_source"] == "ml"      # plain ml — NO _override suffix
    assert ml["ml_genre_conflict"] is True     # ...but the tag was discarded
    assert ml["ml_genre"] == "Trance"          # ML won
    assert ml["ml_genre_audio"] == "Trance"


def test_mlresult_declares_provenance_fields() -> None:
    """The provenance fields the library UI reads must live ON the MLResult
    dataclass (so scripts/generate_ts_types.py emits them into the TS bindings)
    and default to None (so the wire dict — asdict() dropping None values —
    stays free of them until reconciliation stamps them on the final report)."""
    from vibechek.analyzer import MLResult
    m = MLResult()
    for fname in (
        "ml_genre_audio", "ml_subgenre_audio", "ml_genre_web",
        "ml_genre_web_grounded", "ml_genre_source", "ml_genre_conflict",
        "ml_vocal_audio", "ml_vocal_source",
    ):
        assert getattr(m, fname) is None
