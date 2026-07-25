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
    assert genre_web._canon("Dance/Pop") == "Pop"
    assert genre_web._canon("Tech House") == "Tech House"
    assert genre_web._canon("") == ""


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
    # the hazard, pinned: this is what the greedy capture used to feed the canon
    assert genre_web._canon("Electronic Style") == "Electro"

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


def test_parse_field_canons_each_segment() -> None:
    text = genre_web._extract_text(
        _beatport("A", "B", "Trance (Main Floor) | Progressive Trance"))
    got = genre_web._parse_field(text, "A")
    assert got is not None and got[0] == "Trance"


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
