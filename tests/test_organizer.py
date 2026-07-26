"""Tests for vibechek.organizer."""

from __future__ import annotations

import unicodedata
from pathlib import Path

import pytest

from vibechek.config import OrganizationConfig
from vibechek.organizer import (
    organize_from_analysis,
    plan_organization,
    route_new_tracks,
    validate_organize_target,
)


def test_plan_buckets_rare_genres_into_other(synthetic_analysis: dict) -> None:
    config = OrganizationConfig(use_subgenres=True, min_genre_size=3)
    plan = plan_organization(synthetic_analysis, config)

    # 4 House tracks: above threshold, go into House/<Subgenre>/
    # 2 Techno: below threshold, go into Other/Techno/
    # 1 Vaporwave: below threshold, goes into Other/Vaporwave/
    house_moves = [m for m in plan.moves if "House" in m.destination.parts and "Other" not in m.destination.parts]
    other_moves = [m for m in plan.moves if "Other" in m.destination.parts]

    assert len(house_moves) == 4
    assert len(other_moves) == 3  # 2 Techno + 1 Vaporwave
    assert {"Techno", "Vaporwave"}.issubset(plan.small_genres)


def test_plan_respects_no_subgenres_flag(synthetic_analysis: dict) -> None:
    config = OrganizationConfig(use_subgenres=False, min_genre_size=3)
    plan = plan_organization(synthetic_analysis, config)

    # When use_subgenres is False, all House tracks land flat in House/
    house_moves = [m for m in plan.moves if "House" in m.destination.parts and "Other" not in m.destination.parts]
    for m in house_moves:
        # destination should be base_dir / House / filename, not base_dir / House / <sub> / filename
        rel = m.destination.relative_to(plan.base_dir)
        assert rel.parts[0] == "House"
        assert len(rel.parts) == 2  # House/<filename>


def test_dry_run_does_not_move_files(synthetic_analysis: dict) -> None:
    config = OrganizationConfig(use_subgenres=True, min_genre_size=3)
    plan_before = plan_organization(synthetic_analysis, config)
    source_paths = [m.source for m in plan_before.moves]
    assert all(p.exists() for p in source_paths)

    stats = organize_from_analysis(synthetic_analysis, config, dry_run=True)

    assert stats.planned == len(plan_before.moves)
    assert stats.moved == 0
    assert all(p.exists() for p in source_paths)  # no files moved


def test_organize_actually_moves_files(synthetic_analysis: dict) -> None:
    config = OrganizationConfig(use_subgenres=True, min_genre_size=3)
    stats = organize_from_analysis(synthetic_analysis, config, dry_run=False)

    assert stats.moved == stats.planned > 0
    assert len(stats.errors) == 0


def test_plan_with_explicit_base_dir_overrides_inferred(synthetic_analysis: dict, tmp_path: Path) -> None:
    custom_root = tmp_path / "custom_destination"
    config = OrganizationConfig(use_subgenres=True, min_genre_size=3)
    plan = plan_organization(synthetic_analysis, config, base_dir=custom_root)
    assert plan.base_dir == custom_root
    for move in plan.moves:
        assert custom_root in move.destination.parents


def test_plan_scan_only_tracks_route_to_unknown(tmp_path: Path) -> None:
    """A scan-only library (no ML run) has ``ml_analysis=None`` on every track.

    Organizing it must NOT crash — every track routes to Unknown/ (then
    Other/Unknown when below min_genre_size). Regression: the planner used
    ``track.get("ml_analysis", {})``, whose ``{}`` default only applies when
    the key is ABSENT. scan_only records carry the key present-but-null, so the
    call returned ``None`` and ``None.get("ml_genre")`` raised
    ``'NoneType' object has no attribute 'get'`` — crashing the whole organize
    flow for any library the user browsed without running ML analysis.
    """
    tracks = []
    for i in range(3):
        f = tmp_path / f"track_{i}.wav"
        f.write_bytes(b"RIFF\x00\x00\x00\x00WAVE")  # planner only checks .exists()
        tracks.append({"path": str(f), "filename": f.name, "ml_analysis": None})
    analysis = {"tracks": tracks}

    config = OrganizationConfig(use_subgenres=True, min_genre_size=10)
    plan = plan_organization(analysis, config)  # must not raise

    assert len(plan.moves) == 3
    for m in plan.moves:
        assert m.genre == "Unknown"
        assert "Other" in m.destination.parts and "Unknown" in m.destination.parts

    # The execute path runs the same planner — dry_run must also survive.
    stats = organize_from_analysis(analysis, config, dry_run=True)
    assert stats.planned == 3 and stats.moved == 0


def test_plan_resolves_nfd_normalized_paths(tmp_path: Path) -> None:
    """An accented track path arriving NFD-normalized (e.g. a macOS-written
    analysis.json applied on another platform) must still be found. Regression:
    plan_organization reported every accented filename as 'File not found' and
    dropped it from the plan."""
    f = tmp_path / "Tiësto - Strings.flac"  # NFC on disk
    f.write_bytes(b"RIFF\x00\x00\x00\x00WAVE")
    nfd = unicodedata.normalize("NFD", str(f))
    if nfd == str(f):
        pytest.skip("platform pre-normalizes filenames")
    tracks = [{"path": nfd, "filename": f.name,
               "ml_analysis": {"ml_genre": "Trance"}}]
    config = OrganizationConfig(use_subgenres=False, min_genre_size=1)
    plan = plan_organization({"tracks": tracks}, config)
    assert plan.errors == []  # was ["File not found: ...Tiësto..."]
    assert len(plan.moves) == 1
    assert plan.moves[0].genre == "Trance"


def test_route_new_tracks_uniquifies_colliding_basenames(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A different staging track with a colliding basename is imported under a
    uniquified name, not silently dropped.

    Regression for the audit's LOW finding: skip-on-exists discarded a
    legitimately-different same-named track (it only showed in skipped_exists,
    never in the library).
    """
    from vibechek import organizer

    staging = tmp_path / "staging"
    library = tmp_path / "library"
    staging.mkdir()

    # A track already in the library's House folder.
    existing = library / "House" / "track.mp3"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"ORIGINAL library track")

    # A DIFFERENT track in staging that happens to share the basename.
    incoming = staging / "track.mp3"
    incoming.write_bytes(b"DIFFERENT staging track content")

    # Avoid needing real ID3 tags: every staging file resolves to genre "House".
    monkeypatch.setattr(organizer, "_read_genre_tag", lambda _fp: "House")

    summary = route_new_tracks(staging, library)

    # The collision was detected (reported) AND the file was still imported.
    assert summary["skipped_exists"] == 1
    assert summary["copied"] == 1
    assert summary["errors"] == 0

    # The original library track is untouched...
    assert existing.read_bytes() == b"ORIGINAL library track"
    # ...and the different incoming track lives alongside it under a unique name.
    house_files = sorted(p.name for p in (library / "House").iterdir())
    assert house_files == ["track.mp3", "track_1.mp3"]
    assert (library / "House" / "track_1.mp3").read_bytes() == b"DIFFERENT staging track content"


def test_plan_genre_only_track_routes_flat_not_unknown_subfolder(tmp_path: Path) -> None:
    """A track with a real genre but NO subgenre must land in flat <Genre>/,
    not <Genre>/Unknown/.

    Regression: ``sanitize_folder_name(None/"")`` returns the literal sentinel
    "Unknown", which is truthy and ``!= genre`` (e.g. "House"), so the
    subgenre branch wrongly routed genre-only tracks to House/Unknown/ with
    reason "ML genre + subgenre". use_subgenres defaults to True, so this was
    the default path for every track whose ml_subgenre was null/missing/empty.
    """
    library = tmp_path / "library"
    library.mkdir()
    cases = [
        ("missing.mp3", {"ml_genre": "House"}),               # no ml_subgenre key
        ("null.mp3", {"ml_genre": "House", "ml_subgenre": None}),
        ("empty.mp3", {"ml_genre": "House", "ml_subgenre": ""}),
    ]
    tracks = []
    for name, ml in cases:
        f = library / name
        f.write_bytes(b"")
        tracks.append({"path": str(f), "filename": name, "ml_analysis": ml})

    config = OrganizationConfig(use_subgenres=True, min_genre_size=1)
    plan = plan_organization({"tracks": tracks}, config, base_dir=library)

    assert len(plan.moves) == 3
    for m in plan.moves:
        rel = m.destination.relative_to(library)
        # Flat House/<file> — exactly two parts, NOT House/Unknown/<file>.
        assert rel.parts[0] == "House"
        assert "Unknown" not in rel.parts, f"{m.destination} wrongly used Unknown/ subfolder"
        assert len(rel.parts) == 2
        assert m.reason == "ML genre"

    # A track WITH a real subgenre still gets the subgenre folder.
    g = library / "withsub.mp3"
    g.write_bytes(b"")
    plan2 = plan_organization(
        {"tracks": [{"path": str(g), "filename": g.name,
                     "ml_analysis": {"ml_genre": "House", "ml_subgenre": "Deep House"}}]},
        config, base_dir=library,
    )
    rel = plan2.moves[0].destination.relative_to(library)
    assert rel.parts[:2] == ("House", "Deep House")


def test_route_dry_run_matches_real_run_for_colliding_basenames(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """route_new_tracks dry-run must report the SAME rename count as the real
    run when two different staging files share a basename + genre.

    Regression: dry_run never wrote the copies, so the second file's dest
    didn't exist on disk at check time and skipped_exists stayed 0 — the
    preview claimed no renames while the real run renamed one file to _1.
    Fixed by tracking an in-batch ``claimed`` set like plan_organization.
    """
    from vibechek import organizer

    staging = tmp_path / "staging"
    library_dry = tmp_path / "lib_dry"
    library_real = tmp_path / "lib_real"
    staging.mkdir()

    # Two DIFFERENT staging files sharing the same basename, both genre House.
    sub_a = staging / "a"
    sub_b = staging / "b"
    sub_a.mkdir()
    sub_b.mkdir()
    (sub_a / "track.mp3").write_bytes(b"content A")
    (sub_b / "track.mp3").write_bytes(b"content B")

    monkeypatch.setattr(organizer, "_read_genre_tag", lambda _fp: "House")

    dry = route_new_tracks(staging, library_dry, dry_run=True)
    real = route_new_tracks(staging, library_real, dry_run=False)

    # The preview must agree with reality.
    assert dry["copied"] == real["copied"] == 2
    assert dry["skipped_exists"] == real["skipped_exists"] == 1

    # And the real run actually wrote both under unique names.
    real_files = sorted(p.name for p in (library_real / "House").iterdir())
    assert real_files == ["track.mp3", "track_1.mp3"]


def test_organize_cancel_midbatch_preserves_journal_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancelling an organize mid-batch must still expose the undo-journal path
    (and partial stats) so the GUI can offer "Undo this organize" for the
    files that already moved.

    Regression: journal_path was only assigned AFTER the try/finally block, so
    a CancelledError skipped it — the caller got no stats and no undo
    affordance even though real files had moved. Fixed by capturing the journal
    path on cancellation and attaching the partial stats to the exception.
    """
    from vibechek import cancellation, journal

    library = tmp_path / "library"
    library.mkdir()
    tracks = []
    for i in range(6):
        f = library / f"t{i}.mp3"
        f.write_bytes(b"")
        tracks.append({
            "path": str(f), "filename": f.name,
            "ml_analysis": {"ml_genre": "House"},
        })
    analysis = {"tracks": tracks}
    config = OrganizationConfig(use_subgenres=False, min_genre_size=1)

    # Make journals land in a temp dir so we never touch the user's data dir.
    monkeypatch.setattr(journal, "JOURNALS_DIR", tmp_path / "journals")

    # Trip the cancel flag after 2 moves have been recorded by intercepting
    # cancellation.check() (called at the top of each loop iteration).
    real_check = cancellation.check
    calls = {"n": 0}

    def fake_check() -> None:
        calls["n"] += 1
        # check() runs before each move; allow the first 2 moves, cancel on
        # the 3rd iteration's check.
        if calls["n"] == 3:
            raise cancellation.CancelledError("test cancel")
        real_check()

    monkeypatch.setattr(cancellation, "check", fake_check)

    with pytest.raises(cancellation.CancelledError) as excinfo:
        organize_from_analysis(analysis, config, dry_run=False)

    # The exception carries partial stats with a usable journal path.
    partial = getattr(excinfo.value, "partial_stats", None)
    assert partial is not None
    assert partial.moved == 2
    assert partial.journal_path is not None
    jp = Path(partial.journal_path)
    assert jp.exists()

    # The journal records exactly the 2 moves that happened, so a revert is
    # possible for the partial organize.
    _header, entries = journal._read_journal(jp)
    moves = [e for e in entries if e.get("action") == "move"]
    assert len(moves) == 2
    # And those two files physically moved into House/.
    assert sorted(p.name for p in (library / "House").iterdir()) == ["t0.mp3", "t1.mp3"]


# ---------------------------------------------------------------------------
# Destination-aware small-genre decision (incremental organize)
# ---------------------------------------------------------------------------


def _batch(tmp_path: Path, genre: str, n: int, sub: str | None = None) -> dict:
    """n new tracks of one genre, as an analysis dict, files created on disk."""
    src = tmp_path / "incoming"
    src.mkdir(exist_ok=True)
    tracks = []
    for i in range(n):
        p = src / f"{genre.lower()}_{i}.mp3"
        p.write_bytes(b"x")
        ml = {"ml_genre": genre}
        if sub:
            ml["ml_subgenre"] = sub
        tracks.append({"path": str(p), "ml_analysis": ml})
    return {"tracks": tracks}


def _fill(folder: Path, n: int) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        (folder / f"existing_{i}.mp3").write_bytes(b"x")


def test_incremental_batch_joins_established_genre_folder(tmp_path: Path) -> None:
    """3 new House tracks + an established House/ folder must NOT go to Other/.

    Regression: the small-genre decision counted only the batch, so every
    incremental organize shoved small batches into Other/ no matter how many
    files the destination's genre folder already held.
    """
    lib = tmp_path / "lib"
    _fill(lib / "House", 11)
    config = OrganizationConfig(use_subgenres=False, min_genre_size=10, target_root=lib)
    plan = plan_organization(_batch(tmp_path, "House", 3), config)

    assert "House" not in plan.small_genres
    assert len(plan.moves) == 3
    for m in plan.moves:
        assert "Other" not in m.destination.parts
        assert m.destination.parent == lib / "House"
    # Census caps at min_genre_size — 10 reads as "10 or more".
    assert plan.existing_genre_counts["House"] == 10


def test_small_batch_plus_few_existing_still_goes_to_other(tmp_path: Path) -> None:
    lib = tmp_path / "lib"
    _fill(lib / "House", 4)
    config = OrganizationConfig(use_subgenres=False, min_genre_size=10, target_root=lib)
    plan = plan_organization(_batch(tmp_path, "House", 3), config)

    assert "House" in plan.small_genres  # 3 + 4 = 7 < 10
    assert plan.existing_genre_counts["House"] == 4
    for m in plan.moves:
        assert "Other" in m.destination.parts


def test_existing_genre_dir_casing_is_reused(tmp_path: Path) -> None:
    """A batch genre 'House' with an on-disk 'house/' must reuse 'house/' —
    not create a case-duplicate sibling on case-sensitive filesystems."""
    lib = tmp_path / "lib"
    _fill(lib / "house", 12)
    config = OrganizationConfig(use_subgenres=False, min_genre_size=10, target_root=lib)
    plan = plan_organization(_batch(tmp_path, "House", 2), config)

    assert "House" not in plan.small_genres
    for m in plan.moves:
        assert m.destination.parent.name == "house"


def test_other_and_unknown_folders_do_not_establish_genres(tmp_path: Path) -> None:
    """Files already bucketed under Other/ (or Unknown/) must not count as an
    established genre — else Other/Techno/ would self-perpetuate forever."""
    lib = tmp_path / "lib"
    _fill(lib / "Other" / "Techno", 15)
    _fill(lib / "Unknown", 15)
    config = OrganizationConfig(use_subgenres=False, min_genre_size=10, target_root=lib)
    plan = plan_organization(_batch(tmp_path, "Techno", 2), config)

    assert "Techno" in plan.small_genres
    assert plan.existing_genre_counts["Techno"] == 0


def test_census_counts_nested_subgenre_layouts(tmp_path: Path) -> None:
    """Genre/Subgenre nesting counts toward the genre's establishment."""
    lib = tmp_path / "lib"
    _fill(lib / "House" / "Tech House", 11)
    config = OrganizationConfig(use_subgenres=True, min_genre_size=10, target_root=lib)
    plan = plan_organization(_batch(tmp_path, "House", 2, sub="Bass House"), config)

    assert "House" not in plan.small_genres
    for m in plan.moves:
        assert m.destination.parent == lib / "House" / "Bass House"


# ---------------------------------------------------------------------------
# Re-organizing an ALREADY-SORTED library in place
#
# The workflow: a library is already filed into genre folders, gets re-analyzed,
# and some genres come back corrected. Those tracks must move into the right
# folder and everything else must stay put. Both guards around this were wrong.
# ---------------------------------------------------------------------------


def _sorted_library(root: Path) -> None:
    """A library already filed as Root/<Genre>/track.mp3."""
    for genre, names in (("Techno", ["a.mp3", "b.mp3"]), ("House", ["c.mp3"])):
        (root / genre).mkdir(parents=True)
        for n in names:
            (root / genre / n).write_bytes(b"x")


def _retag_analysis(root: Path) -> dict:
    """a.mp3 was re-analyzed as Minimal Techno; b and c are correctly filed."""
    return {"tracks": [
        {"path": str(root / "Techno" / "a.mp3"),
         "ml_analysis": {"ml_genre": "Minimal Techno"}},
        {"path": str(root / "Techno" / "b.mp3"), "ml_analysis": {"ml_genre": "Techno"}},
        {"path": str(root / "House" / "c.mp3"), "ml_analysis": {"ml_genre": "House"}},
    ]}


def test_sorted_library_infers_root_not_first_tracks_genre_folder(tmp_path: Path) -> None:
    """With no target_root, base_dir must be the LIBRARY, not Root/<first genre>.

    Regression: the fallback was `Path(tracks[0]["path"]).parent`, which for a
    sorted library is a genre folder. That didn't merely fail to move the
    re-tagged track — it planned to move every CORRECTLY-filed track into a tree
    nested under that one genre (House/c.mp3 -> Techno/House/c.mp3).
    """
    lib = tmp_path / "lib"
    _sorted_library(lib)
    config = OrganizationConfig(use_subgenres=False, min_genre_size=1, target_root=None)

    plan = plan_organization(_retag_analysis(lib), config)

    assert plan.base_dir == lib
    # Exactly one move: the re-tagged track. The other two are already correct.
    assert [m.source.name for m in plan.moves] == ["a.mp3"]
    assert plan.moves[0].destination == lib / "Minimal Techno" / "a.mp3"


def test_explicit_library_root_beats_path_inference(tmp_path: Path) -> None:
    """`library_root` is authoritative — it settles the case paths cannot.

    All analyzed tracks sitting in ONE genre folder is indistinguishable from a
    flat library by paths alone, so the common-ancestor guess returns the genre
    folder. The caller that knows the real root must win.
    """
    lib = tmp_path / "lib"
    (lib / "Techno").mkdir(parents=True)
    (lib / "Techno" / "a.mp3").write_bytes(b"x")
    analysis = {"tracks": [
        {"path": str(lib / "Techno" / "a.mp3"),
         "ml_analysis": {"ml_genre": "Minimal Techno"}},
    ]}
    config = OrganizationConfig(use_subgenres=False, min_genre_size=1, target_root=None)

    guessed = plan_organization(analysis, config)
    assert guessed.base_dir == lib / "Techno"  # the ambiguity, documented

    told = plan_organization(analysis, config, library_root=lib)
    assert told.base_dir == lib
    assert told.moves[0].destination == lib / "Minimal Techno" / "a.mp3"


def test_target_root_still_beats_library_root(tmp_path: Path) -> None:
    """An explicit target_root must not be overridden by library_root."""
    lib = tmp_path / "lib"
    _sorted_library(lib)
    elsewhere = tmp_path / "sorted"
    config = OrganizationConfig(use_subgenres=False, min_genre_size=1, target_root=elsewhere)

    plan = plan_organization(_retag_analysis(lib), config, library_root=lib)

    assert plan.base_dir == elsewhere


def test_organize_in_place_moves_only_the_retagged_track(tmp_path: Path) -> None:
    """End-to-end: re-organizing in place relocates the corrected track only."""
    lib = tmp_path / "lib"
    _sorted_library(lib)
    config = OrganizationConfig(use_subgenres=False, min_genre_size=1, target_root=None)

    stats = organize_from_analysis(_retag_analysis(lib), config, library_root=lib)

    assert stats.moved == 1
    assert stats.errors == []
    assert (lib / "Minimal Techno" / "a.mp3").exists()
    # Correctly-filed tracks never moved.
    assert (lib / "Techno" / "b.mp3").exists()
    assert (lib / "House" / "c.mp3").exists()
    assert not (lib / "Techno" / "a.mp3").exists()


def test_validate_allows_target_equal_to_library_with_a_warning(tmp_path: Path) -> None:
    """Organizing into the library itself is in-place reorganization, not an error.

    It used to be a hard block, which made "my library is sorted, just fix the
    wrong ones" impossible via the only path that names the root explicitly.
    """
    lib = tmp_path / "lib"
    lib.mkdir()

    check = validate_organize_target(lib, source_library=lib)

    assert check["ok"] is True
    assert check["error"] is None
    assert any("in place" in w for w in check["warnings"])


# ---------------------------------------------------------------------------
# route_new_tracks: canonical spelling + content-free bucketing
#
# `vibechek route` files by the user's tags on purpose. It normalizes the
# SPELLING of a tag (so it agrees with the analyzed path) and refuses to let a
# content-free tag mint a top-level folder — but it never overrides what a tag
# says.
# ---------------------------------------------------------------------------


def _staged(tmp_path: Path, tag: str, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    from vibechek import organizer

    staging = tmp_path / "staging"
    library = tmp_path / "library"
    staging.mkdir()
    (staging / "track.mp3").write_bytes(b"x")
    monkeypatch.setattr(organizer, "_read_genre_tag", lambda _fp: tag)
    return staging, library


@pytest.mark.parametrize(("tag", "folder"), [
    ("Hip-Hop", "Hip Hop"),                       # spelling alias
    ("D&B", "Drum & Bass"),                       # punctuation alias
    ("Techno (Peak Time / Driving)", "Techno"),   # bracket qualifier
    ("Minimal / Deep Tech", "Minimal Techno"),    # measured, not the obvious guess
])
def test_route_canonicalizes_tag_spelling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tag: str, folder: str,
) -> None:
    """Routing must agree with the analyzed path on how a genre is SPELLED.

    Otherwise a library fed by both `vibechek route` and analyze+organize grows
    two folders for one genre — exactly what the alias table exists to prevent.
    """
    staging, library = _staged(tmp_path, tag, monkeypatch)

    summary = route_new_tracks(staging, library)

    assert summary["copied"] == 1
    assert (library / folder / "track.mp3").exists()


def test_route_keeps_the_specific_genre_not_its_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """"Tech House" files under Tech House/, not its parent House/.

    Canonicalization must not silently coarsen the user's tag — this command
    files by what the tag says.
    """
    staging, library = _staged(tmp_path, "Tech House", monkeypatch)

    route_new_tracks(staging, library)

    assert (library / "Tech House" / "track.mp3").exists()
    assert not (library / "House" / "track.mp3").exists()


@pytest.mark.parametrize("tag", ["Dance", "EDM", "Unknown"])
def test_route_sends_content_free_tags_to_other(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tag: str,
) -> None:
    """A tag that names no genre must not mint a top-level folder.

    This is how `Dance/` reached 878 files in a real library. The label is kept
    (under Other/) rather than discarded — the file is still findable.
    """
    staging, library = _staged(tmp_path, tag, monkeypatch)

    summary = route_new_tracks(staging, library)

    assert summary["routed_to_other"] == 1
    assert (library / "Other" / tag / "track.mp3").exists()
    assert not (library / tag).exists()


@pytest.mark.parametrize("tag", ["Electro", "Hypeddit Top Weekly Picks"])
def test_route_does_not_apply_tag_distrust(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tag: str,
) -> None:
    """Spray tags and playlist phrases still get their own folder here.

    `is_specific_genre` distrusts both, but that distrust OVERRIDES what the tag
    says, and overriding the tag is the one thing this command must not do.
    Deliberate: `vibechek route` is a literal "file by my tags".
    """
    staging, library = _staged(tmp_path, tag, monkeypatch)

    summary = route_new_tracks(staging, library)

    assert summary["copied"] == 1
    assert summary["routed_to_other"] == 0
    assert (library / tag / "track.mp3").exists()
