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
