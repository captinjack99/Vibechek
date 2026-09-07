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


def test_blank_library_root_falls_through_to_inference(tmp_path: Path) -> None:
    """A blank `library_root` must not become `Path("")` — i.e. the CWD.

    The sidecar already blanks an empty `library_path`, but the old
    `library_root is not None` test here meant any caller passing "" rooted the
    whole genre tree at "." — wherever the process happened to be started.
    """
    lib = tmp_path / "lib"
    _sorted_library(lib)
    config = OrganizationConfig(use_subgenres=False, min_genre_size=1, target_root=None)

    plan = plan_organization(_retag_analysis(lib), config, library_root="")

    assert plan.base_dir == lib
    assert [m.source.name for m in plan.moves] == ["a.mp3"]


# ---------------------------------------------------------------------------
# base_dir must be absolute — belt-and-braces behind the sidecar's own strip
# ---------------------------------------------------------------------------


def test_relative_base_dir_is_refused(tmp_path: Path) -> None:
    """A relative base_dir would scatter the library under the CWD."""
    lib = tmp_path / "lib"
    _sorted_library(lib)
    config = OrganizationConfig(use_subgenres=False, min_genre_size=1, target_root=None)

    with pytest.raises(ValueError, match="absolute path"):
        plan_organization(_retag_analysis(lib), config, base_dir=Path("sorted"))


def test_relative_target_root_is_refused(tmp_path: Path) -> None:
    """Same guard on the config route — validate_organize_target can be bypassed.

    `plan_organization` is a public entry point (CLI, library use); the sidecar's
    pre-flight validator is not the only path to it.
    """
    lib = tmp_path / "lib"
    _sorted_library(lib)
    config = OrganizationConfig(
        use_subgenres=False, min_genre_size=1, target_root="Sorted",
    )

    with pytest.raises(ValueError, match="absolute path"):
        plan_organization(_retag_analysis(lib), config)


def test_whitespace_only_target_root_is_refused_not_treated_as_cwd(
    tmp_path: Path,
) -> None:
    """`Path("   ")` is relative, not blank — refuse instead of using the CWD."""
    lib = tmp_path / "lib"
    _sorted_library(lib)
    config = OrganizationConfig(
        use_subgenres=False, min_genre_size=1, target_root="   ",
    )

    with pytest.raises(ValueError, match="absolute path"):
        plan_organization(_retag_analysis(lib), config)


def test_absolute_refusal_message_names_the_offending_path(tmp_path: Path) -> None:
    """The error is user-facing: it must say what was wrong and what to type."""
    lib = tmp_path / "lib"
    _sorted_library(lib)
    config = OrganizationConfig(use_subgenres=False, min_genre_size=1, target_root=None)

    with pytest.raises(ValueError) as exc:
        plan_organization(_retag_analysis(lib), config, base_dir=Path("sorted"))

    message = str(exc.value)
    assert "sorted" in message
    assert "folder picker" in message


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


# ---------------------------------------------------------------------------
# Pruning the folders an in-place re-organize empties
#
# The only place Vibechek removes a directory, so the envelope is tight:
# confined to the library root, strictly-empty only, never deletes a file.
# ---------------------------------------------------------------------------


def test_organize_reports_emptied_dirs_without_removing_them(tmp_path: Path) -> None:
    """Organize REPORTS empty folders; removal is a separate, confirmed step."""
    lib = tmp_path / "lib"
    _sorted_library(lib)
    # a.mp3 is Techno's only remaining track once b is removed from the analysis,
    # so moving it empties Techno/.
    (lib / "Techno" / "b.mp3").unlink()
    analysis = {"tracks": [
        {"path": str(lib / "Techno" / "a.mp3"),
         "ml_analysis": {"ml_genre": "Minimal Techno"}},
    ]}
    config = OrganizationConfig(use_subgenres=False, min_genre_size=1, target_root=None)

    stats = organize_from_analysis(analysis, config, library_root=lib)

    assert stats.emptied_dirs == [str((lib / "Techno").resolve())]
    assert (lib / "Techno").exists()  # reported, NOT removed


def test_organize_does_not_report_a_folder_that_still_has_tracks(tmp_path: Path) -> None:
    """A folder that kept a track is not a prune candidate."""
    lib = tmp_path / "lib"
    _sorted_library(lib)  # Techno/ holds a.mp3 AND b.mp3
    analysis = {"tracks": [
        {"path": str(lib / "Techno" / "a.mp3"),
         "ml_analysis": {"ml_genre": "Minimal Techno"}},
    ]}
    config = OrganizationConfig(use_subgenres=False, min_genre_size=1, target_root=None)

    stats = organize_from_analysis(analysis, config, library_root=lib)

    assert stats.emptied_dirs == []


def test_prune_removes_empty_dirs_and_cascades_to_parents(tmp_path: Path) -> None:
    """Emptying House/Tech House/ leaves House/ empty — both go in one pass."""
    from vibechek.organizer import prune_empty_dirs

    lib = tmp_path / "lib"
    nested = lib / "House" / "Tech House"
    nested.mkdir(parents=True)

    summary = prune_empty_dirs([nested], lib)

    assert sorted(summary["removed"]) == sorted([
        str(nested.resolve()), str((lib / "House").resolve()),
    ])
    assert not (lib / "House").exists()
    assert lib.exists()  # the root itself is never removed


def test_prune_never_removes_a_non_empty_dir(tmp_path: Path) -> None:
    """Re-checked at prune time: a folder that gained a file is left alone."""
    from vibechek.organizer import prune_empty_dirs

    lib = tmp_path / "lib"
    genre = lib / "Techno"
    genre.mkdir(parents=True)
    (genre / "late_arrival.mp3").write_bytes(b"x")

    summary = prune_empty_dirs([genre], lib)

    assert summary["removed"] == []
    assert genre.exists()
    assert "not empty" in summary["skipped"][0]["reason"]


def test_prune_treats_os_cruft_as_not_empty(tmp_path: Path) -> None:
    """A folder holding only .DS_Store stays: removing it means deleting a FILE.

    Deliberate — pruning is allowed to remove directories, never file content.
    """
    from vibechek.organizer import prune_empty_dirs

    lib = tmp_path / "lib"
    genre = lib / "Techno"
    genre.mkdir(parents=True)
    (genre / ".DS_Store").write_bytes(b"x")

    summary = prune_empty_dirs([genre], lib)

    assert summary["removed"] == []
    assert (genre / ".DS_Store").exists()


def test_prune_refuses_paths_outside_the_root(tmp_path: Path) -> None:
    """Confinement: a path outside the library root is refused, not removed."""
    from vibechek.organizer import prune_empty_dirs

    lib = tmp_path / "lib"
    lib.mkdir()
    outsider = tmp_path / "not_my_library"
    outsider.mkdir()

    summary = prune_empty_dirs([outsider, lib], lib)

    assert summary["removed"] == []
    assert outsider.exists()
    assert lib.exists()
    assert {s["reason"] for s in summary["skipped"]} == {"outside the library root"}


def test_prune_is_idempotent(tmp_path: Path) -> None:
    """Running it twice reports the second pass as gone, not as an error."""
    from vibechek.organizer import prune_empty_dirs

    lib = tmp_path / "lib"
    genre = lib / "Techno"
    genre.mkdir(parents=True)

    first = prune_empty_dirs([genre], lib)
    second = prune_empty_dirs([genre], lib)

    assert first["removed"] == [str(genre.resolve())]
    assert second["removed"] == []
    assert second["errors"] == []
    assert second["skipped"][0]["reason"] == "no longer exists"


def test_undo_after_prune_restores_the_folder(tmp_path: Path) -> None:
    """Pruning must not break Undo — revert recreates the parent directory."""
    from vibechek.journal import revert_journal
    from vibechek.organizer import prune_empty_dirs

    lib = tmp_path / "lib"
    (lib / "Techno").mkdir(parents=True)
    (lib / "Techno" / "a.mp3").write_bytes(b"x")
    analysis = {"tracks": [
        {"path": str(lib / "Techno" / "a.mp3"),
         "ml_analysis": {"ml_genre": "Minimal Techno"}},
    ]}
    config = OrganizationConfig(use_subgenres=False, min_genre_size=1, target_root=None)

    stats = organize_from_analysis(analysis, config, library_root=lib)
    prune_empty_dirs(stats.emptied_dirs, lib)
    assert not (lib / "Techno").exists()

    summary = revert_journal(stats.journal_path)

    assert summary["reverted"] == 1
    assert (lib / "Techno" / "a.mp3").exists()


# ---------------------------------------------------------------------------
# Failure modes the audit found: no silent guessing, no half-written files, no
# undo record that lies about being complete.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("stray", ["relative/dir/b.mp3", "another/rel/b.mp3"])
def test_tracks_spanning_roots_refuse_to_guess_a_base_dir(
    tmp_path: Path, stray: str,
) -> None:
    """No common ancestor means no library root — say so instead of guessing.

    The old fallback was `parents[0]`, i.e. the first track's parent, which for
    a sorted library is a GENRE folder: every track in the analysis would then
    be planned inside it (House/c.mp3 -> Techno/House/c.mp3). The CLI turns this
    ValueError into a clean message; guessing had no such backstop.
    """
    analysis = {"tracks": [
        {"path": str(tmp_path / "Techno" / "a.mp3"),
         "ml_analysis": {"ml_genre": "Techno"}},
        {"path": stray, "ml_analysis": {"ml_genre": "House"}},
    ]}
    config = OrganizationConfig(use_subgenres=False, min_genre_size=1, target_root=None)

    with pytest.raises(ValueError, match="different drives or roots"):
        plan_organization(analysis, config)


def test_explicit_root_still_works_for_tracks_spanning_roots(tmp_path: Path) -> None:
    """Refusing to GUESS must not refuse the case where the caller knows."""
    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / "a.mp3").write_bytes(b"x")
    analysis = {"tracks": [
        {"path": str(lib / "a.mp3"), "ml_analysis": {"ml_genre": "Techno"}},
        {"path": "relative/b.mp3", "ml_analysis": {"ml_genre": "House"}},
    ]}
    config = OrganizationConfig(use_subgenres=False, min_genre_size=1, target_root=None)

    plan = plan_organization(analysis, config, library_root=lib)

    assert plan.base_dir == lib


def test_failed_move_removes_the_half_written_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cross-device move that dies mid-copy must not leave a truncated file.

    shutil.move falls back to copy+unlink across volumes (organizing onto an
    external drive is the normal case), and a copy killed by a full disk leaves
    a partial file at the destination with a valid extension. It isn't in the
    journal (record_move never ran), so undo can't remove it and a later
    organize just uniquifies around it.
    """
    from vibechek import organizer

    lib = tmp_path / "lib"
    lib.mkdir()
    victim = lib / "victim.mp3"
    victim.write_bytes(b"full contents")
    analysis = {"tracks": [
        {"path": str(victim), "ml_analysis": {"ml_genre": "House"}},
    ]}

    # `copy_function=` is now passed through (see _copy_preserving_stat), so the
    # stub has to accept the same signature shutil.move really has.
    def _die_mid_copy(src: str, dst: str, copy_function=None) -> None:  # noqa: ANN001, ARG001
        Path(dst).write_bytes(b"trunc")     # what copyfile leaves behind
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(organizer.shutil, "move", _die_mid_copy)
    config = OrganizationConfig(use_subgenres=False, min_genre_size=1)

    stats = organize_from_analysis(analysis, config, base_dir=lib)

    assert stats.moved == 0
    assert not (lib / "House" / "victim.mp3").exists()
    assert victim.read_bytes() == b"full contents"   # original untouched
    assert "removed the incomplete copy" in stats.errors[0]


def test_a_failed_move_keeps_the_source_when_cleanup_would_be_the_last_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cleanup only fires while the source is still there — never delete the
    only copy of a track because the tail end of a move raised."""
    from vibechek import organizer

    lib = tmp_path / "lib"
    lib.mkdir()
    victim = lib / "victim.mp3"
    victim.write_bytes(b"full contents")
    analysis = {"tracks": [
        {"path": str(victim), "ml_analysis": {"ml_genre": "House"}},
    ]}

    def _move_then_raise(src: str, dst: str, copy_function=None) -> None:  # noqa: ANN001, ARG001
        Path(dst).write_bytes(Path(src).read_bytes())
        Path(src).unlink()                  # the move DID complete
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(organizer.shutil, "move", _move_then_raise)
    config = OrganizationConfig(use_subgenres=False, min_genre_size=1)

    stats = organize_from_analysis(analysis, config, base_dir=lib)

    assert (lib / "House" / "victim.mp3").read_bytes() == b"full contents"
    assert "removed the incomplete copy" not in stats.errors[0]


def test_journal_write_failure_marks_the_undo_record_incomplete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Moves the journal didn't record can't be undone — say so.

    Without this the run reports zero errors, the GUI renders Undo, and the
    revert reports "reverted N, skipped 0, errors 0" while the unrecorded files
    stay stranded in the new tree.
    """
    from vibechek import journal as _journal

    lib = tmp_path / "lib"
    lib.mkdir()
    tracks = []
    for i in range(3):
        f = lib / f"t{i}.mp3"
        f.write_bytes(b"x")
        tracks.append({"path": str(f), "ml_analysis": {"ml_genre": "House"}})

    real_start = _journal.start_journal

    def _start_then_fill_the_disk(kind: str, root=None):
        j = real_start(kind, root=root)
        original_write = j._fh.write
        state = {"left": 1}

        def _write(s: str) -> int:
            if state["left"] <= 0:
                raise OSError(28, "No space left on device")
            state["left"] -= 1
            return original_write(s)

        j._fh.write = _write
        return j

    monkeypatch.setattr(_journal, "start_journal", _start_then_fill_the_disk)
    config = OrganizationConfig(use_subgenres=False, min_genre_size=1)

    stats = organize_from_analysis({"tracks": tracks}, config, base_dir=lib)

    assert stats.moved == 3
    assert stats.errors == []          # the moves themselves all succeeded
    assert stats.journal_path is not None
    assert stats.journal_incomplete is True


def test_a_healthy_organize_does_not_claim_an_incomplete_journal(
    synthetic_analysis: dict,
) -> None:
    config = OrganizationConfig(use_subgenres=True, min_genre_size=3)
    stats = organize_from_analysis(synthetic_analysis, config)
    assert stats.moved > 0
    assert stats.journal_incomplete is False


def test_moved_pairs_key_on_the_path_the_caller_supplied(tmp_path: Path) -> None:
    """The GUI looks moved_pairs up by exact string against what it SENT.

    `resolve_existing_path` may hand back a different Unicode normalization of
    the same file (the whole reason it exists), and reporting THAT spelling
    means the lookup misses and the track keeps a dead path until a re-scan.
    """
    lib = tmp_path / "lib"
    lib.mkdir()
    on_disk = unicodedata.normalize("NFD", "Tiësto - Adagio.mp3")
    stored = str(lib / unicodedata.normalize("NFC", "Tiësto - Adagio.mp3"))
    (lib / on_disk).write_bytes(b"x")
    analysis = {"tracks": [
        {"path": stored, "ml_analysis": {"ml_genre": "Trance"}},
    ]}
    config = OrganizationConfig(use_subgenres=False, min_genre_size=1)

    stats = organize_from_analysis(analysis, config, base_dir=lib)

    assert stats.moved == 1
    assert stats.moved_pairs[0][0] == stored
    assert Path(stats.moved_pairs[0][1]).exists()


# ---------------------------------------------------------------------------
# _read_genre_tag — every route test monkeypatches it, so the reader itself
# had no coverage at all. These use real files.
# ---------------------------------------------------------------------------


def _wav_with_genre(path: Path, values: list[str]) -> Path:
    """A real RIFF WAV carrying an ID3v2 TCON frame, exactly as the tagger writes it."""
    import wave as _wave

    from mutagen.id3 import TCON
    from mutagen.wave import WAVE

    with _wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(44100)
        w.writeframes(b"\x00\x00" * 128)
    audio = WAVE(str(path))
    audio.add_tags()
    audio.tags.add(TCON(encoding=3, text=values))
    audio.save()
    return path


def test_read_genre_tag_reads_wav_id3(tmp_path: Path) -> None:
    """Vibechek's own tagger writes ID3 genre frames into WAV — the router has
    to be able to read them back, or it reports a file it tagged itself as
    untagged and the user's "re-tag it" fix does nothing."""
    from vibechek.organizer import _read_genre_tag

    fp = _wav_with_genre(tmp_path / "t.wav", ["Tech House"])

    assert _read_genre_tag(fp) == "Tech House"


def test_read_genre_tag_takes_the_first_of_a_multi_value_frame(tmp_path: Path) -> None:
    """`str(TCON)` NUL-joins every value ("Techno\x00House" — what Picard and
    foobar2000 write for ID3v2.4). That NUL reached mkdir, which raises
    ValueError, NOT OSError, so it escaped route's handler and killed the batch."""
    from vibechek.organizer import _read_genre_tag

    fp = _wav_with_genre(tmp_path / "t.wav", ["Techno", "House"])

    assert _read_genre_tag(fp) == "Techno"


def test_read_genre_tag_reads_ogg_vorbis(tmp_path: Path) -> None:
    """`.ogg` is a supported extension too, and an externally Vorbis-tagged file
    was reported as untagged for the same reason `.wav` was — no read arm."""
    sf = pytest.importorskip("soundfile")
    import numpy as np
    from mutagen.oggvorbis import OggVorbis

    from vibechek.organizer import _read_genre_tag

    fp = tmp_path / "t.ogg"
    sf.write(str(fp), np.zeros(4410, dtype="float32"), 44100,
             format="OGG", subtype="VORBIS")
    audio = OggVorbis(str(fp))
    audio["genre"] = ["Tech House"]
    audio.save()

    assert _read_genre_tag(fp) == "Tech House"


def _wma_with_genre(path: Path, genre: str, attr_type: int = 0) -> Path:
    """Write a minimal-but-real ASF/WMA file carrying a WM/Genre descriptor.

    Hand-built rather than synthesized: there is no encoder in the dev deps, and
    mutagen's ASF reader is the thing under test, so the bytes have to be a real
    ASF header object (GUID + size + one Extended Content Description object).

    `attr_type` is the ASF attribute type: 0 = unicode (what WMP and Rekordbox
    write), 1 = byte array, 3 = DWORD. The non-unicode types are legal and a
    reader that just str()s the attribute mints a folder out of the repr.
    """
    import struct
    import uuid

    header_guid = uuid.UUID("75B22630-668E-11CF-A6D9-00AA0062CE6C").bytes_le
    ext_content_guid = uuid.UUID("D2D0A440-E307-11D2-97F0-00A0C95EA850").bytes_le

    def utf16z(s: str) -> bytes:
        return s.encode("utf-16-le") + b"\x00\x00"

    name = utf16z("WM/Genre")
    if attr_type == 0:
        value = utf16z(genre)
    elif attr_type == 1:
        value = genre.encode("utf-8")              # raw byte array
    elif attr_type == 3:
        value = struct.pack("<I", 130)             # a DWORD genre id
    else:  # pragma: no cover - guard against a typo in a future case
        raise AssertionError(f"unhandled ASF attribute type {attr_type}")
    body = (
        struct.pack("<H", 1)                       # one descriptor
        + struct.pack("<H", len(name)) + name
        + struct.pack("<HH", attr_type, len(value)) + value
    )
    ext = ext_content_guid + struct.pack("<Q", 16 + 8 + len(body)) + body
    path.write_bytes(
        header_guid
        + struct.pack("<Q", 16 + 8 + 4 + 2 + len(ext))
        + struct.pack("<I", 1)                     # one header sub-object
        + b"\x01\x02"                              # reserved1 / reserved2
        + ext
    )
    return path


def test_read_genre_tag_reads_wma_asf(tmp_path: Path) -> None:
    """`.wma` is a supported extension, and mutagen reads its genre from the
    ASF "WM/Genre" descriptor. Without the arm, a file tagged by Windows Media
    Player or Rekordbox was reported as having no genre and left in staging —
    the same silent misreport the missing `.wav` arm caused."""
    from vibechek.organizer import _read_genre_tag

    fp = _wma_with_genre(tmp_path / "t.wma", "Tech House")

    assert _read_genre_tag(fp) == "Tech House"


@pytest.mark.parametrize("attr_type", [1, 3])
def test_read_genre_tag_ignores_a_non_text_wma_attribute(
    tmp_path: Path, attr_type: int,
) -> None:
    """An ASF attribute is TYPED. The descriptor can legally hold a DWORD or a
    raw byte array, and str()ing one of those stringifies to a number or a
    `b'...'` repr — which `route_new_tracks` would then mint a real folder out
    of. A value that isn't text is no genre at all."""
    from vibechek.organizer import _read_genre_tag

    fp = _wma_with_genre(tmp_path / "t.wma", "Tech House", attr_type=attr_type)

    assert _read_genre_tag(fp) is None


def test_route_leaves_a_wma_with_a_non_text_genre_in_staging(tmp_path: Path) -> None:
    """End-to-end: the untyped read minted a folder named after the attribute's
    repr. Nothing gets filed under a number."""
    staging = tmp_path / "staging"
    library = tmp_path / "library"
    staging.mkdir()
    _wma_with_genre(staging / "a.wma", "Tech House", attr_type=3)

    summary = route_new_tracks(staging, library)

    assert summary["copied"] == 0
    assert summary["skipped_no_genre"] == 1
    assert not library.exists() or list(library.iterdir()) == []


def test_route_files_a_wma_by_its_asf_genre(tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    library = tmp_path / "library"
    staging.mkdir()
    _wma_with_genre(staging / "a.wma", "Tech House")

    summary = route_new_tracks(staging, library)

    assert summary["copied"] == 1
    assert summary["skipped_no_genre"] == 0
    assert (library / "Tech House" / "a.wma").exists()


def test_route_names_aac_as_unreadable_rather_than_untagged(tmp_path: Path) -> None:
    """mutagen exposes no tag reader for raw `.aac` (mutagen.aac.AAC has no
    `.tags`), so the genre can never be read no matter how the file was tagged.
    Counting that as "no genre tag" sends the user off to re-tag a file whose
    tags we could not read either way; it gets its own counter and log line."""
    staging = tmp_path / "staging"
    library = tmp_path / "library"
    staging.mkdir()
    (staging / "a.aac").write_bytes(b"\xff\xf1nope")
    _wav_with_genre(staging / "b.wav", ["Trance"])

    summary = route_new_tracks(staging, library)

    assert summary["skipped_unreadable_format"] == 1
    assert summary["skipped_no_genre"] == 0
    assert summary["copied"] == 1
    assert (library / "Trance" / "b.wav").exists()


def test_route_still_counts_a_genuinely_untagged_file_as_no_genre(
    tmp_path: Path,
) -> None:
    """The new counter must not swallow the ordinary case."""
    staging = tmp_path / "staging"
    library = tmp_path / "library"
    staging.mkdir()
    _wav_with_genre(staging / "a.wav", [""])

    summary = route_new_tracks(staging, library)

    assert summary["skipped_no_genre"] == 1
    assert summary["skipped_unreadable_format"] == 0


def test_route_files_a_multi_value_tagged_track_instead_of_crashing(
    tmp_path: Path,
) -> None:
    """End-to-end: a two-value genre frame must route one track, not abort the run."""
    staging = tmp_path / "staging"
    library = tmp_path / "library"
    staging.mkdir()
    _wav_with_genre(staging / "a.wav", ["Techno", "House"])
    _wav_with_genre(staging / "b.wav", ["Trance"])

    summary = route_new_tracks(staging, library)

    assert summary["copied"] == 2
    assert summary["errors"] == 0
    assert (library / "Techno" / "a.wav").exists()
    assert (library / "Trance" / "b.wav").exists()


def test_route_skips_one_unwritable_file_instead_of_aborting_the_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ValueError out of mkdir/copy2 (a path-shaped tag) used to escape the
    per-file handler, so the remaining tracks were never routed and the user
    got a traceback instead of a summary."""
    from vibechek import organizer

    staging, library = _staged(tmp_path, "Techno", monkeypatch)
    (staging / "second.mp3").write_bytes(b"x")

    calls = {"n": 0}
    real_copyfile = organizer.shutil.copyfile

    def _first_one_explodes(src: str, dst: str):
        calls["n"] += 1
        if calls["n"] == 1:
            Path(dst).write_bytes(b"trunc")   # partial copy left behind
            raise ValueError("embedded null character in path")
        return real_copyfile(src, dst)

    # The route loop copies through `copyfile` now, so that a copystat failure
    # on exFAT/SMB can no longer be mistaken for a truncated copy (R12).
    monkeypatch.setattr(organizer.shutil, "copyfile", _first_one_explodes)

    summary = route_new_tracks(staging, library)

    # find_audio_files sorts by path, so "second.mp3" is the one that explodes
    # and "track.mp3" proves the batch carried on past it.
    assert summary["errors"] == 1
    assert summary["copied"] == 1
    # ...and the half-written file the failed copy left behind is gone.
    assert [p.name for p in (library / "Techno").iterdir()] == ["track.mp3"]


# ---------------------------------------------------------------------------
# A copystat failure must not discard a complete copy (R12)
# ---------------------------------------------------------------------------


def _reject_copystat(monkeypatch: pytest.MonkeyPatch) -> None:
    """exFAT sticks and SMB shares routinely reject os.utime/os.chmod on a file
    that copied perfectly — the failure `shutil.copy2` raises AFTER every byte
    has landed."""
    from vibechek import organizer

    def _boom(*_a: object, **_kw: object) -> None:
        raise PermissionError(13, "Operation not permitted")

    monkeypatch.setattr(organizer.shutil, "copystat", _boom)


def test_route_keeps_a_copy_whose_only_failure_was_copystat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`shutil.copy2` is copyfile + copystat, and the old handler could not tell
    the two apart: a copystat failure sent a COMPLETE file to
    `_discard_partial_copy`, which unlinked it. Routing onto an exFAT/SMB
    library reported `copied: 0, errors: N` over a transfer that worked, every
    retry included.
    """
    staging, library = _staged(tmp_path, "Techno", monkeypatch)
    _reject_copystat(monkeypatch)

    summary = route_new_tracks(staging, library)

    assert summary["copied"] == 1
    assert summary["errors"] == 0
    dest = library / "Techno" / "track.mp3"
    assert dest.exists()
    assert dest.read_bytes() == (staging / "track.mp3").read_bytes()


def test_route_still_discards_a_copy_whose_bytes_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other half: when the BYTE copy fails it really can leave a truncated
    file behind, and that one must still be removed."""
    from vibechek import organizer

    staging, library = _staged(tmp_path, "Techno", monkeypatch)

    def _half_written(src: str, dst: str) -> None:
        Path(dst).write_bytes(b"trunc")
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(organizer.shutil, "copyfile", _half_written)

    summary = route_new_tracks(staging, library)

    assert summary["copied"] == 0
    assert summary["errors"] == 1
    assert not (library / "Techno" / "track.mp3").exists()
    assert (staging / "track.mp3").exists()   # the source is never touched


def test_copy_preserving_stat_applies_metadata_when_it_can(tmp_path: Path) -> None:
    """The warning path must not become the only path: on a filesystem that
    accepts it, the timestamps are still preserved."""
    import os

    from vibechek.organizer import _copy_preserving_stat

    src = tmp_path / "a.mp3"
    src.write_bytes(b"audio")
    os.utime(src, (1_600_000_000, 1_600_000_000))
    dest = tmp_path / "b.mp3"

    _copy_preserving_stat(src, dest)

    assert dest.read_bytes() == b"audio"
    assert int(dest.stat().st_mtime) == 1_600_000_000


def test_organize_move_survives_a_copystat_failure_on_a_cross_device_move(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`shutil.move`'s cross-device fallback is copy2 + unlink, so the same
    copystat failure turned a working organize onto an external drive into
    `0 moved / N errors` with an empty destination. The move loop hands
    shutil.move its own copy_function for exactly this reason.
    """
    from vibechek import organizer

    lib = tmp_path / "lib"
    lib.mkdir()
    track = lib / "t.mp3"
    track.write_bytes(b"audio")

    # Force shutil.move down its cross-device branch for THIS file only (a
    # blanket os.rename patch would also break the journal's atomic writes),
    # then fail the copystat that branch runs after the bytes have landed.
    real_rename = organizer.os.rename

    def _no_rename(src, dst, *a, **kw):  # noqa: ANN001, ANN202
        if str(src) == str(track):
            raise OSError(18, "Invalid cross-device link")
        return real_rename(src, dst, *a, **kw)

    monkeypatch.setattr(organizer.os, "rename", _no_rename)
    _reject_copystat(monkeypatch)

    analysis = {"tracks": [{"path": str(track), "ml_analysis": {
        "ml_genre": "Techno", "ml_subgenre": "Hard Techno",
        "ml_genre_confidence": 0.95,
    }}]}
    stats = organize_from_analysis(
        analysis, OrganizationConfig(use_subgenres=True, min_genre_size=1),
        dry_run=False, library_root=str(lib),
    )

    assert stats.errors == []
    assert stats.moved == 1
    assert not track.exists()
    assert (lib / "Techno" / "Hard Techno" / "t.mp3").read_bytes() == b"audio"
