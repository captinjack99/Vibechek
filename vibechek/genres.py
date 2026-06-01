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
    "Dubstep": ["Bassline", "Grime"],
    "Disco": ["Nu-Disco", "Euro-Disco", "Italo-Disco", "Disco Polo"],
    "Ambient": ["Dark Ambient", "Drone", "New Age"],
    "Downtempo": ["Trip Hop", "Chillwave"],
    "Industrial": ["EBM", "Power Electronics", "Rhythmic Noise", "Noise", "Neofolk"],
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


__all__ = [
    "GENRE_HIERARCHY",
    "SUBGENRE_TO_PARENT",
    "DJ_GENRE_MAP",
    "GenrePrediction",
    "GenreResult",
    "UNKNOWN",
    "parse_discogs_genre",
    "get_best_genre",
]
