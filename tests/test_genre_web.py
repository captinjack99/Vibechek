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


def test_resolve_skips_ungrounded_llm_when_ddgs_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """A web-search blowup must NOT masquerade as an online read.

    When the caller asked for a web-grounded genre but ddgs failed (throttle,
    outage), the LLM would otherwise guess from its training data alone and the
    pipeline would label it "Set by an online source" at 0.9 — a web-verified
    provenance the user never got. The tier is skipped instead (empty genre →
    reconcile falls back to the honest audio read), and the LLM is never called.
    """
    def ddgs_boom(q, n=6):
        raise RuntimeError("throttled")
    llm_calls: list = []

    def spy_llm(*a, **k):
        llm_calls.append(a)
        return {"genre": "Deep House", "confidence": 0.6,
                "source_matched": False, "evidence": ""}

    monkeypatch.setattr(genre_web, "_ddgs_snippets", ddgs_boom)
    monkeypatch.setattr(genre_web, "_llm_chat", spy_llm)
    r = genre_web.resolve("nimino", "Better")
    assert r["genre"] == ""          # no ungrounded read leaks out as "web"
    assert r["used_web"] is False
    assert llm_calls == []           # didn't even pay for the ungrounded guess


def test_resolve_skips_ungrounded_llm_when_ddgs_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """ddgs returning zero hits ("(no results)") is the same masquerade risk —
    also skip rather than let the LLM guess and be labelled a web read."""
    monkeypatch.setattr(genre_web, "_ddgs_snippets", lambda q, n=6: "(no results)")
    called = []
    monkeypatch.setattr(genre_web, "_llm_chat",
                        lambda *a, **k: called.append(a) or {"genre": "Trance"})
    r = genre_web.resolve("nimino", "Better")
    assert r["genre"] == "" and r["used_web"] is False
    assert called == []
