"""Genre taxonomy and the classification logic that maps Discogs-400 model
predictions into DJ-friendly genre / subgenre labels.

The Discogs-400 model returns labels like 'Electronic---Tech House'. DJs don't
think in terms of Discogs's full taxonomy — they want 'House' (parent) and
'Tech House' (subgenre). The maps below define that translation.

Everything in this module is pure logic — no I/O, no ML. Safe to import without
essentia installed.
"""

from __future__ import annotations

import re
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

# Every genre name the taxonomy recognizes in any position — parent, subgenre, or
# Discogs alias. Used to tell "a genre we don't happen to list" (Reggaeton) apart
# from "not a genre name at all" (see `_is_playlist_phrase`).
_KNOWN_GENRE_NAMES: frozenset[str] = frozenset(
    set(GENRE_HIERARCHY) | set(SUBGENRE_TO_PARENT) | set(DJ_GENRE_MAP)
    | set(DJ_GENRE_MAP.values())
)

# ---------------------------------------------------------------------------
# Tag spelling aliases
# ---------------------------------------------------------------------------

# The measured lesson of d7921d7 is that a tag the hierarchy cannot place is
# usually a HIERARCHY GAP, not junk — distrusting all unplaceable tags cost two
# corpus tracks and fixed none. These are the other half of that finding: tags
# naming a genre the hierarchy ALREADY KNOWS, under a spelling it does not list.
# `split_tag_genre` returns (tag, tag) for those, so the raw string becomes the
# track's genre and — via organizer's `sanitize_folder_name(ml_genre)` — its own
# destination folder. "Hip-Hop" and "Hip Hop" are then two folders for one genre,
# and neither can be matched, filtered or grouped against the other.
#
# Every entry must map onto a name the hierarchy already carries; an alias that
# misses leaves the tag just as unplaceable while LOOKING handled, so
# tests/test_genres.py asserts that over the whole table.
#
# The risk an alias carries is not accuracy in the abstract — it is landing in
# the WRONG FAMILY, which moves organizer's destination folder and flips
# reconcile's conflict flag. So each was checked against the family its files
# independently resolve to (internal/bughunt/score_genre_aliases.py route 2: the
# tag's tracks joined to the 2,577 web-resolved labels over D:\Music\Tracks; HIT
# = share whose resolved family equals the alias target's, against that library's
# own base rate for it). `n` below is files carrying that exact tag of 12,092.
#
# The 86-track adjudicated corpus cannot judge these — it carries exactly one of
# these tags — so it was run as a REGRESSION GATE instead, and holds to the digit:
# 53.5% exact / 72.1% family web-off and 73.3% / 86.0% web-on, in strict AND
# accept, 0 broken, 0 conflict-flag flips.
_GENRE_TAG_ALIASES: dict[str, str] = {
    # -- spelling / punctuation only; no family risk to measure ----------------
    "Hip-Hop": "Hip Hop",                    # n=105
    "D&B": "Drum & Bass",                    # n=16
    "Rock & Roll": "Rock",                   # n=14
    "Synth Pop": "Synth-pop",                # n=5    HIT 67% (2/3) vs 1% base
    # -- Beatport's own slash-joined genre names ------------------------------
    # Single genre names on Beatport, not lists, so they are aliased here rather
    # than left to the multi-value tag parser.
    "Nu Disco / Disco": "Nu-Disco",          # n=70   HIT 84% (54/64) vs 10%, 8.6x
    "Minimal / Deep Tech": "Minimal Techno",  # n=29  HIT 85% (22/26) vs 8%, 10.4x
    #   — and that measurement is why the target is Minimal TECHNO and not plain
    #   "Minimal": the hierarchy files "Minimal" under House, which would have put
    #   6 of every 7 of these tracks in the wrong family and the wrong folder.
    "Organic House / Downtempo": "Organic House",
    "Indie Dance / Nu Disco": "Indie Dance",
    "UK Garage / Bassline": "Bassline",       # n=4 — and the adjudicated corpus's
    "Bassline / Speed Garage": "Bassline",    #   truth label for that same track
    "Trap / Future Bass": "Trap",             # n=1    HIT 100% (1/1)
}
# REFUTED by the same measurement, and the reason it exists: "Breaks / Breakbeat
# / UK Bass" (5 files) looks like the identical case — a slash-joined Beatport
# name whose first component the hierarchy carries — but its 4 independently
# resolved tracks come back house 3 / Future Garage 1 and breaks ZERO. An alias
# there would have moved 5 files into a Breaks folder on nothing but the name.
#
# Also deliberately NOT guessed at: Beatport's storefront returns 403 to a fetch,
# so only names attested in a real library, in the adjudicated corpus, or in a
# retrieved Beatport genre listing are here. A Beatport name we have never seen
# ("Hard Dance / Hardcore / Neo Rave") stays as unplaceable as it is today — a
# gap, not a regression.

# Beatport also qualifies a plain genre name in brackets — "Techno (Peak Time /
# Driving)", "Trance (Raw / Deep / Hypnotic)", "Electro (Classic / Detroit /
# Modern)". The qualifier narrows a genre we already carry, so strip it instead of
# enumerating a list that changes whenever Beatport reorganizes. The rule is
# self-limiting: it applies ONLY when what remains is a name the hierarchy knows,
# so a bracketed suffix on anything else is left alone.
_PAREN_QUALIFIER = re.compile(r"\s*\([^()]*\)\s*$")


def _alias_key(tag: str) -> str:
    """Alias lookups ignore case and internal spacing; the hierarchy proper does not."""
    return " ".join(tag.split()).casefold()


_GENRE_TAG_ALIAS_LOOKUP: dict[str, str] = {
    _alias_key(k): v for k, v in _GENRE_TAG_ALIASES.items()
}


def _resolve_tag_alias(tag: str) -> str:
    """Rewrite a known spelling variant to the hierarchy's own name for it."""
    if not tag:
        return tag
    direct = _GENRE_TAG_ALIAS_LOOKUP.get(_alias_key(tag))
    if direct:
        return direct
    base = _PAREN_QUALIFIER.sub("", tag).strip()
    if base and base != tag:
        base = _GENRE_TAG_ALIAS_LOOKUP.get(_alias_key(base), base)
        if base in _KNOWN_GENRE_NAMES:
            return base
    return tag


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

# CONTENT-FREE labels: retailer sales buckets and catch-alls that name no genre at
# all. Nothing may carry one of these as an answer — not a file tag, and not a
# catalog read either, which is why this set also gates the online lookup below.
_GENERIC_GENRE_TAGS: frozenset[str] = frozenset({
    "", "unknown", "other", "others", "misc", "miscellaneous", "various",
    "electronic", "electronica", "dance", "dance/electronic", "dance / electronic",
    "dance/pop", "dance / pop", "dance/electronica", "dance / electronica",
    "electronica/dance", "electronica / dance",
    "edm", "club", "club/dance", "pop", "music", "untagged", "no genre",
    # Same buckets in another language. Listed rather than aliased because the
    # translation lands on a name that is itself generic — "Électronique" says
    # exactly as little as "Electronic" (15 files, D:\Music\Tracks).
    "électronique", "electronique",
})

# UNTRUSTED AS A FILE TAG: labels that DO name a real genre, so a verified catalog
# read of one stands, but that taggers spray so indiscriminately over a whole
# release/pool that the tag carries no information about the track.
#
# "Electro" is the measured case (internal/bughunt/score_generic_set.py, 86-track
# adjudicated corpus, through this function):
#   keep trusting it .... 52.3% exact / 70.9% family   (web off)   72.1% / 84.9% (web on)
#   distrust it ......... 53.5% exact / 72.1% family   (web off)   73.3% / 86.0% (web on)
# +1 exact, +1 family, ZERO broken, stable across strict/accept scoring x 2 passes.
# None of the 4 Electro-tagged corpus tracks is actually Electro (two Electronica,
# one Bass House, one House). At library scale (12,145 files, D:\Music\Tracks) the
# tag is on 299 files (2.5%) and only 14 of those 299 sit in an Electro folder —
# 285 are filed under Techno / Progressive House / Tech Trance / Bassline, so the
# tag disagrees with the curation 95% of the time.
#
# "House" was the other candidate (the measurement harness distrusts it) and is
# REFUTED: -1 exact / -1 family, breaks 1 and fixes 0, in every cell. It is the
# library's largest tag (2,645 files, 21.9%) and 2,357 of those sit in the House
# folder — the tag and the curation agree. build_reference.py found the same thing
# from the other side: applying the harness's tag-distrust set to LABEL validity
# deleted all 407 "House" reference vectors and cost 4.1 exact points.
_UNTRUSTED_GENRE_TAGS: frozenset[str] = _GENERIC_GENRE_TAGS | frozenset({
    "electro",
})

# A tag can also fail STRUCTURALLY, with no denylist entry: a long phrase naming
# no genre we know is a playlist or record-pool name that landed in the genre
# field ("Hypeddit Top Weekly Picks"). Left trusted, it becomes the track's genre
# AND — via organizer's `sanitize_folder_name(ml_genre)` — a destination folder
# named after a playlist.
#
# The rule is deliberately narrow: 4+ words, NO list separator, and unplaceable in
# the hierarchy. Wider structural rules were measured and REFUTED on the 86-track
# adjudicated corpus (internal/bughunt/score_unplaceable_rule.py, through
# reconcile_genre, strict|accept x web off|on):
#   distrust ALL unplaceable ....... 53.5->52.3% exact, 72.1->70.9% family, BROKE 2
#       "Stutter House" (truth House, exact via the tag) -> audio said Bassline;
#       "Dubstep, House, Electronic, Trance" (truth Melodic Dubstep, family-right
#       because the canon picked the Dubstep member) -> audio said Hands Up.
#   distrust unplaceable LISTS ..... breaks that same track, fixes none. A list of
#       genres still names genres; parsing it beats discarding it.
#   distrust 4+ WORD phrases ....... exact AND family UNCHANGED in all four cells,
#       0 broken. The one corpus track it touches was a miss either way.
# So the corpus licenses this rule as harmless, not as an accuracy win; the reason
# to ship it is correctness — a playlist name is not a genre, the same argument
# that put "Dance / Pop" in the generic set. Library check (12,145 files, run
# through THIS predicate): it fires on 187 files across 3 tags — "Hypeddit Top
# Weekly Picks" (185), "Electronic Pop Pop Rock Soft Rock Synth-Pop", "Dance Deep
# House House Edm" — and on nothing else, so zero false positives at that scale.
# No real genre name is caught: the long ones ("Melodic House & Techno", "Techno
# (Peak Time / Driving)", "Bassline / Speed Garage") are either placeable or carry
# a separator, and single-token pool labels ("TMU", "Urban") are NOT caught — no
# structural rule separates those from "Donk", so they need their own evidence.
_GENRE_LIST_SEP = re.compile(r"[,;/]|\s+[&x]\s+", re.IGNORECASE)
_MIN_JUNK_PHRASE_WORDS = 4


def _placeable_in_hierarchy(tag: str) -> bool:
    """True if the hierarchy recognizes `tag` — as a subgenre, a parent, or an alias."""
    parent, sub = split_tag_genre(tag)
    if parent != sub:
        return True          # split found a parent, so the subgenre is known
    return parent in _KNOWN_GENRE_NAMES


def _is_playlist_phrase(tag: str) -> bool:
    """A multi-word phrase that names no genre we know — a playlist / pool label."""
    t = tag.strip()
    return (
        len(t.split()) >= _MIN_JUNK_PHRASE_WORDS
        and not _GENRE_LIST_SEP.search(t)
        and not _placeable_in_hierarchy(t)
    )


def is_specific_genre(tag: str | None) -> bool:
    """True if `tag` is a usable, specific genre (not a generic bucket / junk).

    This is the FILE-TAG question, so it uses the wider untrusted set — a label
    like "Electro" is a real genre but not a trustworthy tag. Use
    `is_usable_genre_label` for a genre coming off a catalog page or the model.
    """
    if not tag:
        return False
    if tag.strip().lower() in _UNTRUSTED_GENRE_TAGS:
        return False
    return not _is_playlist_phrase(tag)


def is_usable_genre_label(genre: str | None) -> bool:
    """True if `genre` names an actual genre — the test for a read we RESOLVED
    (online catalog field, audio model) rather than a tag we inherited.

    Deliberately narrower than `is_specific_genre`: a tag we distrust because
    taggers spray it is still a perfectly good answer when a verified catalog
    field says it.
    """
    if not genre:
        return False
    return genre.strip().lower() not in _GENERIC_GENRE_TAGS


def split_tag_genre(tag: str) -> tuple[str, str]:
    """Split an existing genre tag into (parent_genre, subgenre) using the
    hierarchy. 'Tech House' -> ('House', 'Tech House'); an unknown modern genre
    -> (tag, tag) so we still surface the specific label.

    A known spelling variant ('Hip-Hop', 'Techno (Peak Time / Driving)') is
    rewritten to the hierarchy's own name for it first — see `_GENRE_TAG_ALIASES`.
    """
    t = _resolve_tag_alias((tag or "").strip())
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

    `web_genre` (optional) is a genre resolved by the online genre lookup — a
    stronger signal than the audio model. When present + usable it SUPERSEDES the
    audio read as the "ML-side" source: with a specific tag it overrides only
    when the lookup was `web_grounded`; otherwise the tiered blend is
    tag › web › audio.

    `web_grounded` means the lookup VERIFIED the read itself — the genre came off
    a catalog page's structured field, quoted verbatim from the page bytes, on a
    page naming this exact artist and title (see genre_web.resolve). That is why
    such a read is allowed to replace a specific tag even when the two sit in the
    SAME family: the guard used to also require a family-level disagreement,
    because "grounded" once meant only that a language model claimed it had a
    source. Against the deterministic read that guard is pure loss — measured on
    the 86-track adjudicated corpus it suppressed 6 overrides, every one of which
    turned a merely family-correct answer into an exact one, and broke nothing
    (64.0% → 70.9% exact, family unchanged at 82.6%). An override always sets
    `conflict`, so a replaced tag still reaches the review queue rather than
    changing silently.
    """
    tag = (existing_tag or "").strip()
    specific = is_specific_genre(tag)

    web = (web_genre or "").strip()
    # A RESOLVED read, not an inherited tag: judged by content-freeness only, so a
    # verified catalog "Electro" stands even though the file tag isn't trusted.
    web_usable = is_usable_genre_label(web)
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
        # prefer_tag: trust the specific tag, but a VERIFIED catalog read replaces
        # it whenever the two actually differ — including a same-family
        # refinement like Tech House → Funky House (see the docstring for the
        # measurement). An identical read changes nothing and stays "tag".
        if web_grounded and (w_parent, w_sub) != (tag_parent, tag_sub):
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
    "is_usable_genre_label",
    "split_tag_genre",
    "reconcile_genre",
]
