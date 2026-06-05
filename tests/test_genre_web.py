"""Tests for the online web-synthesis genre resolver (vibechek/genre_web.py).

The reconcile/canon/evidence logic is pure-python; the LLM HTTP call (`_llm_chat`)
and `ddgs` search (`_ddgs_snippets`) are monkeypatched so nothing hits the network
or needs Ollama/ddgs installed. The contract under test: `resolve()` NEVER raises
and degrades to an empty read on any failure.
"""

from __future__ import annotations

import sys

import pytest

from vibechek import genre_web


def test_module_is_import_clean() -> None:
    for heavy in ("ddgs", "duckduckgo_search"):
        assert heavy not in sys.modules


def test_canon_maps_chart_buckets_and_vocab() -> None:
    assert genre_web._canon("Dance/Pop") == "Pop"
    assert genre_web._canon("Tech House") == "Tech House"
    assert genre_web._canon("") == ""


def test_evidence_supports() -> None:
    assert genre_web._evidence_supports("Funky House", "RYM: Genres: Funky House")
    assert genre_web._evidence_supports("Tech House", "Beatport — Genre: Tech House. BPM 128")
    assert not genre_web._evidence_supports("Trance", "a title with no genre word")
    assert not genre_web._evidence_supports("Pop", "")


def test_resolve_no_artist_title_is_empty() -> None:
    r = genre_web.resolve("", "")
    assert r["genre"] == "" and r["used_web"] is False


def test_resolve_grounded_match(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(genre_web, "_ddgs_snippets",
                        lambda q, n=6: "- Mau P - Like I Like It | Beatport: Genre: Tech House")
    monkeypatch.setattr(genre_web, "_llm_chat",
                        lambda *a, **k: {"genre": "Tech House", "confidence": 0.9,
                                         "source_matched": True,
                                         "evidence": "Beatport: Genre: Tech House"})
    r = genre_web.resolve("Mau P", "Like I Like It", "House", "House")
    assert r["genre"] == "Tech House"
    assert r["source_matched"] is True
    assert r["used_web"] is True


def test_resolve_hallucinated_source_is_downgraded(monkeypatch: pytest.MonkeyPatch) -> None:
    """source_matched is honoured ONLY if the evidence text names the genre."""
    monkeypatch.setattr(genre_web, "_ddgs_snippets", lambda q, n=6: "- some chart - various artists")
    monkeypatch.setattr(genre_web, "_llm_chat",
                        lambda *a, **k: {"genre": "Trance", "confidence": 0.9,
                                         "source_matched": True,  # claims a match...
                                         "evidence": "DJ chart, artist: VA"})  # ...but no genre cited
    r = genre_web.resolve("Some", "Track")
    assert r["genre"] == "Trance"
    assert r["source_matched"] is False  # evidence gate rejected the claim


def test_resolve_never_raises_on_llm_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a, **k):
        raise ConnectionError("no ollama")
    monkeypatch.setattr(genre_web, "_ddgs_snippets", lambda q, n=6: "(no results)")
    monkeypatch.setattr(genre_web, "_llm_chat", boom)
    r = genre_web.resolve("A", "B")
    assert r == {"genre": "", "confidence": 0.0, "source_matched": False, "used_web": False}


def test_resolve_survives_ddgs_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A web-search blowup falls back to a parametric (no-web) classification."""
    def ddgs_boom(q, n=6):
        raise RuntimeError("throttled")
    monkeypatch.setattr(genre_web, "_ddgs_snippets", ddgs_boom)
    monkeypatch.setattr(genre_web, "_llm_chat",
                        lambda *a, **k: {"genre": "Deep House", "confidence": 0.6,
                                         "source_matched": False, "evidence": ""})
    r = genre_web.resolve("nimino", "Better")
    assert r["genre"] == "Deep House"
    assert r["used_web"] is False
