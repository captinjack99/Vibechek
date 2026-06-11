"""Genre taxonomy and the classification logic that maps Discogs-400 model
predictions into DJ-friendly genre / subgenre labels.

The Discogs-400 model returns labels like 'Electronic---Tech House'. DJs don't
think in terms of Discogs's full taxonomy — they want 'House' (parent) and
'Tech House' (subgenre). The maps below define that translation.

Everything in this module is pure logic — no I/O, no ML. Safe to import without
essentia installed.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Taxonomy — parent genre → known subgenres
# ---------------------------------------------------------------------------

GENRE_HIERARCHY: dict[str, list[str]] = {
    "House": [
        "Deep House", "Tech House", "Electro House", "Progressive House",
        "Acid House", "Tribal House", "Garage House", "Euro House",
        "Italo House", "Ghetto House", "Tropical House", "Hard House",
        "Hip-House", "Minimal", "Speed Garage", "UK Garage",
        # Modern Beatport house subgenres (recognized by the CLAP/web genre sources)
        "Bass House", "Funky House", "Future House", "Afro House", "Organic House",
    ],
    "Techno": ["Deep Techno", "Dub Techno", "Hard Techno", "Minimal Techno", "Schranz"],
    "Trance": [
        "Progressive Trance", "Psy-Trance", "Goa Trance", "Hard Trance",
        "Tech Trance", "Hands Up",
    ],
    "Drum n Bass": ["Jungle", "Halftime"],
    "Hardcore": [
        "Happy Hardcore", "Gabber", "Hardstyle", "Speedcore", "Jumpstyle", "Makina",
    ],
    "Breaks": ["Breakbeat", "Progressive Breaks", "Big Beat", "Breakcore"],
    "Dubstep": ["Bassline", "Grime", "Melodic Dubstep", "Riddim"],
    "Disco": ["Nu-Disco", "Euro-Disco", "Italo-Disco", "Disco Polo"],
    "Ambient": ["Dark Ambient", "Drone", "New Age"],
    "Downtempo": ["Trip Hop", "Chillwave"],
    "Industrial": ["EBM", "Power Electronics", "Rhythmic Noise", "Noise", "Neofolk"],
    # Modern Beatport top-level genres that don't nest under the above.
    "Melodic House & Techno": [],
    "Indie Dance": [],
    "Big Room": ["Future Rave"],
    "Midtempo Bass": [],
    "Electronica": [],
}

# Reverse lookup: subgenre → parent
SUBGENRE_TO_PARENT: dict[str, str] = {
    sub: parent
    for parent, subs in GENRE_HIERARCHY.items()
    for sub in subs
}

# DJ-friendly display name overrides (e.g. "Drum n Bass" → "Drum & Bass")
DJ_GENRE_MAP: dict[str, str] = {
    # House variants
    "House": "House", "Deep House": "Deep House", "Tech House": "Tech House",
    "Electro House": "Electro House", "Progressive House": "Progressive House",
    "Tribal House": "Tribal House", "Garage House": "UK Garage", "UK Garage": "UK Garage",
    "Acid House": "Acid House", "Euro House": "Euro House", "Italo House": "Italo House",
    "Ghetto House": "Ghetto House", "Tropical House": "Tropical House", "Hard House": "Hard House",
    "Hip-House": "Hip-House", "Speed Garage": "Speed Garage", "Minimal": "Minimal",
    # Modern Beatport house + crossover subgenres (CLAP/web genre vocabulary)
    "Bass House": "Bass House", "Funky House": "Funky House", "Future House": "Future House",
    "Afro House": "Afro House", "Organic House": "Organic House",
    "Melodic House & Techno": "Melodic House & Techno", "Indie Dance": "Indie Dance",
    "Big Room": "Big Room", "Future Rave": "Future Rave", "Midtempo Bass": "Midtempo Bass",
    "Melodic Dubstep": "Melodic Dubstep", "Riddim": "Riddim", "Electronica": "Electronica",
    # Techno
    "Techno": "Techno", "Minimal Techno": "Minimal Techno",
    "Deep Techno": "Deep Techno", "Hard Techno": "Hard Techno", "Dub Techno": "Dub Techno",
    "Schranz": "Hard Techno",
    # Trance
    "Trance": "Trance", "Progressive Trance": "Progressive Trance",
    "Psy-Trance": "Psytrance", "Goa Trance": "Psytrance",
    "Hard Trance": "Hard Trance", "Tech Trance": "Tech Trance", "Hands Up": "Hands Up",
    # Bass music
    "Drum n Bass": "Drum & Bass", "Jungle": "Jungle", "Halftime": "Halftime",
    "Dubstep": "Dubstep", "Bassline": "Bassline", "Grime": "Grime",
    "Breaks": "Breaks", "Breakbeat": "Breaks", "Big Beat": "Big Beat",
    "Progressive Breaks": "Progressive Breaks", "Breakcore": "Breakcore",
    # Hardcore
    "Hardcore": "Hardcore", "Happy Hardcore": "Happy Hardcore", "Gabber": "Gabber",
    "Hardstyle": "Hardstyle", "Speedcore": "Hardcore",
    "Jumpstyle": "Jumpstyle", "Makina": "Makina",
    # Other electronic
    "Electro": "Electro", "Electroclash": "Electroclash",
    "IDM": "IDM", "Leftfield": "Leftfield",
    "Ambient": "Ambient", "Dark Ambient": "Dark Ambient", "Drone": "Drone",
    "Downtempo": "Downtempo", "Trip Hop": "Trip Hop", "Chillwave": "Chillwave",
    "Synthwave": "Synthwave", "Synth-pop": "Synth-pop", "Vaporwave": "Vaporwave",
    "EBM": "EBM", "Industrial": "Industrial", "Darkwave": "Darkwave",
    "Disco": "Disco", "Nu-Disco": "Nu-Disco",
    "Euro-Disco": "Euro-Disco", "Italo-Disco": "Italo-Disco",
    "Eurodance": "Eurodance", "Eurobeat": "Eurobeat", "Italodance": "Italodance",
    "Dance-pop": "Dance Pop", "Hi NRG": "Hi-NRG",
    "New Beat": "New Beat", "New Wave": "New Wave",
    # Non-electronic (for mixed collections)
    "Hip Hop": "Hip Hop", "Trap": "Trap", "Bass Music": "Bass Music", "Miami Bass": "Miami Bass",
    "Pop": "Pop", "Rock": "Rock", "R&B": "R&B", "Soul": "Soul", "Funk": "Funk",
    "Reggae": "Reggae", "Dancehall": "Dancehall", "Dub": "Dub",
    "Latin": "Latin", "Jazz": "Jazz", "Blues": "Blues",
    "K-pop": "K-Pop", "J-pop": "J-Pop",
}

# Canonical display names must inherit their hierarchy parent too: DJ_GENRE_MAP
# renames e.g. "Psy-Trance" → "Psytrance", but SUBGENRE_TO_PARENT was keyed on
# the raw hierarchy names only — so split_tag_genre("Psytrance") lost its Trance
# parent, and a same-family web/tag pair ("Psytrance" vs "Trance") counted as a
# family CONFLICT, enabling a wrongful grounded-web override of a correct tag.
for _parent, _subs in GENRE_HIERARCHY.items():
    for _sub in _subs:
        _canon_name = DJ_GENRE_MAP.get(_sub, _sub)
        if _canon_name not in SUBGENRE_TO_PARENT:
            SUBGENRE_TO_PARENT[_canon_name] = _parent
del _parent, _subs, _canon_name

# Discogs parent categories that map to DJ-friendly genre buckets when subgenre
# information is too generic to use as the main genre.
_PARENT_CATEGORY_OVERRIDES = {
    "Funk / Soul": "Funk/Soul",
    "Hip Hop": "Hip Hop",
    "Rock": "Rock",
    "Pop": "Pop",
    "Reggae": "Reggae",
}


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


@dataclass
class GenrePrediction:
    raw: str
    parent_category: str | None
    subgenre: str | None
    dj_name: str | None
    confidence: float


@dataclass
class GenreResult:
    genre: str
    subgenre: str
    confidence: float
    raw_confidence: float = 0.0
    all_predictions: list[GenrePrediction] = field(default_factory=list)


UNKNOWN = GenreResult(genre="Unknown", subgenre="Unknown", confidence=0.0)


def parse_discogs_genre(raw_genre: str | None) -> tuple[str | None, str | None]:
    """Split 'Electronic---Tech House' into ('Electronic', 'Tech House').

    Returns (None, None) on empty input. Without '---', the whole label is
    treated as both parent and subgenre.
    """
    if not raw_genre:
        return None, None
    parts = raw_genre.split("---")
    if len(parts) >= 2:
        return parts[0], parts[-1]
    return raw_genre, raw_genre


def get_best_genre(
    predictions: Sequence[float],
    genre_classes: Sequence[str],
    min_confidence: float = 0.05,
    top_n: int = 15,
) -> GenreResult:
    """Aggregate Discogs-400 predictions into a DJ-friendly (genre, subgenre).

    The model returns 400 fine-grained Discogs labels per analysis frame. We
    take the per-class average across frames (caller's responsibility), then:
      1. Keep the top N over `min_confidence`.
      2. Aggregate "related" subgenres (same DJ family) so 'House' isn't
         penalized for being split across 8 different House subgenres.
      3. Pick the most specific subgenre that crosses a confidence floor.
    """
    if not predictions or not genre_classes:
        return UNKNOWN

    # Sort indices descending by score, take top_n
    indexed = sorted(
        range(len(predictions)),
        key=lambda i: predictions[i],
        reverse=True,
    )[:top_n]

    top: list[GenrePrediction] = []
    for idx in indexed:
        conf = float(predictions[idx])
        if conf < min_confidence:
            continue
        parent_category, subgenre = parse_discogs_genre(genre_classes[idx])
        top.append(GenrePrediction(
            raw=genre_classes[idx],
            parent_category=parent_category,
            subgenre=subgenre,
            dj_name=DJ_GENRE_MAP.get(subgenre or "", subgenre),
            confidence=conf,
        ))

    if not top:
        return UNKNOWN

    pri = top[0]
    top_subgenre = pri.subgenre or ""
    top_parent_category = pri.parent_category

    # Determine the DJ-friendly parent for the top prediction
    if top_subgenre in SUBGENRE_TO_PARENT:
        top_dj_parent = SUBGENRE_TO_PARENT[top_subgenre]
    elif top_subgenre in GENRE_HIERARCHY:
        top_dj_parent = top_subgenre
    else:
        top_dj_parent = top_subgenre

    # Aggregate confidence across the family
    if top_dj_parent in GENRE_HIERARCHY:
        related = set(GENRE_HIERARCHY[top_dj_parent]) | {top_dj_parent}
    else:
        related = {top_subgenre}

    # `top` is sorted by confidence descending and `top[0]` is the leader, so
    # the chosen subgenre is always the top prediction's subgenre. (An earlier
    # version tried to "promote a more specific sibling" inside this loop, but
    # the guard `pred.confidence > leader.confidence` could never be true for a
    # later, lower-confidence prediction — it was dead code that always fell
    # through to `top_subgenre`.) We keep the loop solely to sum the family's
    # aggregate confidence across related subgenres.
    family_confidence = 0.0
    best_subgenre = top_subgenre

    for pred in top:
        sub = pred.subgenre or ""
        is_related = (
            sub == top_subgenre
            or sub in related
            or (
                pred.parent_category == top_parent_category
                and sub in SUBGENRE_TO_PARENT
                and SUBGENRE_TO_PARENT[sub] == top_dj_parent
            )
        )
        if is_related:
            family_confidence += pred.confidence

    dj_genre = DJ_GENRE_MAP.get(top_dj_parent, top_dj_parent)
    dj_subgenre = DJ_GENRE_MAP.get(best_subgenre, best_subgenre)

    # For non-electronic genres where subgenre == genre, prefer the parent
    # category if we have a friendlier name for it
    if dj_genre == dj_subgenre and top_parent_category not in (None, "Electronic"):
        dj_genre = _PARENT_CATEGORY_OVERRIDES.get(top_parent_category, dj_genre)

    return GenreResult(
        genre=dj_genre or "Unknown",
        subgenre=dj_subgenre or "Unknown",
        confidence=round(min(family_confidence, 1.0), 3),
        raw_confidence=round(pri.confidence, 3),
        all_predictions=top[:5],
    )


# ---------------------------------------------------------------------------
# Existing-tag vs ML reconciliation
# ---------------------------------------------------------------------------

# Generic / low-quality genre tags that must NOT be trusted over the audio model
# — these are the "notoriously bad" Beatport broad buckets and catch-alls. A tag
# in this set is treated as "no usable tag" so ML decides.
_GENERIC_GENRE_TAGS: frozenset[str] = frozenset({
    "", "unknown", "other", "others", "misc", "miscellaneous", "various",
    "electronic", "electronica", "dance", "dance/electronic", "dance / electronic",
    "dance/pop", "dance / pop", "dance/electronica", "dance / electronica",
    "edm", "club", "club/dance", "pop", "music", "untagged", "no genre",
})


def is_specific_genre(tag: str | None) -> bool:
    """True if `tag` is a usable, specific genre (not a generic bucket / junk)."""
    if not tag:
        return False
    return tag.strip().lower() not in _GENERIC_GENRE_TAGS


def split_tag_genre(tag: str) -> tuple[str, str]:
    """Split an existing genre tag into (parent_genre, subgenre) using the
    hierarchy. 'Tech House' -> ('House', 'Tech House'); an unknown modern genre
    -> (tag, tag) so we still surface the specific label."""
    t = (tag or "").strip()
    canon = DJ_GENRE_MAP.get(t, t)
    if canon in SUBGENRE_TO_PARENT:
        parent = SUBGENRE_TO_PARENT[canon]
        return DJ_GENRE_MAP.get(parent, parent), canon
    return canon, canon


def _genre_conflicts(tag: str, ml_genre: str, ml_subgenre: str) -> bool:
    """True if the existing tag and the ML read are NOT the same genre family."""
    tag_parent, tag_sub = split_tag_genre(tag)
    if tag_sub in (ml_subgenre, ml_genre) or tag_parent in (ml_genre, ml_subgenre):
        return False
    return True


@dataclass
class ReconciledGenre:
    genre: str
    subgenre: str
    confidence: float
    source: str          # "tag" | "ml" | "ml_override"
    conflict: bool       # tag and ML disagreed (informational / for review)


def reconcile_genre(
    ml_genre: str,
    ml_subgenre: str,
    ml_raw_confidence: float,
    existing_tag: str | None,
    policy: str = "prefer_tag",
    ml_override_confidence: float = 0.90,
    web_genre: str | None = None,
    web_grounded: bool = False,
) -> ReconciledGenre:
    """Reconcile an existing genre tag with the ML read per `policy`.

    Trusts SPECIFIC existing tags by default (they're usually curated/Beatport),
    ignores generic junk tags, and lets a confident, disagreeing ML read
    override. Pure + fully unit-testable; see AnalysisConfig.genre_source_policy.

    `web_genre` (optional) is a genre resolved by the online web-synthesis
    classifier — a stronger signal than the audio model. When present + usable it
    SUPERSEDES the audio read as the "ML-side" source: with a specific tag it only
    overrides when the lookup was `web_grounded` (cited an explicit source) AND
    disagrees on family (catches stale-tag taxonomy drift, zero-regression);
    otherwise the tiered blend is tag › web › audio.
    """
    tag = (existing_tag or "").strip()
    specific = is_specific_genre(tag)

    web = (web_genre or "").strip()
    web_usable = bool(web) and web.lower() not in _GENERIC_GENRE_TAGS and web.lower() != "unknown"
    if web_usable:
        w_parent, w_sub = split_tag_genre(web)
        if policy == "ml_only" or not specific:
            return ReconciledGenre(w_parent, w_sub, 0.9, "web", False)
        tag_parent, tag_sub = split_tag_genre(tag)
        conflict = _genre_conflicts(tag, w_parent, w_sub)
        if policy == "tag_only":
            return ReconciledGenre(tag_parent, tag_sub, 1.0, "tag", conflict)
        if policy == "prefer_ml":
            return ReconciledGenre(w_parent, w_sub, 0.9, "web", conflict)
        # prefer_tag: trust the specific tag; the grounded web read overrides ONLY
        # when it disagrees on family (the tag is probably stale/wrong).
        if web_grounded and conflict:
            return ReconciledGenre(w_parent, w_sub, 0.9, "web_override", True)
        return ReconciledGenre(tag_parent, tag_sub, 0.99, "tag", conflict)

    # No usable tag, or pure-ML mode → ML.
    if policy == "ml_only" or not specific:
        return ReconciledGenre(ml_genre, ml_subgenre, ml_raw_confidence, "ml", False)

    tag_parent, tag_sub = split_tag_genre(tag)
    conflict = _genre_conflicts(tag, ml_genre, ml_subgenre)

    if policy == "tag_only":
        return ReconciledGenre(tag_parent, tag_sub, 1.0, "tag", conflict)

    if policy == "prefer_ml":
        # ML wins unless it's weak, then defer to the specific tag.
        if ml_raw_confidence is not None and ml_raw_confidence < 0.5:
            return ReconciledGenre(tag_parent, tag_sub, 0.99, "tag", conflict)
        return ReconciledGenre(ml_genre, ml_subgenre, ml_raw_confidence, "ml", conflict)

    # "prefer_tag" (default): trust the specific tag; ML overrides ONLY when it
    # both disagrees and is very confident (the tag is probably wrong).
    if (
        conflict
        and ml_raw_confidence is not None
        and ml_raw_confidence >= ml_override_confidence
    ):
        return ReconciledGenre(ml_genre, ml_subgenre, ml_raw_confidence, "ml_override", True)
    return ReconciledGenre(tag_parent, tag_sub, 0.99, "tag", conflict)


__all__ = [
    "GENRE_HIERARCHY",
    "SUBGENRE_TO_PARENT",
    "DJ_GENRE_MAP",
    "GenrePrediction",
    "GenreResult",
    "ReconciledGenre",
    "UNKNOWN",
    "parse_discogs_genre",
    "get_best_genre",
    "is_specific_genre",
    "split_tag_genre",
    "reconcile_genre",
]
