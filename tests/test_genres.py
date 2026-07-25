"""Tests for vibechek.genres (Discogs label aggregation)."""

from __future__ import annotations

import pytest

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

from vibechek.genres import (  # noqa: E402
    is_specific_genre,
    is_usable_genre_label,
    reconcile_genre,
    split_tag_genre,
)


def test_is_specific_genre_filters_generic_buckets() -> None:
    assert is_specific_genre("Tech House")
    assert is_specific_genre("Melodic House & Techno")
    # the "notoriously bad" generic Beatport buckets / junk:
    for junk in ("", "Dance/Pop", "Dance / Electronica", "Electronic", "Pop", "Unknown", "EDM"):
        assert not is_specific_genre(junk), junk
    # both spellings of the retailer's Electronica/Dance bucket
    for junk in ("Electronica/Dance", "Electronica / Dance", "electronica/dance"):
        assert not is_specific_genre(junk), junk


def test_electro_is_not_a_trustworthy_file_tag() -> None:
    """"Electro" names a real genre but is sprayed over whole pools, so it is not
    trusted AS A TAG. Measured on the 86-track adjudicated corpus: +1 exact,
    +1 family, zero broken (internal/bughunt/score_generic_set.py)."""
    for spelling in ("Electro", "electro", "  ELECTRO  "):
        assert not is_specific_genre(spelling), spelling
    # ...but a genre we RESOLVED ourselves is still a usable answer.
    assert is_usable_genre_label("Electro")


def test_house_stays_a_trusted_tag() -> None:
    """The measurement harness distrusts "House"; production must NOT. Adding it
    cost 1 exact + 1 family and broke a track while fixing none, and it is the
    largest tag in a real library (2,645/12,145 files) with the curation
    agreeing. Subgenres obviously stay trusted too."""
    for tag in ("House", "house", "Tech House", "Deep House", "Progressive House"):
        assert is_specific_genre(tag), tag


def test_playlist_phrase_in_the_genre_field_is_not_a_genre() -> None:
    """A long phrase naming no genre we know is a playlist / record-pool label that
    landed in the genre field. Left trusted it becomes the track's genre AND an
    organize destination folder. Measured non-harmful on the adjudicated corpus and
    zero-false-positive over a 12,145-file library
    (internal/bughunt/score_unplaceable_rule.py)."""
    for junk in ("Hypeddit Top Weekly Picks",
                 "Electronic Pop Pop Rock Soft Rock Synth-Pop",
                 "Dance Deep House House Edm",
                 "Chillwave Electronic Downtempo Future Bass Dream Pop Trip-Hop"):
        assert not is_specific_genre(junk), junk


def test_playlist_phrase_rule_stays_narrow() -> None:
    """The boundaries that keep the rule from eating real genres. Every case here
    is a tag that occurs in a real library; wider variants of this rule were
    measured and refuted (they broke "Stutter House" and a 4-genre list whose
    first member was family-correct)."""
    # placeable in the hierarchy, however many words
    assert is_specific_genre("Melodic House & Techno")
    # carries a list separator -> a list of genres still names genres
    for sep in ("Techno (Peak Time / Driving)", "Bassline / Speed Garage",
                "Dubstep, House, Electronic, Trance", "UK Garage / Bassline",
                "Minimal / Deep Tech", "Juke X Footwork X D&B"):
        assert is_specific_genre(sep), sep
    # under the word threshold — an obscure genre is still a genre
    for short in ("Reggaeton", "Stutter House", "Electro Swing Jazz", "Donk"):
        assert is_specific_genre(short), short
    # single-token pool labels are NOT caught by this rule (by design — nothing
    # structural separates "TMU" from "Donk"); they would need their own evidence
    for pool in ("TMU", "Urban", "White Label", "Essentials"):
        assert is_specific_genre(pool), pool


def test_is_usable_genre_label_is_narrower_than_tag_trust() -> None:
    # Content-free labels are unusable from ANY source.
    for junk in ("", None, "Unknown", "Dance / Pop", "EDM", "Electronic",
                 "Electronica/Dance", "Other"):
        assert not is_usable_genre_label(junk), junk
    for good in ("Tech House", "Electro", "House", "Melodic House & Techno"):
        assert is_usable_genre_label(good), good
    # The two sets differ ONLY on the spray-tag entries — every label the tag
    # test accepts must also pass the (weaker) label test.
    for tag in ("Tech House", "House", "Techno", "Trance", "Electro"):
        assert not is_specific_genre(tag) or is_usable_genre_label(tag), tag


def test_split_tag_genre_uses_hierarchy() -> None:
    assert split_tag_genre("Tech House") == ("House", "Tech House")
    # a modern genre absent from the hierarchy keeps its specific label
    assert split_tag_genre("Melodic House & Techno") == (
        "Melodic House & Techno", "Melodic House & Techno")


def test_every_alias_target_exists_in_the_hierarchy() -> None:
    """An alias pointing at a name the hierarchy does not carry is worse than no
    alias: the tag stays unplaceable while the table makes it look handled."""
    from vibechek.genres import _GENRE_TAG_ALIASES, _KNOWN_GENRE_NAMES
    missing = sorted(v for v in _GENRE_TAG_ALIASES.values()
                     if v not in _KNOWN_GENRE_NAMES)
    assert not missing, f"alias targets absent from the hierarchy: {missing}"


def test_spelling_variants_resolve_to_the_hierarchy_name() -> None:
    """A tag the hierarchy cannot place is usually a HIERARCHY GAP, not junk
    (d7921d7). These are the gaps that are pure spelling: left unaliased the raw
    string becomes the track's genre AND its own organize folder, so "Hip-Hop"
    and "Hip Hop" are two destinations for one genre."""
    assert split_tag_genre("Hip-Hop") == ("Hip Hop", "Hip Hop")
    assert split_tag_genre("D&B") == ("Drum & Bass", "Drum & Bass")
    assert split_tag_genre("Synth Pop") == ("Synth-pop", "Synth-pop")
    assert split_tag_genre("Rock & Roll") == ("Rock", "Rock")
    # lookups ignore case and surrounding space
    assert split_tag_genre("  hip-hop ") == ("Hip Hop", "Hip Hop")


def test_beatport_slash_names_resolve_to_their_family() -> None:
    """Beatport ships these as SINGLE genre names, not lists. Each target was
    checked against the family its files independently resolve to
    (internal/bughunt/score_genre_aliases.py route 2)."""
    assert split_tag_genre("Nu Disco / Disco") == ("Disco", "Nu-Disco")
    assert split_tag_genre("Organic House / Downtempo") == ("House", "Organic House")
    assert split_tag_genre("Indie Dance / Nu Disco") == ("Indie Dance", "Indie Dance")
    assert split_tag_genre("UK Garage / Bassline") == ("Dubstep", "Bassline")
    assert split_tag_genre("Bassline / Speed Garage") == ("Dubstep", "Bassline")


def test_minimal_deep_tech_lands_in_techno_not_house() -> None:
    """The measured one. "Minimal / Deep Tech" reads like plain "Minimal", but the
    hierarchy files Minimal under HOUSE while 85% of the tag's tracks (22/26
    independently resolved, 10.4x the library base rate) come back techno-family.
    Aliasing it to "Minimal" would have moved 6 of every 7 into the wrong folder."""
    assert split_tag_genre("Minimal / Deep Tech") == ("Techno", "Minimal Techno")
    # the plain name keeps its existing (House) placement — untouched by this
    assert split_tag_genre("Minimal") == ("House", "Minimal")


def test_bracketed_beatport_qualifier_is_stripped() -> None:
    """"Techno (Peak Time / Driving)" narrows a genre we already carry. A rule
    beats a table here because Beatport reorganizes its qualifiers."""
    assert split_tag_genre("Techno (Peak Time / Driving)") == ("Techno", "Techno")
    assert split_tag_genre("Trance (Main Floor)") == ("Trance", "Trance")
    assert split_tag_genre("Trance (Raw / Deep / Hypnotic)") == ("Trance", "Trance")
    assert split_tag_genre("Deep House (Electronic)") == ("House", "Deep House")


def test_bracket_rule_only_fires_when_the_remainder_is_a_genre() -> None:
    """Self-limiting by design: strip the qualifier only if what is left is a name
    we can place. Otherwise a track title in the genre field, or an unknown
    bracketed genre, would silently lose half of itself."""
    for untouched in ("Chemicals (Extended Mix)", "Reggaeton (Latin)",
                      "Hypeddit Top Weekly Picks"):
        assert split_tag_genre(untouched) == (untouched, untouched), untouched


def test_french_electronic_bucket_is_content_free() -> None:
    """"Électronique" is "Electronic" in another language — already generic, so it
    is listed rather than aliased (15 files in a real library)."""
    for spelling in ("Électronique", "électronique", " ÉLECTRONIQUE "):
        assert not is_specific_genre(spelling), spelling
        assert not is_usable_genre_label(spelling), spelling


def test_aliases_do_not_rescue_a_genre_the_hierarchy_lacks() -> None:
    """Scope guard. These name real genres the taxonomy has no entry for at all —
    a product decision, not a spelling fix — so they must stay exactly as they
    are: trusted, specific, and their own label."""
    for gap in ("Reggaeton", "Soundtrack", "Acid Jazz", "Bossa Nova", "Salsa",
                "Dembow", "Melodic Bass", "African"):
        assert is_specific_genre(gap), gap
        assert split_tag_genre(gap) == (gap, gap), gap


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


def test_reconcile_electro_tag_defers_but_electro_web_read_stands() -> None:
    """The asymmetry the split set buys: an "Electro" TAG is no tag at all, while
    an "Electro" genre we resolved online is a perfectly good answer.

    This is the "Ain't Giving Up" case from the adjudicated corpus — tagged
    Electro, actually House, and the audio model had it right all along.
    """
    # tag side: falls through to the audio read, no conflict flag (there is no
    # tag to be in conflict WITH)
    r = reconcile_genre("House", "House", 0.42, "Electro", "prefer_tag")
    assert r.source == "ml" and r.genre == "House" and not r.conflict

    # web side: a verified catalog read of Electro is usable, and with no
    # trustworthy tag in the way it simply wins
    w = reconcile_genre("House", "Bass House", 0.8, "Electro", "prefer_tag",
                        web_genre="Electro", web_grounded=True)
    assert w.source == "web" and w.subgenre == "Electro"

    # ...and it still overrides a genuinely specific tag
    o = reconcile_genre("House", "Bass House", 0.8, "Tech House", "prefer_tag",
                        web_genre="Electro", web_grounded=True)
    assert o.source == "web_override" and o.subgenre == "Electro" and o.conflict

    # a CONTENT-FREE web read is discarded from either source, tag or not
    for tag in ("Electro", "Tech House"):
        d = reconcile_genre("Techno", "Techno", 0.5, tag, "prefer_tag",
                            web_genre="Dance / Pop", web_grounded=True)
        assert d.source != "web" and "Dance" not in d.genre, tag


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


def test_build_report_stamps_web_provenance(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the online lookup runs, its read + verification flag land on the
    record so the UI's 'Genre sources' panel can show the web row honestly."""
    from vibechek import analyzer, genre_web
    monkeypatch.setattr(genre_web, "resolver_ready", lambda: True)
    monkeypatch.setattr(
        genre_web, "resolve",
        lambda *a, **k: {"genre": "Trance", "confidence": 0.9,
                         "source_matched": True, "used_web": True},
    )
    results = [{
        "path": "/m/x.flac", "filename": "x.flac",
        "existing_tags": {"genre": "Tech House", "artist": "A", "title": "T"},
        "ml_analysis": {"ml_genre": "House", "ml_subgenre": "Deep House",
                        "ml_genre_raw_confidence": 0.4},
    }]
    rep = analyzer._build_report(
        results, 1, in_progress=False,
        web_cfg={"enabled": True, "backend": "ollama"},
    )
    ml = rep["tracks"][0]["ml_analysis"]
    assert ml["ml_genre_web"] == "Trance"
    assert ml["ml_genre_web_grounded"] is True
    # A grounded web read that disagrees with the specific tag's family overrides it.
    assert ml["ml_genre_source"] == "web_override"
    assert ml["ml_genre_conflict"] is True


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
