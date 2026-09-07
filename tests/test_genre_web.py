"""Tests for the online genre lookup (vibechek/genre_web.py).

The tier is deterministic: search → fetch catalog pages → regex the structured
genre field → identity-gate it against the track → refuse retailer sales
buckets. Nothing here touches the network: `_ddgs_results` (search) and
`_fetch_page` (HTTP) are monkeypatched, and the canned pages go through the REAL
HTML → text extraction and the REAL parser, so the fixtures exercise everything
that decides an answer.

Contract under test: `resolve()` NEVER raises, degrades to an empty read on any
failure, and only sets `source_matched` on evidence it verified itself.
"""

from __future__ import annotations

import sys

import pytest

from vibechek import genre_web

# ---------------------------------------------------------------------------
# Canned catalog pages. Minimal HTML in the field format the real sites emit —
# the Beatport detail block is copied from a live page's extracted text
# ("Label : Siona Records Genre : Melodic House & Techno BPM: 126 Key : ...").
# ---------------------------------------------------------------------------


def _beatport(artist: str, title: str, genre: str, label: str = "Siona Records") -> str:
    return f"""<html><head><title>{title} by {artist} on Beatport</title>
<meta name="description" content="{title} by {artist}"></head>
<body><nav>Home Genres Charts</nav>
<main><h1>Track {title}</h1>
<p>Artists : {artist}</p>
<div class="meta">Label : {label} Genre : {genre} BPM: 126 Key : A Major
Length : 7:35 Released : 2024-06-14</div>
</main><footer>Appears On Pump Chart</footer></body></html>"""


def _discogs(artist: str, title: str, genre: str, style: str) -> str:
    return f"""<html><head><title>{artist} - {title} | Discogs</title></head>
<body><h1>{artist} &ndash; {title}</h1>
<table><tr><td>Genre: {genre}</td></tr><tr><td>Style: {style}</td></tr>
<tr><td>Released: 2024</td></tr></table></body></html>"""


def _juno(artist: str, title: str, genre: str) -> str:
    return f"""<html><head><title>{artist} - {title}</title></head>
<body><h1>{title}</h1><p>{artist}</p>
<div>Genre: {genre} Released: 2024-06-14</div></body></html>"""


def _fake_search(urls: list[str]):
    """A `_ddgs_results` stand-in returning those URLs as search hits."""
    def _search(query: str, n: int = 6) -> list[dict[str, str]]:
        return [{"title": "", "body": "", "url": u} for u in urls]
    return _search


def _fake_fetcher(pages: dict[str, str], *, fail: set[str] | None = None):
    """A `_fetch_page` stand-in serving canned HTML through the REAL extractor."""
    fail = fail or set()

    def _fetch(url: str) -> dict[str, object]:
        tier = genre_web._domain_tier(url)
        html = pages.get(url, "")
        if url in fail or not html or tier == "X":
            return {"url": url, "host": genre_web._host_of(url), "tier": tier,
                    "final_url": url, "status": 403, "err": "HTTP 403", "text": ""}
        return {"url": url, "host": genre_web._host_of(url), "tier": tier,
                "final_url": url, "status": 200, "err": "",
                "text": genre_web._extract_text(html)}
    return _fetch


@pytest.fixture(autouse=True)
def _no_politeness_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip the between-search politeness sleep so the suite stays fast."""
    monkeypatch.setattr(genre_web, "SEARCH_SLEEP", 0.0)


@pytest.fixture(autouse=True)
def _no_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    """The retired model path must never be reached from resolve()."""
    def boom(*a: object, **k: object) -> object:
        raise AssertionError("resolve() called the retired LLM path")

    monkeypatch.setattr(genre_web, "_llm_chat", boom)
    monkeypatch.setattr(genre_web, "ensure_backend", boom)


# ---------------------------------------------------------------------------
# module hygiene + canon
# ---------------------------------------------------------------------------


def test_module_is_import_clean() -> None:
    """Search + HTML parsing are lazy: importing vibechek must not drag them in."""
    for heavy in ("ddgs", "duckduckgo_search"):
        assert heavy not in sys.modules


def test_canon_maps_chart_buckets_and_vocab() -> None:
    assert genre_web._canon("Tech House") == "Tech House"
    assert genre_web._canon("") == ""
    # A multi-word phrase whose extra word is not a qualifier is NOT rounded to
    # the vocab word inside it — "Dance/Pop" is a retailer sales bucket, not a
    # pop record. `_parse_field` splits on "/" before it canons anything, so
    # each real segment ("Dance", "Pop") still meets the bucket refusal it
    # always did; nothing about a live page's handling changes here.
    assert genre_web._canon("Dance/Pop") not in genre_web.VOCAB


@pytest.mark.parametrize(("raw", "expected"), [
    ("Melodic House & Techno", "Melodic House & Techno"),
    ("Trance (Main Floor)", "Trance"),          # longest contained vocab entry
    ("Progressive Trance", "Trance"),           # not in vocab -> nearest parent
    # Separator-insensitive on BOTH sides: the catalog field says "Nu Disco",
    # the vocab entry is "Nu-Disco". Without normalizing the vocab side this
    # fell through to containment and lost specificity to plain "Disco".
    ("Nu Disco", "Nu-Disco"),
    ("house", "House"),                         # generic stays generic
    ("disco", "Disco"),                         # never invents "Nu-Disco"
])
def test_canon_segment_mapping(raw: str, expected: str) -> None:
    assert genre_web._canon(raw) == expected


def test_canon_folds_the_ampersand_the_way_norm_does() -> None:
    """`_norm` folds "&" to " and "; the canon's own normalizer did not, so the
    two vocab entries carrying an ampersand never matched a page that spells the
    word out. "Drum and Bass" was dropped outright, and "Melodic House and
    Techno" fell through to containment and came back as the wrong family."""
    assert genre_web._canon("Melodic House and Techno") == "Melodic House & Techno"
    assert genre_web._canon("Drum and Bass") == "Drum & Bass"


@pytest.mark.parametrize("fused", ["Britpop", "Electronic Style", "Electropop"])
def test_canon_refuses_a_vocab_word_fused_inside_another(fused: str) -> None:
    """Containment is whole-word: a subgenre must be NAMED, not found inside a
    longer word ("electro" in "electronic", "pop" in "britpop")."""
    assert genre_web._canon(fused) not in genre_web.VOCAB


@pytest.mark.parametrize("phrase", [
    "Hardcore Punk",    # a punk record, not gabber
    "Pop Rock",
    "Synth-pop",
    "Electro Pop",
    "Disco Polo",
    "Post-Disco",
    "Rock. Buy the reissue of this hardcore classic.",   # a prose span, not a field
])
def test_canon_refuses_a_phrase_that_names_a_genre_we_do_not_know(phrase: str) -> None:
    """The containment rung had no "outside our taxonomy → refuse" branch, so any
    phrase merely CONTAINING a vocab word was rounded to it — a Black Flag record
    labelled "Hardcore Punk" came back as the EDM genre "Hardcore". A hit that
    leaves un-consumed, non-qualifier words behind is not a read."""
    assert genre_web._canon(phrase) not in genre_web.VOCAB


@pytest.mark.parametrize(("phrase", "expected"), [
    ("Progressive Trance", "Trance"),        # the leftover only NARROWS the family
    ("Deep Techno", "Techno"),
    ("Trance (Main Floor)", "Trance"),       # a bracketed aside qualifies, never names
    ("Tech House music", "Tech House"),
])
def test_canon_still_reads_a_qualified_family(phrase: str, expected: str) -> None:
    """The refusal must not cost the legitimate narrowing reads: a page that says
    "Progressive Trance" is still evidence for Trance."""
    assert genre_web._canon(phrase) == expected


@pytest.mark.parametrize("text", [
    "Genre: Rock. Buy the reissue of this hardcore classic. Released: 1981",
    "Genre: Rock (feat. hardcore punk influences) Released: 1981",
    "Genre: Blues + Hardcore reissue notes Released: 1981",
    "Genre: Rock is a hardcore classic Released: 1981",
])
def test_parse_field_refuses_a_prose_span_between_the_label_and_a_stop_word(
    text: str,
) -> None:
    """`_extract_text` collapses the page to ONE line, so ordinary prose sits
    between "Genre:" and the next labelled cell. Admitting sentence punctuation
    let the whole span be captured, and it donated whichever vocab word it
    happened to mention ("Hardcore")."""
    assert genre_web._parse_field(text, "B") is None
    assert genre_web._parse_field(text, "A") is None


# ---------------------------------------------------------------------------
# extraction + field parsing
# ---------------------------------------------------------------------------


def test_extract_text_surfaces_the_labelled_genre_field() -> None:
    text = genre_web._extract_text(_beatport("GENESI (ITA)", "Slow Down",
                                             "Melodic House & Techno"))
    assert "Genre : Melodic House & Techno BPM: 126" in text
    assert "<div" not in text  # markup gone


def test_parse_field_reads_a_tier_a_block() -> None:
    text = genre_web._extract_text(_beatport("Mau P", "Like I Like It", "Tech House"))
    got = genre_web._parse_field(text, "A")
    assert got is not None
    genre, raw, quote = got
    assert genre == "Tech House"
    assert raw == "Tech House"
    assert quote in text                      # the span really is on the page


def test_parse_field_refuses_chart_buckets() -> None:
    """A retailer sales bucket is not a musical genre — no read at all."""
    for bucket in ("Dance / Pop", "Electronic", "EDM"):
        text = genre_web._extract_text(_beatport("Some", "Track", bucket))
        assert genre_web._parse_field(text, "A") is None


def test_parse_field_bounded_capture_survives_the_discogs_style_field() -> None:
    """REGRESSION: Discogs reads "Genre: Electronic Style: House, Tech House".

    A greedy capture swallowed "Electronic Style" — which is not a listed bucket,
    so the bucket refusal missed it, and the canon then turned the substring
    "electro" into the real subgenre "Electro". That artifact was the source of
    every bogus "Discogs says Electro" record. The stop-word lookahead bounds the
    capture at "Style", so the field reads as the bare bucket "Electronic" and is
    refused outright — a top-level retailer bucket must never become a subgenre.
    """
    # The hazard, pinned. This is what the greedy capture used to feed the canon;
    # the canon's containment rung is now whole-word, so "electro" can no longer
    # be lifted out of the middle of "electronic" even if a capture does slip.
    assert genre_web._canon("Electronic Style") not in genre_web.VOCAB

    text = genre_web._extract_text(
        _discogs("Mau P", "Like I Like It", "Electronic", "House, Tech House"))
    m = genre_web.FIELD_GEN.search(text)
    assert m is not None and m.group(1).strip() == "Electronic"   # bounded, not greedy
    assert genre_web._parse_field(text, "B") is None              # and then refused


def test_parse_field_refuses_a_bucket_only_discogs_page() -> None:
    """Same page shape with nothing but the bucket: refuse, do not downgrade to
    "Electro" (that was the source of every bogus "Discogs says Electro")."""
    text = genre_web._extract_text(_discogs("Some", "Track", "Electronic", "Electronic"))
    assert genre_web._parse_field(text, "B") is None


def test_parse_field_never_swallows_a_neighbouring_label() -> None:
    """REGRESSION: `_extract_text` collapses the page to ONE line, so FIELD_A's
    dot-any capture spanned what were separate table cells. On a Discogs-style
    "Genre: Rock Style: Hardcore Punk Released: 1981" it captured
    "Rock Style: Hardcore Punk", whose "Hardcore Punk" segment canons to the EDM
    genre "Hardcore" — a punk record silently filed as gabber, with conflict
    False so nothing reached the review queue."""
    text = genre_web._extract_text(
        _discogs("Black Flag", "Rise Above", "Rock", "Hardcore Punk"))
    assert "Genre: Rock Style: Hardcore Punk" in text     # the hazardous adjacency
    m = genre_web.FIELD_A.search(text)
    assert m is None or ":" not in m.group(1)            # capture stays in its cell
    assert genre_web._parse_field(text, "B") is None     # and no genre is invented


def test_parse_field_reads_a_spelled_out_ampersand_genre() -> None:
    """A tier-A page that writes "Melodic House and Techno" must land on the
    vocab entry, not on the containment rung's "Techno"."""
    text = genre_web._extract_text(
        _beatport("Yotto", "Aviation", "Melodic House and Techno"))
    got = genre_web._parse_field(text, "A")
    assert got is not None and got[0] == "Melodic House & Techno"


def test_parse_field_canons_each_segment() -> None:
    text = genre_web._extract_text(
        _beatport("A", "B", "Trance (Main Floor) | Progressive Trance"))
    got = genre_web._parse_field(text, "A")
    assert got is not None and got[0] == "Trance"


@pytest.mark.parametrize(("field", "expected"), [
    ("Techno (Peak Time / Driving)", "Techno"),
    ("Techno (Raw / Deep / Hypnotic)", "Techno"),
    ("House (Classic / Vocal)", "House"),
])
def test_parse_field_reads_a_genre_whose_aside_contains_a_slash(
    field: str, expected: str,
) -> None:
    """Beatport's real Techno taxonomy puts a '/' INSIDE the aside. Splitting the
    field on every '/' tore the bracket pair in half, so `_canon` no longer saw
    the aside as an aside and the orphaned "(peak time" left non-qualifier words
    behind — the highest-trust tier refused its own commonest field. Only a
    separator at bracket depth 0 divides the field."""
    text = genre_web._extract_text(_beatport("Charlotte de Witte", "Sgadi Li Mi", field))
    got = genre_web._parse_field(text, "A")
    assert got is not None and got[0] == expected


def test_split_segments_still_divides_a_real_multi_genre_field() -> None:
    """Protecting the asides must not cost the split its day job: a field really
    does list several genres, and each one still meets the bucket refusal."""
    assert genre_web._split_segments("House, Tech House") == ["House", "Tech House"]
    assert genre_web._split_segments("Dance / Pop") == ["Dance", "Pop"]
    assert genre_web._split_segments("Trance (Main Floor) | Progressive Trance") == [
        "Trance (Main Floor)", "Progressive Trance"]
    assert genre_web._split_segments("House - Tech House") == ["House", "Tech House"]


def test_canon_reads_a_value_that_is_entirely_a_bracketed_aside() -> None:
    """Dropping brackets wholesale emptied a value with nothing outside them, and
    the canon then handed back the raw "(Deep House)". When the aside is ALL
    there is, the brackets are decoration and the value inside is the field."""
    assert genre_web._canon("(Deep House)") == "Deep House"
    assert genre_web._canon("[Techno]") == "Techno"


def test_quote_verification_rejects_a_span_from_another_page() -> None:
    """The quote is held to substring discipline against the page we fetched —
    a genre field lifted off some OTHER page is not evidence about this one."""
    page = genre_web._extract_text(_beatport("Mau P", "Like I Like It", "Tech House"))
    assert genre_web._quote_verified("Genre : Tech House BPM: 126", page)
    assert not genre_web._quote_verified("Genre : Hardstyle BPM: 150", page)
    assert not genre_web._quote_verified("", page)


# ---------------------------------------------------------------------------
# the identity gate
# ---------------------------------------------------------------------------


def test_identity_gate_strips_feat_credits_and_mix_qualifiers() -> None:
    page = genre_web._norm(genre_web._extract_text(
        _beatport("Jerro, Forester", "Breathless", "Progressive House")))
    assert genre_web._identity_ok(page, "Jerro feat. Forester", "Breathless (Extended Mix)")


def test_identity_gate_grades_title_only_as_weaker() -> None:
    page = genre_web._norm(genre_web._extract_text(
        _beatport("Someone Else", "Breathless", "Progressive House")))
    assert genre_web._identity_grade(page, "Jerro", "Breathless") == "title-only"
    assert not genre_web._identity_ok(page, "Jerro", "Breathless")


def test_identity_gate_refuses_a_short_artist_it_cannot_verify() -> None:
    """REGRESSION: `_artist_names` drops every component under 3 normalized
    characters, and an EMPTY list used to grade as 'full' — the exact opposite of
    "cannot verify". For a real release like "MK - 17" the gate collapsed to
    "does '17' appear on this page", which is true of nearly any catalog page
    (chart position, year, price), so a page about a different act licensed a
    web_override of the user's tag."""
    page = genre_web._norm(genre_web._extract_text(
        _beatport("Charlotte de Witte", "Sgadi Li Mi", "Techno",
                  label="Top 100 chart position 17")))
    assert "17" in page                       # the coincidental title match
    assert genre_web._identity_grade(page, "MK", "17") == "title-only"
    assert not genre_web._identity_ok(page, "MK", "17")


def test_identity_gate_still_verifies_a_genuinely_short_artist() -> None:
    """The short name is CHECKED, not skipped: on MK's own page the gate passes."""
    page = genre_web._norm(genre_web._extract_text(_beatport("MK", "17", "Deep House")))
    assert genre_web._identity_ok(page, "MK", "17")


def test_identity_gate_refuses_a_title_that_reduces_to_nothing() -> None:
    """Same falsy-empty shape on the title side: `_title_core` strips mix
    qualifiers, and when that left nothing the title check was skipped entirely
    instead of falling back to the raw title."""
    page = genre_web._norm(genre_web._extract_text(
        _beatport("Charlotte de Witte", "Sgadi Li Mi", "Techno")))
    assert genre_web._title_core("(Original Mix)") == ""
    assert genre_web._identity_grade(page, "MK", "(Original Mix)") != "full"


# ---------------------------------------------------------------------------
# resolve(): the shipped ladder
# ---------------------------------------------------------------------------

_BP = "https://www.beatport.com/track/like-i-like-it/1"
_DC = "https://www.discogs.com/release/1-Mau-P-Like-I-Like-It"
_JU = "https://www.junodownload.com/products/like-i-like-it/1"
_TX = "https://www.traxsource.com/title/1/like-i-like-it"
_YT = "https://www.youtube.com/watch?v=1"


def test_resolve_tier_a_field_is_verified_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(genre_web, "_ddgs_results", _fake_search([_YT, _BP]))
    monkeypatch.setattr(genre_web, "_fetch_page", _fake_fetcher(
        {_BP: _beatport("Mau P", "Like I Like It", "Tech House"),
         _YT: "<html><body>Mau P - Like I Like It (Official Video)</body></html>"}))
    r = genre_web.resolve("Mau P", "Like I Like It", "House", "Deep House")
    assert r["genre"] == "Tech House"
    assert r["source_matched"] is True
    assert r["used_web"] is True
    assert r["url"] == _BP
    assert "Tech House" in r["quote"]


def test_resolve_reads_a_beatport_genre_whose_aside_contains_a_slash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end guard for the same defect: "Techno (Peak Time / Driving)" is one
    of Beatport's commonest fields, and the tier returned NO read at all for it —
    coverage loss on the highest-trust tier for a whole genre family."""
    monkeypatch.setattr(genre_web, "_ddgs_results", _fake_search([_BP]))
    monkeypatch.setattr(genre_web, "_fetch_page", _fake_fetcher(
        {_BP: _beatport("Charlotte de Witte", "Sgadi Li Mi",
                        "Techno (Peak Time / Driving)")}))
    r = genre_web.resolve("Charlotte de Witte", "Sgadi Li Mi", "Electronic", "")
    assert r["genre"] == "Techno"
    assert r["source_matched"] is True
    assert r["tier"] == "A"
    assert "Peak Time / Driving" in r["quote"]


def test_resolve_rejects_a_page_about_a_different_track(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The identity gate is the whole defence against wrong-row misattribution:
    a Beatport page for someone else's song states a perfectly real genre."""
    monkeypatch.setattr(genre_web, "_ddgs_results", _fake_search([_BP]))
    monkeypatch.setattr(genre_web, "_fetch_page", _fake_fetcher(
        {_BP: _beatport("Another Artist", "A Different Song", "Hardstyle")}))
    r = genre_web.resolve("Mau P", "Like I Like It")
    assert r["genre"] == ""
    assert r["source_matched"] is False
    assert r["used_web"] is True          # we searched; we just found no evidence


def test_resolve_refuses_a_chart_bucket_as_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    """Beatport files a slice of releases under "Dance / Pop". That is a sales
    category, so it never becomes a genre read — the track falls back instead."""
    monkeypatch.setattr(genre_web, "_ddgs_results", _fake_search([_BP]))
    monkeypatch.setattr(genre_web, "_fetch_page", _fake_fetcher(
        {_BP: _beatport("Mau P", "Like I Like It", "Dance / Pop")}))
    r = genre_web.resolve("Mau P", "Like I Like It")
    assert r["genre"] == ""


def test_resolve_tier_b_alone_fills_but_never_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One tier-B catalog is a fill-only signal: `source_matched` stays False so
    reconcile can use it for a generic/missing tag but not to override a
    specific one."""
    monkeypatch.setattr(genre_web, "_ddgs_results", _fake_search([_JU]))
    monkeypatch.setattr(genre_web, "_fetch_page", _fake_fetcher(
        {_JU: _juno("Mau P", "Like I Like It", "Tech House")}))
    r = genre_web.resolve("Mau P", "Like I Like It")
    assert r["genre"] == "Tech House"
    assert r["source_matched"] is False
    assert r["tier"] == "B"


def test_resolve_refuses_a_compound_genre_from_outside_the_taxonomy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A catalog whose own Genre field reads "Hardcore Punk" is describing punk.

    Two agreeing tier-B hosts is the STRONGEST tier-B outcome (source_matched
    True, confidence 0.90 — web-grounded enough to override a specific tag), so
    this is where the containment rung's missing refusal did the most damage: a
    punk record was filed as the EDM genre "Hardcore" with nothing flagged for
    review. The phrase names a genre outside our taxonomy, so there is no read.
    """
    monkeypatch.setattr(genre_web, "_ddgs_results", _fake_search([_JU, _DC]))
    monkeypatch.setattr(genre_web, "_fetch_page", _fake_fetcher(
        {_JU: _juno("Black Flag", "Rise Above", "Hardcore Punk"),
         _DC: _discogs("Black Flag", "Rise Above", "Hardcore Punk", "Hardcore Punk")}))
    r = genre_web.resolve("Black Flag", "Rise Above", "Punk", "")
    assert r["genre"] == ""
    assert r["source_matched"] is False
    assert r["used_web"] is True          # we searched; the pages just aren't evidence


def test_resolve_two_agreeing_domains_are_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(genre_web, "_ddgs_results", _fake_search([_JU, _BP]))
    monkeypatch.setattr(genre_web, "_fetch_page", _fake_fetcher(
        {_JU: _juno("Mau P", "Like I Like It", "Tech House"),
         # the tier-A page is unparseable (no BPM stop word), so tier-A
         # consensus is empty and the two-domain rung has to carry it
         _BP: "<html><body>Mau P Like I Like It Genres - Tech House</body></html>"},
    ))
    r = genre_web.resolve("Mau P", "Like I Like It")
    assert r["genre"] == "Tech House"
    assert r["source_matched"] is True


def test_resolve_cites_an_evidence_tier_page_for_the_two_domain_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """REGRESSION: `_two_domain` restricts the DECISION to tier A/B, but the
    citation was picked from the unfiltered hit list. `hits` accumulates across
    both query phrasings, so a tier-C blog from query 1 sat ahead of the catalog
    pages from query 2 and came back as the evidence for a source_matched
    answer — the module's documented {url, quote, tier} contract, inverted."""
    blog = "https://randomblog.example/mau-p"
    seen: list[str] = []

    def _search(query: str, n: int = 6) -> list[dict[str, str]]:
        seen.append(query)
        urls = [blog] if len(seen) == 1 else [_DC, _JU]
        return [{"title": "", "body": "", "url": u} for u in urls]

    monkeypatch.setattr(genre_web, "_ddgs_results", _search)
    monkeypatch.setattr(genre_web, "_fetch_page", _fake_fetcher({
        blog: _juno("Mau P", "Like I Like It", "Tech House"),
        _DC: _discogs("Mau P", "Like I Like It", "Tech House", "Tech House"),
        _JU: _juno("Mau P", "Like I Like It", "Tech House"),
    }))
    r = genre_web.resolve("Mau P", "Like I Like It")
    assert r["genre"] == "Tech House" and r["source_matched"] is True
    assert r["tier"] in ("A", "B")
    assert r["url"] != blog


def test_resolve_reports_the_web_as_unavailable_when_every_search_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Offline / rate-limited is NOT the same as "searched and found nothing".
    The tier degraded to tags+audio with no signal of any kind, while the
    progress line kept claiming the online lookup was running."""
    def boom(query: str, n: int = 6) -> list[dict[str, str]]:
        raise RuntimeError("Ratelimit: 202 https://duckduckgo.com/")

    monkeypatch.setattr(genre_web, "_ddgs_results", boom)
    r = genre_web.resolve("Mau P", "Like I Like It")
    assert r["used_web"] is False
    assert "Ratelimit" in r["web_unavailable"]


def test_resolve_empty_results_are_a_clean_miss_not_a_degradation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(genre_web, "_ddgs_results", lambda q, n=6: [])
    r = genre_web.resolve("Mau P", "Like I Like It")
    assert r["used_web"] is False and r["web_unavailable"] == ""


def test_resolve_answer_carries_no_unavailable_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(genre_web, "_ddgs_results", _fake_search([_BP]))
    monkeypatch.setattr(genre_web, "_fetch_page", _fake_fetcher(
        {_BP: _beatport("Mau P", "Like I Like It", "Tech House")}))
    r = genre_web.resolve("Mau P", "Like I Like It")
    assert r["genre"] == "Tech House" and r["web_unavailable"] == ""


def test_resolve_stops_after_the_first_query_once_tier_a_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cost control: the second search phrasing only runs when the first left the
    track unresolved."""
    queries: list[str] = []

    def _search(query: str, n: int = 6) -> list[dict[str, str]]:
        queries.append(query)
        return [{"title": "", "body": "", "url": _BP}]

    monkeypatch.setattr(genre_web, "_ddgs_results", _search)
    monkeypatch.setattr(genre_web, "_fetch_page", _fake_fetcher(
        {_BP: _beatport("Mau P", "Like I Like It", "Tech House")}))
    genre_web.resolve("Mau P", "Like I Like It")
    assert len(queries) == 1
    assert queries[0] == "Mau P Like I Like It genre beatport"


def test_resolve_tries_the_second_phrasing_when_unresolved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queries: list[str] = []

    def _search(query: str, n: int = 6) -> list[dict[str, str]]:
        queries.append(query)
        return []

    monkeypatch.setattr(genre_web, "_ddgs_results", _search)
    r = genre_web.resolve("Mau P", "Like I Like It")
    assert queries == ['Mau P Like I Like It genre beatport', 'Mau P "Like I Like It" style']
    assert r["genre"] == "" and r["used_web"] is False


def test_resolve_no_artist_title_is_empty() -> None:
    r = genre_web.resolve("", "")
    assert r["genre"] == "" and r["used_web"] is False


def test_resolve_use_web_false_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """There is no offline read to fall back on — the tier IS the web."""
    def boom(*a: object, **k: object) -> object:
        raise AssertionError("searched despite use_web=False")

    monkeypatch.setattr(genre_web, "_ddgs_results", boom)
    r = genre_web.resolve("Mau P", "Like I Like It", use_web=False)
    assert r["genre"] == "" and r["used_web"] is False


def test_resolve_never_raises_when_search_blows_up(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(query: str, n: int = 6) -> list[dict[str, str]]:
        raise RuntimeError("throttled")

    monkeypatch.setattr(genre_web, "_ddgs_results", boom)
    r = genre_web.resolve("A", "B")
    assert r["genre"] == "" and r["source_matched"] is False and r["used_web"] is False


def test_resolve_never_raises_when_fetching_blows_up(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(genre_web, "_ddgs_results", _fake_search([_BP]))

    def boom(url: str) -> dict[str, object]:
        raise ConnectionError("reset")

    monkeypatch.setattr(genre_web, "_fetch_page", boom)
    r = genre_web.resolve("A", "B")
    assert r["genre"] == "" and r["source_matched"] is False


def test_resolve_survives_a_403_and_keeps_reading_the_rest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(genre_web, "_ddgs_results", _fake_search([_TX, _BP]))
    monkeypatch.setattr(genre_web, "_fetch_page", _fake_fetcher(
        {_TX: _beatport("Mau P", "Like I Like It", "Bass House"),
         _BP: _beatport("Mau P", "Like I Like It", "Tech House")},
        fail={_TX}))
    r = genre_web.resolve("Mau P", "Like I Like It")
    assert r["genre"] == "Tech House"


# ---------------------------------------------------------------------------
# fetch policy + readiness
# ---------------------------------------------------------------------------


def test_hard_blocked_host_is_never_fetched(monkeypatch: pytest.MonkeyPatch) -> None:
    """rateyourmusic's robots.txt prohibits automated access to the SERVICE, so
    it is refused before any request is made, on every route."""
    import urllib.request

    def boom(*a: object, **k: object) -> object:
        raise AssertionError("hard-blocked host was requested")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    out = genre_web._fetch_page("https://rateyourmusic.com/release/album/x/y/")
    assert out["text"] == ""
    assert "robots" in out["err"]


def test_non_http_url_is_refused() -> None:
    out = genre_web._fetch_page("javascript:alert(1)")
    assert out["text"] == "" and out["err"] == "non-http url"


@pytest.mark.parametrize(("url", "reason"), [
    ("ftp://example.com/x", "non-http url"),
    ("https://rateyourmusic.com/release/album/x/y/", "robots.txt prohibits automated access"),
    ("http://127.0.0.1:11434/api/tags", "refusing a non-public address"),
    ("http://169.254.169.254/latest/meta-data/", "refusing a non-public address"),
    ("http://192.168.1.1/setup.cgi", "refusing a non-public address"),
])
def test_fetch_refusal_states_the_whole_policy(url: str, reason: str) -> None:
    """One place holds the policy so every redirect hop can be held to it."""
    assert genre_web._fetch_refusal(url) == reason


def test_public_literal_address_is_allowed() -> None:
    assert genre_web._is_public_host("93.184.216.34") is True
    assert genre_web._is_public_host("") is False


def test_a_redirect_is_held_to_the_same_fetch_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """REGRESSION (policy bypass / SSRF): urllib's default opener follows 30x
    anywhere, so the hard block, robots.txt, the scheme check and the per-host
    rate limit were evaluated against the search-result URL ONLY. A page could
    bounce the fetch onto a host the module promises never to touch — or at
    127.0.0.1 / the LAN — and the sole consequence was `tier` being recomputed
    after the request had already been made, with `err` reporting success.

    "localhost" stands in for the hard-blocked host so nothing leaves the box.
    """
    import http.server
    import threading

    requested: list[str] = []

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler's name
            requested.append(self.path)
            if self.path == "/seo-spam":
                self.send_response(302)
                self.send_header("Location", f"http://localhost:{port}/blocked")
                self.end_headers()
                return
            body = b"<html><body>ROUTER ADMIN Genre : Tech House BPM: 126</body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            """Keep the suite's output clean."""

    server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_port
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        monkeypatch.setattr(genre_web, "HARD_BLOCK", frozenset({"localhost"}))
        # The loopback origin only exists because the test can't reach the real
        # web; the address check and robots probe are what we're standing in for.
        monkeypatch.setattr(genre_web, "_is_public_host", lambda host: True)
        monkeypatch.setattr(genre_web, "_robots_allows", lambda url: True)
        monkeypatch.setattr(genre_web, "DOMAIN_MIN_INTERVAL", 0.0)
        out = genre_web._fetch_page(f"http://127.0.0.1:{port}/seo-spam")
    finally:
        server.shutdown()
        server.server_close()

    assert requested == ["/seo-spam"]          # the blocked host was never asked
    assert out["text"] == ""
    assert "refused" in out["err"]


def test_domain_tiers() -> None:
    assert genre_web._domain_tier(_BP) == "A"
    assert genre_web._domain_tier(_TX) == "A"
    assert genre_web._domain_tier(_DC) == "B"
    assert genre_web._domain_tier(_YT) == "C"
    assert genre_web._domain_tier("https://rateyourmusic.com/x") == "X"


def test_resolver_ready_reports_missing_packages(monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib.util

    real = importlib.util.find_spec

    def without(name: str):
        def _find(mod: str, *a: object, **k: object):
            return None if mod == name else real(mod, *a, **k)
        return _find

    monkeypatch.setattr(importlib.util, "find_spec", without("bs4"))
    assert genre_web.resolver_ready() is False
    monkeypatch.setattr(importlib.util, "find_spec", lambda *a, **k: None)
    assert genre_web.resolver_ready() is False


def test_resolver_ready_true_with_both_packages(monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib.util

    monkeypatch.setattr(importlib.util, "find_spec", lambda *a, **k: object())
    assert genre_web.resolver_ready() is True
