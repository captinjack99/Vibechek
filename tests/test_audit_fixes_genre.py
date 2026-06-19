"""Regression tests for the post-genre-feature audit round.

Each test pins a confirmed finding from the end-to-end review: CLAP worker
sizing, same-version WSL drift self-heal, the web-lookup phase's cancellation/
backend probing/checkpoint honesty, genre_web canonicalization + grounding,
the Psytrance family mapping, kNN vote shares, config snap-backs, RPC
parameter threading, and the organizer's actual-moved-pairs contract.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from vibechek import analyzer, cancellation, genre_web
from vibechek.genres import reconcile_genre, split_tag_genre

# ---------------------------------------------------------------------------
# CLAP worker memory budget
# ---------------------------------------------------------------------------


def test_per_worker_mb_accounts_for_clap() -> None:
    """CLAP loads a ~2.2 GB model into EVERY worker (MEASURED ~3.8 GB resident),
    so the RAM cap budgets 4.5 GB — not the essentia-only 800 MB (which put a
    32 GB box at 15 workers ≈ 50 GB) and not the old 3.5 GB (which still let
    three 3.8 GB workers OOM a 16 GB box)."""
    assert analyzer._per_worker_mb("discogs") == 800
    assert analyzer._per_worker_mb("clap") == 4500
    # The bug: a 16 GB box under WSL must cap CLAP at 2 workers, not 3 — each
    # worker is ~3.8 GB and WSL shares RAM with the Windows host.
    assert (16384 - 4096) // analyzer._per_worker_mb("clap") == 2
    # A 32 GB native box stays generous but bounded.
    assert (32768 - 2048) // analyzer._per_worker_mb("clap") <= 8


def test_running_under_wsl_detected_via_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """WSL always sets WSL_DISTRO_NAME; the worker-cap keys its larger RAM
    reserve off this so it doesn't starve the Windows host."""
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu")
    assert analyzer._running_under_wsl() is True
    assert isinstance(analyzer._running_under_wsl(), bool)  # never raises


# ---------------------------------------------------------------------------
# Same-version-but-stale WSL install: "No such option" → upgrade + retry
# ---------------------------------------------------------------------------


def _stub_preflight(version: str | None) -> MagicMock:
    distro = MagicMock(vibechek_installed=True, essentia_installed=True,
                       vibechek_version=version)
    distro.name = "Ubuntu"
    wsl_status = MagicMock(usable_distro="Ubuntu", distros=[distro])
    return MagicMock(ready=True, analyze_via="wsl", wsl=wsl_status,
                     reasons_not_ready=[])


def test_no_such_option_triggers_inplace_upgrade_and_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A WSL install at the SAME version but older code (no release bump
    between commits) rejects a newly-added CLI flag with click's "No such
    option" — the version-drift guard can't see that (versions are equal), so
    the rejection itself must trigger the in-place upgrade + one retry."""
    import vibechek as _vibechek  # noqa: PLC0415
    monkeypatch.setattr(_vibechek, "__version__", "0.5.0-beta", raising=False)

    (tmp_path / "x.flac").write_bytes(b"\x00")
    output = tmp_path / "out.json"
    output.write_text('{"tracks": [], "status": "complete", "summary": {}}', encoding="utf-8")

    calls = {"n": 0}

    def fake_run(distro, args, on_stderr_line=None, venv_subdir="venv"):
        calls["n"] += 1
        if calls["n"] == 1:
            if on_stderr_line:
                on_stderr_line("Error: No such option: --genre-classifier")
            return MagicMock(returncode=2, stdout="", stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    fake_upgrade = MagicMock(return_value={"ok": True})

    with patch("vibechek.preflight.preflight",
               return_value=_stub_preflight("0.5.0-beta")), \
         patch("vibechek.utils.find_audio_files", return_value=[tmp_path / "x.flac"]), \
         patch("vibechek.wsl.upgrade_vibechek_in_wsl", fake_upgrade), \
         patch("vibechek.wsl.run_vibechek_in_wsl", side_effect=fake_run), \
         patch("vibechek.wsl.win_to_wsl_path", side_effect=lambda s: s), \
         patch("vibechek.wsl.wsl_to_win_path", side_effect=lambda s: s):
        from vibechek.config import AnalysisConfig  # noqa: PLC0415
        report = analyzer.analyze_directory(
            tmp_path,
            config=AnalysisConfig(workers=1, use_gpu="off"),
            output_path=output,
        )

    assert calls["n"] == 2                      # failed once, retried once
    assert fake_upgrade.call_count == 1
    assert report.get("status") == "complete"


# ---------------------------------------------------------------------------
# Web-lookup phase: backend probe, caching, cancellation, checkpoint honesty
# ---------------------------------------------------------------------------


def _track(path: str, artist: str, title: str, tag: str | None = None) -> dict:
    return {
        "path": path, "filename": Path(path).name,
        "existing_tags": {"genre": tag, "artist": artist, "title": title},
        "ml_analysis": {"ml_genre": "House", "ml_subgenre": "Deep House",
                        "ml_genre_raw_confidence": 0.4},
    }


def test_web_phase_skipped_loudly_when_backend_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the local LLM isn't reachable (and can't be started), the web tier
    must be skipped ENTIRELY — not pay a wasted web search per track only to
    silently fall back."""
    monkeypatch.setattr(genre_web, "ensure_backend", lambda *a, **k: False)

    def boom(*a, **k):  # resolve must never be called in this scenario
        raise AssertionError("genre_web.resolve called despite dead backend")

    monkeypatch.setattr(genre_web, "resolve", boom)
    rep = analyzer._build_report(
        [_track("/m/a.flac", "A", "T", "Dance/Pop")], 1, in_progress=False,
        web_cfg={"enabled": True, "backend": "ollama"},
    )
    # Reconciliation still ran (generic tag -> audio read).
    assert rep["tracks"][0]["ml_analysis"]["ml_genre_source"] == "ml"


def test_web_phase_caches_per_artist_title(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(genre_web, "ensure_backend", lambda *a, **k: True)
    calls = []

    def fake_resolve(artist, title, tag, audio, **kw):
        calls.append((artist, title))
        return {"genre": "Tech House", "confidence": 0.9,
                "source_matched": True, "used_web": True}

    monkeypatch.setattr(genre_web, "resolve", fake_resolve)
    tracks = [
        _track("/m/a.flac", "Mau P", "BOBA"),       # same song, two files
        _track("/m/b.mp3", "Mau P", "BOBA"),
        _track("/m/c.flac", "Other", "Song"),
    ]
    analyzer._build_report(tracks, 3, in_progress=False,
                           web_cfg={"enabled": True, "backend": "ollama"})
    assert len(calls) == 2                       # cached for the duplicate pair


def test_web_phase_is_cancellable(monkeypatch: pytest.MonkeyPatch) -> None:
    """The reconcile loop must observe cancellation — it runs AFTER the
    progress bar hits N/N and used to be unstoppable."""
    cancellation.begin("analyze")
    try:
        cancellation.cancel()
        with pytest.raises(cancellation.CancelledError):
            analyzer._build_report(
                [_track("/m/a.flac", "A", "T")], 1, in_progress=False,
            )
    finally:
        cancellation.end()


def test_checkpoint_writes_never_claim_complete(tmp_path: Path) -> None:
    """_write_partial is a crash-recovery checkpoint: it must always stamp
    in_progress, even at the final-iteration call site that used to pass
    in_progress=False — only analyze_directory's real final build (with the
    user's genre policy + web lookup) may claim "complete"."""
    out = tmp_path / "analysis.json"
    analyzer._write_partial(out, [_track("/m/a.flac", "A", "T")], 1, in_progress=False)
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["status"] == "in_progress"
    # And no reconciliation happened (that's the final build's job).
    assert "ml_genre_source" not in data["tracks"][0]["ml_analysis"]


# ---------------------------------------------------------------------------
# genre_web: canonicalization, backend probe, grounding
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("raw", "expected"), [
    ("house", "House"),                 # generic stays generic (was "Tech House")
    ("House", "House"),
    ("disco", "Disco"),                 # was "Nu-Disco"
    ("electro", "Electro"),             # was "Electro House"
    ("Tech-House", "Tech House"),       # separator-insensitive exact
    ("tech_house", "Tech House"),
    ("Melodic House & Techno (peak)", "Melodic House & Techno"),  # longest contained
    ("progressive house", "Progressive House"),
])
def test_canon_never_invents_specificity(raw: str, expected: str) -> None:
    assert genre_web._canon(raw) == expected


def test_ensure_backend_true_when_reachable(monkeypatch: pytest.MonkeyPatch) -> None:
    import urllib.request  # noqa: PLC0415

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _Resp())
    assert genre_web.ensure_backend("ollama") is True


def test_ensure_backend_false_when_unreachable_and_no_install(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    import urllib.request  # noqa: PLC0415

    def boom(*a, **k):
        raise ConnectionError("refused")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)   # no ~/ollama install
    assert genre_web.ensure_backend("ollama") is False
    assert genre_web.ensure_backend("not-a-backend") is False


def test_grounding_requires_actual_web_results(monkeypatch: pytest.MonkeyPatch) -> None:
    """With ZERO web snippets the model can still claim source_matched and
    fabricate evidence text that names the genre — grounding must be refused
    when no search results were actually seen."""
    def ddgs_boom(q, n=6):
        raise RuntimeError("throttled")

    monkeypatch.setattr(genre_web, "_ddgs_snippets", ddgs_boom)
    monkeypatch.setattr(
        genre_web, "_llm_chat",
        lambda *a, **k: {"genre": "Tech House", "confidence": 0.95,
                         "source_matched": True,
                         "evidence": "Beatport: Genre: Tech House"},
    )
    r = genre_web.resolve("A", "B")
    assert r["genre"] == "Tech House"
    assert r["used_web"] is False
    assert r["source_matched"] is False


# ---------------------------------------------------------------------------
# Genre taxonomy: canonical names keep their parents
# ---------------------------------------------------------------------------


def test_canonical_subgenres_inherit_parent() -> None:
    assert split_tag_genre("Psytrance") == ("Trance", "Psytrance")
    assert split_tag_genre("UK Garage") == ("House", "UK Garage")


def test_same_family_web_does_not_override_canonical_tag() -> None:
    """Grounded web "Psytrance" vs a correct "Trance" tag is the SAME family —
    it must keep the tag, not fire web_override (the lost-parent bug)."""
    r = reconcile_genre("House", "Deep House", 0.4, "Trance",
                        policy="prefer_tag", web_genre="Psytrance",
                        web_grounded=True)
    assert r.source == "tag"
    assert r.conflict is False


# ---------------------------------------------------------------------------
# clap_genre vote shares
# ---------------------------------------------------------------------------


def test_knn_vote_shares_normalized() -> None:
    import numpy as np  # noqa: PLC0415

    from vibechek import clap_genre  # noqa: PLC0415

    emb = np.eye(4, dtype=np.float32)
    ref = clap_genre.ClapReference(
        emb=emb, labels=np.array(["Tech House", "Deep House", "Trance", "Pop"],
                                 dtype=object), meta={})
    shares = clap_genre.knn_vote_shares(
        np.array([0.9, 0.4, 0.1, 0.0], dtype=np.float32), ref, k=4)
    assert shares
    assert abs(sum(shares.values()) - 1.0) < 1e-6
    assert max(shares, key=shares.__getitem__) == "Tech House"
    # degenerate inputs -> {}
    assert clap_genre.knn_vote_shares(np.zeros(4, dtype=np.float32), ref) == {}


# ---------------------------------------------------------------------------
# Organizer: ACTUAL moved pairs (the GUI's path-update contract)
# ---------------------------------------------------------------------------


def test_organize_reports_actual_moved_pairs(tmp_path: Path) -> None:
    """moved_pairs must contain only files that actually moved, with their
    ACTUAL (possibly re-uniquified) destinations — the GUI maps store paths
    from this list, and mapping the plan pointed unmoved/renamed tracks at
    phantom locations."""
    from vibechek.config import OrganizationConfig  # noqa: PLC0415
    from vibechek.organizer import organize_from_analysis  # noqa: PLC0415

    lib = tmp_path / "lib"
    lib.mkdir()
    a = lib / "a.mp3"
    a.write_bytes(b"a")
    gone = lib / "gone.mp3"          # planned but vanishes before execute
    gone.write_bytes(b"g")
    analysis = {"tracks": [
        {"path": str(a), "filename": "a.mp3",
         "ml_analysis": {"ml_genre": "House", "ml_subgenre": "Deep House",
                         "ml_genre_confidence": 0.99}},
        {"path": str(gone), "filename": "gone.mp3",
         "ml_analysis": {"ml_genre": "House", "ml_subgenre": "Deep House",
                         "ml_genre_confidence": 0.99}},
    ]}
    cfg = OrganizationConfig(use_subgenres=False, min_genre_size=1)
    gone.unlink()                     # vanish AFTER planning input built
    stats = organize_from_analysis(analysis, cfg, base_dir=lib)
    # The vanished file is excluded at plan time (plan.errors), so exactly one
    # move was planned AND executed — moved_pairs must carry its ACTUAL pair.
    assert stats.planned == 1
    assert stats.moved == 1
    assert len(stats.moved_pairs) == 1
    src, dst = stats.moved_pairs[0]
    assert src == str(a)
    assert Path(dst).exists()
    assert not a.exists()             # really moved, not copied
