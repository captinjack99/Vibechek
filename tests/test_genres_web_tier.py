"""Tests for the modern-Beatport taxonomy additions + the web tier of
genres.reconcile_genre (tag › grounded web › audio)."""

from __future__ import annotations

import pytest

from vibechek.genres import reconcile_genre, split_tag_genre


@pytest.mark.parametrize(("genre", "parent", "sub"), [
    ("Tech House", "House", "Tech House"),
    ("Bass House", "House", "Bass House"),
    ("Funky House", "House", "Funky House"),
    ("Future House", "House", "Future House"),
    ("Afro House", "House", "Afro House"),
    ("Melodic House & Techno", "Melodic House & Techno", "Melodic House & Techno"),
    ("Melodic Dubstep", "Dubstep", "Melodic Dubstep"),
    ("Future Rave", "Big Room", "Future Rave"),
    ("Nu-Disco", "Disco", "Nu-Disco"),
])
def test_modern_subgenres_map_to_sensible_parents(genre, parent, sub) -> None:
    assert split_tag_genre(genre) == (parent, sub)


def test_web_used_when_tag_generic() -> None:
    r = reconcile_genre("House", "House", 0.4, "Dance/Pop", policy="prefer_tag",
                        web_genre="Funky House", web_grounded=False)
    assert (r.genre, r.subgenre, r.source) == ("House", "Funky House", "web")


def test_web_used_when_no_tag() -> None:
    r = reconcile_genre("House", "House", 0.4, None, policy="prefer_tag",
                        web_genre="Tech House")
    assert r.subgenre == "Tech House" and r.source == "web"


def test_grounded_web_overrides_stale_specific_tag() -> None:
    # tag says Progressive House, but a GROUNDED web read says Melodic H&T (diff family)
    r = reconcile_genre("House", "Tech House", 0.5, "Progressive House",
                        policy="prefer_tag", web_genre="Melodic House & Techno",
                        web_grounded=True)
    assert r.genre == "Melodic House & Techno"
    assert r.source == "web_override"
    assert r.conflict is True


def test_ungrounded_web_does_not_override_specific_tag() -> None:
    r = reconcile_genre("House", "Tech House", 0.5, "Tech House", policy="prefer_tag",
                        web_genre="Trance", web_grounded=False)
    assert r.subgenre == "Tech House" and r.source == "tag"


def test_verified_web_overrides_inside_the_family_too() -> None:
    """A verified catalog read replaces the tag even when both sit in the House
    family. The old rule required a family-level disagreement, which cost 6
    exact answers on the adjudicated corpus and prevented none — "grounded" now
    means the lookup quoted the genre off a page naming this exact track, not
    that a model claimed a source."""
    r = reconcile_genre("House", "Deep House", 0.5, "Tech House", policy="prefer_tag",
                        web_genre="Bass House", web_grounded=True)
    assert r.subgenre == "Bass House" and r.source == "web_override"
    assert r.conflict is True     # the tag changed → still goes to the review queue


def test_verified_web_agreeing_with_the_tag_keeps_the_tag() -> None:
    r = reconcile_genre("House", "Deep House", 0.5, "Tech House", policy="prefer_tag",
                        web_genre="Tech House", web_grounded=True)
    assert r.subgenre == "Tech House" and r.source == "tag" and r.conflict is False


def test_tag_only_ignores_web() -> None:
    r = reconcile_genre("House", "Tech House", 0.5, "Progressive House",
                        policy="tag_only", web_genre="Melodic House & Techno",
                        web_grounded=True)
    assert r.subgenre == "Progressive House" and r.source == "tag"


def test_generic_web_genre_falls_back_to_audio_path() -> None:
    # an unusable web genre ("Dance") must not hijack reconciliation.
    r = reconcile_genre("House", "Tech House", 0.5, "Tech House", policy="prefer_tag",
                        web_genre="Dance", web_grounded=True)
    assert r.subgenre == "Tech House" and r.source == "tag"


def test_backward_compatible_without_web() -> None:
    # No web args → unchanged legacy behaviour.
    r = reconcile_genre("House", "Tech House", 0.5, "Deep House", policy="prefer_tag")
    assert r.subgenre == "Deep House" and r.source == "tag"
