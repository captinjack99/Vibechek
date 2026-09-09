"""Tests for vibechek.duplicates."""

from __future__ import annotations

from pathlib import Path

import pytest

from vibechek.config import DuplicateConfig
from vibechek.duplicates import (
    DuplicateAction,
    FileInfo,
    choose_keeper,
    file_md5,
    find_duplicates,
    handle_duplicates,
)

# ---------- Pure helpers ----------


def test_file_md5_matches_known_string(tmp_path: Path) -> None:
    f = tmp_path / "x.mp3"
    f.write_bytes(b"hello world")
    # echo -n "hello world" | md5sum → 5eb63bbbe01eeed093cb22bb8f5acdc3
    assert file_md5(f) == "5eb63bbbe01eeed093cb22bb8f5acdc3"


def _fi(path: str, size_mb: float = 5.0) -> FileInfo:
    return FileInfo(path=path, filename=Path(path).name,
                    size_bytes=int(size_mb * 1_000_000), size_mb=size_mb)


def test_merge_overlapping_clusters_unions_shared_files() -> None:
    """A~B in one bucket and B~C in another must collapse to ONE group {A,B,C},
    not two overlapping groups (which let a file be keeper AND duplicate)."""
    from vibechek.duplicates import _merge_overlapping_clusters

    a, b, c, d = _fi("/m/a.mp3"), _fi("/m/b.mp3"), _fi("/m/c.mp3"), _fi("/m/d.mp3")
    merged = _merge_overlapping_clusters([[a, b], [b, c]])
    assert len(merged) == 1
    assert {f.path for f in merged[0]} == {"/m/a.mp3", "/m/b.mp3", "/m/c.mp3"}
    # Disjoint clusters stay separate.
    merged2 = _merge_overlapping_clusters([[a, b], [c, d]])
    assert len(merged2) == 2


def test_handle_duplicates_never_moves_a_keeper_of_another_group(tmp_path: Path) -> None:
    """Defense-in-depth: even if a report has overlapping groups where a file is
    the KEEPER of one group and a DUPLICATE in another, handle_duplicates must
    NOT move/trash that keeper (the data-loss bug)."""
    from vibechek.duplicates import DuplicateGroup, DuplicateReport

    for name in ("a.mp3", "b.mp3", "c.mp3"):
        (tmp_path / name).write_bytes(b"x" * 1000)
    a, b, c = (_fi(str(tmp_path / n)) for n in ("a.mp3", "b.mp3", "c.mp3"))
    report = DuplicateReport()
    # b is a DUPLICATE in group 1 but the KEEPER of group 2.
    report.audio_duplicates = [
        DuplicateGroup(method="chromaprint", key="g1", keep=a, duplicates=[b], recoverable_mb=0.0),
        DuplicateGroup(method="chromaprint", key="g2", keep=b, duplicates=[c], recoverable_mb=0.0),
    ]
    review = tmp_path / "review"
    cfg = DuplicateConfig(action="move", review_folder=str(review),
                          use_md5=False, use_chromaprint=True)
    summary = handle_duplicates(report, cfg)
    assert (tmp_path / "a.mp3").exists(), "keeper a must stay"
    assert (tmp_path / "b.mp3").exists(), "b is a keeper of group 2 — must NOT be moved"
    assert not (tmp_path / "c.mp3").exists(), "c (a real dupe, not any keeper) should move"
    assert summary["moved"] == 1 and summary["errors"] == 0


def _fiv(name: str, dur: float | None, size_mb: float = 5.0) -> FileInfo:
    return FileInfo(path=f"/m/{name}", filename=name,
                    size_bytes=int(size_mb * 1_000_000), size_mb=size_mb, duration_s=dur)


# ---- Variant awareness: keep distinct versions, collapse only same-version ----


def test_version_key_distinguishes_real_versions() -> None:
    from vibechek.filename import version_key
    assert version_key("Song (Original Mix)") == version_key("Song")          # original == base
    assert version_key("Song (Extended Mix)") != version_key("Song (Radio Edit)")
    assert version_key("Song (Extended Mix)") != version_key("Song")          # extended != base
    assert version_key("Song (Imanbek Remix)") != version_key("Song")         # remix != original
    assert version_key("Song (Imanbek Remix)") != version_key("Song (CamelPhat Remix)")
    assert version_key("Song (Imanbek Remix)") == version_key("X - Song (Imanbek Remix)")


def test_variant_keeps_extended_and_radio_separate() -> None:
    """Extended Mix + Radio Edit of one song = two versions a DJ keeps."""
    from vibechek.duplicates import _split_into_versions
    groups = _split_into_versions(
        [_fiv("Song (Extended Mix).flac", 360), _fiv("Song (Radio Edit).flac", 180)], 0.12)
    assert len(groups) == 2


def test_variant_flac_original_mp3_extended_both_kept() -> None:
    """The user's exact case: FLAC original + MP3 extended = different versions."""
    from vibechek.duplicates import _split_into_versions
    groups = _split_into_versions(
        [_fiv("Song.flac", 200, 40), _fiv("Song (Extended Mix).mp3", 360, 12)], 0.12)
    assert len(groups) == 2


def test_variant_collapses_flac_mp3_of_same_version() -> None:
    """FLAC + MP3 of the SAME version is a true duplicate → keep the FLAC."""
    from vibechek.duplicates import _resolve_encodings, _split_into_versions
    flac = _fiv("Song (Extended Mix).flac", 360, 40)
    mp3 = _fiv("Song (Extended Mix).mp3", 360, 12)
    groups = _split_into_versions([flac, mp3], 0.12)
    assert len(groups) == 1
    (keeper, dupes), = _resolve_encodings(groups[0], DuplicateConfig())
    assert keeper.filename.endswith(".flac")
    assert [d.filename for d in dupes] == ["Song (Extended Mix).mp3"]


def test_keep_all_formats_keeps_one_per_format() -> None:
    """keep_all_formats: a controller-friendly MP3 is kept alongside the FLAC."""
    from vibechek.duplicates import _resolve_encodings
    flac = _fiv("Song (Extended Mix).flac", 360, 40)
    mp3 = _fiv("Song (Extended Mix).mp3", 360, 12)
    pairs = _resolve_encodings([flac, mp3], DuplicateConfig(keep_all_formats=True))
    assert len(pairs) == 2
    assert all(len(dupes) == 0 for _, dupes in pairs)  # nothing removed


def test_mislabeled_extended_radio_split_by_duration() -> None:
    """Two files with the same (empty) version key but very different durations
    are still treated as different versions (a mislabeled extended/radio pair)."""
    from vibechek.duplicates import _split_into_versions
    groups = _split_into_versions(
        [_fiv("Song.flac", 360), _fiv("Song.mp3", 180)], 0.12)
    assert len(groups) == 2


def test_choose_keeper_prefers_flac_over_mp3() -> None:
    files = [
        FileInfo(path="/a/track.mp3", filename="track.mp3", size_bytes=5_000_000, size_mb=5.0),
        FileInfo(path="/a/track.flac", filename="track.flac", size_bytes=20_000_000, size_mb=20.0),
    ]
    keeper, dupes = choose_keeper(files)
    assert keeper.filename == "track.flac"
    assert dupes[0].filename == "track.mp3"


def test_choose_keeper_prefers_larger_on_format_tie() -> None:
    files = [
        FileInfo(path="/a/short.mp3", filename="short.mp3", size_bytes=3_000_000, size_mb=3.0),
        FileInfo(path="/a/long.mp3", filename="long.mp3", size_bytes=8_000_000, size_mb=8.0),
    ]
    keeper, _ = choose_keeper(files)
    assert keeper.filename == "long.mp3"


def test_choose_keeper_prefers_shorter_path_on_size_tie() -> None:
    files = [
        FileInfo(path="/very/deep/folder/track.mp3", filename="track.mp3",
                 size_bytes=1000, size_mb=0.001),
        FileInfo(path="/a/track.mp3", filename="track.mp3",
                 size_bytes=1000, size_mb=0.001),
    ]
    keeper, _ = choose_keeper(files)
    assert keeper.path == "/a/track.mp3"


def test_choose_keeper_ranks_folder_depth_not_character_count() -> None:
    """REGRESSION: the rule is "prefer the canonical location over a /dupes/
    copy" — folder DEPTH, which is also what the GUI renders ("N levels"). It
    compared `len(path)` in characters, and on any realistic pair the two
    disagree: a deeply-buried short name beat a top-level long one, and the UI
    explained the win with a level count larger than the loser's."""
    deep = FileInfo(path="/DJ/Sets/2024/Peak/A.mp3", filename="A.mp3",
                    size_bytes=5_000_000, size_mb=5.0)
    shallow = FileInfo(path="/Archive/Artist - Title (Extended Mix) [Label].mp3",
                       filename="Artist - Title (Extended Mix) [Label].mp3",
                       size_bytes=5_000_000, size_mb=5.0)
    assert len(deep.path) < len(shallow.path)       # character count disagrees
    keeper, dupes = choose_keeper([deep, shallow])
    assert keeper.path == shallow.path
    assert dupes[0].path == deep.path


def test_unknown_duration_never_joins_a_known_length_group() -> None:
    """REGRESSION: `duration_s` is None exactly when the mutagen probe fails —
    the corrupt/truncated case. Attaching such a file to the first sub-group
    voided the guard `test_mislabeled_extended_radio_split_by_duration` exists
    to protect: a truncated `.flac` joined the full track's group and, ranking
    lossless-first, became the KEEPER while the healthy MP3 was listed for
    trash."""
    from vibechek.duplicates import _split_into_versions

    truncated = _fiv("Song.flac", None, 3.0)
    full = _fiv("Song.mp3", 360, 12.0)
    assert len(_split_into_versions([truncated, full], 0.12)) == 2
    assert len(_split_into_versions([full, truncated], 0.12)) == 2


# ---------- End-to-end scan ----------


def test_find_duplicates_detects_exact_md5_match(tiny_library: Path) -> None:
    # track3.mp3 and track3_dup.mp3 share content per the fixture
    report = find_duplicates(
        tiny_library,
        DuplicateConfig(use_md5=True, use_chromaprint=False),
    )

    assert len(report.exact_duplicates) == 1
    group = report.exact_duplicates[0]
    assert group.method == "md5"
    filenames = {group.keep.filename, *(d.filename for d in group.duplicates)}
    assert filenames == {"track3.mp3", "track3_dup.mp3"}


def test_find_duplicates_survives_a_file_that_vanishes_mid_scan(tmp_path: Path) -> None:
    """REGRESSION: enumeration and hashing are minutes apart on a real library
    (12k tracks on a syncing Drive folder). One entry disappearing in that window
    raised FileNotFoundError straight out of find_duplicates and lost the WHOLE
    scan — `find_audio_files` and `file_md5` already tolerate a vanished file
    without aborting the whole scan; this should too."""
    lib = tmp_path / "lib"
    lib.mkdir()
    for name in ("a.mp3", "b.mp3", "c.mp3"):
        (lib / name).write_bytes(b"identical-bytes")

    def _sync_removes_c(current: int, total: int, message: str = "") -> None:
        if message == "hash a.mp3":
            (lib / "c.mp3").unlink()

    report = find_duplicates(
        lib,
        DuplicateConfig(use_md5=True, use_chromaprint=False),
        on_progress=_sync_removes_c,
    )

    assert len(report.exact_duplicates) == 1
    group = report.exact_duplicates[0]
    names = {group.keep.filename, *(d.filename for d in group.duplicates)}
    assert names == {"a.mp3", "b.mp3"}      # the survivors are still reported


def _symlink_or_skip(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError) as e:  # Windows without the privilege
        pytest.skip(f"symlinks unavailable on this machine: {e}")


def test_dedupe_ignores_a_symlink_pointing_at_a_scanned_file(tmp_path: Path) -> None:
    """REGRESSION: `Path.is_file()` follows a symlink, so a link named `*.flac`
    was enumerated as a track, hashed to its target's MD5 and joined the same
    group. Every keeper field is identical for a link and its target, so the
    winner came down to path length — and when the link won, the REAL audio was
    the file offered for trash and the library kept a dangling link."""
    lib = tmp_path / "lib"
    (lib / "Deep House Classics").mkdir(parents=True)
    (lib / "Sets").mkdir()
    real = lib / "Deep House Classics" / "track.flac"
    real.write_bytes(b"audio" * 200)
    _symlink_or_skip(lib / "Sets" / "track.flac", real)

    report = find_duplicates(lib, DuplicateConfig(use_md5=True, use_chromaprint=False))

    assert report.exact_duplicates == []        # an alias is not a second copy
    assert report.summary.total_files == 1


def test_dedupe_drops_an_alias_the_scan_enumerated_without_needing_os_symlinks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Portable twin of the scan-level symlink drop — this one RUNS on Windows.

    `_symlink_or_skip` skips without SeCreateSymbolicLinkPrivilege, i.e. on the
    maintainer's own box and on the Windows CI leg, where a skip is
    indistinguishable from a pass. Here the two files are ordinary byte-identical
    copies (so the scan really would group them — asserted first) and only
    `Path.is_symlink` is patched, for the one enumerated entry, to reach
    `_drop_aliases`'s symlink branch on every platform without any OS privilege.
    """
    lib = tmp_path / "lib"
    (lib / "Deep House Classics").mkdir(parents=True)
    (lib / "Sets").mkdir()
    real = lib / "Deep House Classics" / "track.flac"
    real.write_bytes(b"audio" * 200)
    alias = lib / "Sets" / "track.flac"
    alias.write_bytes(real.read_bytes())

    cfg = DuplicateConfig(use_md5=True, use_chromaprint=False)
    # Control: as two plain copies they ARE a duplicate group, so the assertions
    # below can only pass because the alias was dropped.
    control = find_duplicates(lib, cfg)
    assert len(control.exact_duplicates) == 1
    assert control.summary.total_files == 2

    unpatched_is_symlink = Path.is_symlink

    def fake_is_symlink(self: Path) -> bool:
        return True if self == alias else unpatched_is_symlink(self)

    monkeypatch.setattr(Path, "is_symlink", fake_is_symlink)

    report = find_duplicates(lib, cfg)

    assert report.exact_duplicates == []        # an alias is not a second copy
    assert report.summary.total_files == 1      # counted once, not twice


def test_dedupe_counts_a_hard_link_once(tmp_path: Path) -> None:
    """Same shape as the symlink case, and portable: two hard links are ONE file.
    Grouping them advertised space that trashing either cannot recover."""
    import os

    lib = tmp_path / "lib"
    lib.mkdir()
    real = lib / "track.flac"
    real.write_bytes(b"audio" * 200)
    try:
        os.link(real, lib / "alias.flac")
    except (OSError, NotImplementedError) as e:
        pytest.skip(f"hard links unavailable on this filesystem: {e}")

    report = find_duplicates(lib, DuplicateConfig(use_md5=True, use_chromaprint=False))

    assert report.exact_duplicates == []
    assert report.summary.total_files == 1


def test_dedupe_ignores_a_hard_link_even_when_it_would_win_the_keeper_vote(
    tmp_path: Path,
) -> None:
    """Portable twin of the symlink regression above — this one RUNS on Windows.

    Both symlink tests skip without SeCreateSymbolicLinkPrivilege, i.e. on the
    maintainer's own box and on the Windows CI leg, where a skip is
    indistinguishable from a pass. A hard link needs no privilege and reaches
    the same data-loss shape by a different door: two names, one inode, so
    trashing either recovers nothing and the "duplicate" offered for removal is
    the very file the keeper points at.

    The alias here is deliberately given the SHALLOWER, SHORTER path, so
    `choose_keeper` would hand it the win — the assertion below pins that the
    scan-level `_drop_aliases` (st_dev/st_ino) is what keeps the pair from ever
    reaching the vote, not the keeper ordering.

    CHARACTERIZATION, NOT A CONTRACT: the final `keeper.path == str(alias)` is
    NOT a claim that a hard-link alias SHOULD win. It records that it currently
    does, and why that is tolerable — `_path_is_alias` reads `Path.is_symlink`,
    and a hard link is indistinguishable from an ordinary file by path alone, so
    `choose_keeper` has nothing to discriminate on and st_dev/st_ino (the scan's
    check) is the only layer that can. If a future change teaches `choose_keeper`
    to stat for a link count, this assertion is the one to FLIP, not to defend.
    """
    import os

    from vibechek.duplicates import choose_keeper

    lib = tmp_path / "lib"
    (lib / "Deep House Classics").mkdir(parents=True)
    real = lib / "Deep House Classics" / "track.flac"
    real.write_bytes(b"audio" * 200)
    alias = lib / "t.flac"
    try:
        os.link(real, alias)
    except (OSError, NotImplementedError) as e:
        pytest.skip(f"hard links unavailable on this filesystem: {e}")

    report = find_duplicates(lib, DuplicateConfig(use_md5=True, use_chromaprint=False))

    assert report.exact_duplicates == []       # an alias is not a second copy
    assert report.summary.total_files == 1     # counted once, not twice
    assert real.exists() and alias.exists()    # nothing was offered for removal

    # And the reason it matters: assembled by hand, the alias wins the vote today
    # (see CHARACTERIZATION above — recorded, not endorsed).
    keeper, dupes = choose_keeper([_fi(str(real)), _fi(str(alias))])
    assert keeper.path == str(alias)
    assert dupes[0].path == str(real)


def test_choose_keeper_ranks_a_real_file_above_a_symlink(tmp_path: Path) -> None:
    """Defence in depth for a caller that assembled its own group: the alias has
    the shallower, shorter path here, so nothing but the link check saves the
    real file."""
    real = tmp_path / "Deep House Classics" / "track.flac"
    real.parent.mkdir()
    real.write_bytes(b"audio" * 200)
    link = tmp_path / "t.flac"
    _symlink_or_skip(link, real)

    keeper, dupes = choose_keeper([_fi(str(link)), _fi(str(real))])
    assert keeper.path == str(real)
    assert dupes[0].path == str(link)


def test_choose_keeper_ranks_a_real_file_above_an_alias_without_needing_os_symlinks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Portable twin of the keeper test above — this one RUNS on Windows.

    The real-symlink version skips without SeCreateSymbolicLinkPrivilege, so on
    the maintainer's box and the Windows CI leg a regression in the alias term
    reads as a pass. Patching `_path_is_alias` (the seam `choose_keeper` consults)
    exercises that term on every platform with no OS privilege at all.

    The alias is given the shallower, shorter path AND the same size and format,
    so every other term ties or favours it — asserted first, so the win below can
    only come from the alias term.
    """
    from vibechek import duplicates as dd

    real = _fi("/lib/Deep House Classics/track.flac")
    link = _fi("/lib/t.flac")

    # Control: with nothing patched (neither path is a real symlink) the alias
    # wins on folder depth.
    assert choose_keeper([link, real])[0].path == link.path

    monkeypatch.setattr(dd, "_path_is_alias", lambda p: p == link.path)

    keeper, dupes = choose_keeper([link, real])
    assert keeper.path == real.path
    assert dupes[0].path == link.path


def test_find_duplicates_flags_failed_provision(tmp_path: Path, monkeypatch) -> None:
    """When audio fingerprinting is requested but auto-provisioning the tool
    FAILS, the phase no-ops — the report records fpcalc_available=False AND a
    classified plain-user `fpcalc_error` so the GUI banners the real reason
    (with an automatic retry) instead of implying a clean fuzzy pass."""
    from vibechek import duplicates
    from vibechek.fpcalc_provision import FpcalcProvisionError

    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / "a.mp3").write_bytes(b"aaaa")
    (lib / "b.mp3").write_bytes(b"bbbb")

    def boom(on_progress=None):
        raise FpcalcProvisionError(
            "All 1 mirror(s) failed", reason="the download didn't complete (check your connection)",
        )

    monkeypatch.setattr(duplicates, "ensure_fpcalc", boom)
    report = duplicates.find_duplicates(
        lib, DuplicateConfig(use_md5=True, use_chromaprint=True),
    )
    assert report.summary.fpcalc_available is False
    assert report.summary.fpcalc_error == "the download didn't complete (check your connection)"
    assert "chromaprint" not in report.summary.phases_run
    assert report.summary.phases_run == ["md5"]
    # Both new fields survive the wire round-trip (asdict) the RPC uses.
    wire = report.to_dict()["summary"]
    assert wire["fpcalc_available"] is False
    assert wire["fpcalc_error"] == "the download didn't complete (check your connection)"


def test_find_duplicates_flags_skipped_provision(tmp_path: Path, monkeypatch) -> None:
    """When provisioning is SKIPPED (not failed) — e.g. auto-heal off or an
    unsupported platform — ensure_fpcalc returns None; the report still flags
    fpcalc_available=False and surfaces the skip reason from fpcalc_skip_reason."""
    from vibechek import duplicates

    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / "a.mp3").write_bytes(b"aaaa")

    monkeypatch.setattr(duplicates, "ensure_fpcalc", lambda on_progress=None: None)
    monkeypatch.setattr(
        duplicates, "fpcalc_skip_reason", lambda: "automatic setup is turned off",
    )
    report = duplicates.find_duplicates(
        lib, DuplicateConfig(use_md5=True, use_chromaprint=True),
    )
    assert report.summary.fpcalc_available is False
    assert report.summary.fpcalc_error == "automatic setup is turned off"
    assert report.summary.phases_run == ["md5"]


def test_find_duplicates_fpcalc_available_when_not_requested(tmp_path: Path) -> None:
    """fpcalc_available defaults True (no false 'skipped' banner) when the user
    never asked for the fingerprint phase."""
    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / "a.mp3").write_bytes(b"aaaa")
    report = find_duplicates(lib, DuplicateConfig(use_md5=True, use_chromaprint=False))
    assert report.summary.fpcalc_available is True
    assert report.summary.phases_run == ["md5"]
    # Never requested → no error reason surfaced.
    assert report.summary.fpcalc_error is None


def test_duplicate_summary_roundtrip_carries_fpcalc_error() -> None:
    """The new fpcalc_error field survives asdict() (the RPC wire) and defaults
    to None so a healthy report never triggers the failure banner."""
    from vibechek.duplicates import DuplicateReport, DuplicateSummary

    summary = DuplicateSummary(
        fpcalc_available=False,
        fpcalc_error="the download didn't complete (check your connection)",
    )
    wire = DuplicateReport(summary=summary).to_dict()["summary"]
    assert wire["fpcalc_available"] is False
    assert wire["fpcalc_error"] == "the download didn't complete (check your connection)"
    # Default report: no error (no false banner).
    assert DuplicateReport().to_dict()["summary"]["fpcalc_error"] is None


def test_find_duplicates_handle_move_relocates_dupes(tmp_path: Path) -> None:
    # Build two byte-identical files
    a = tmp_path / "lib" / "a.mp3"
    b = tmp_path / "lib" / "b.mp3"
    a.parent.mkdir()
    a.write_bytes(b"same content")
    b.write_bytes(b"same content")

    review = tmp_path / "review"
    config = DuplicateConfig(
        use_md5=True,
        use_chromaprint=False,
        action=DuplicateAction.MOVE.value,
        review_folder=review,
    )

    report = find_duplicates(tmp_path / "lib", config)
    summary = handle_duplicates(report, config)

    assert summary["moved"] == 1
    assert review.exists()
    moved_files = list(review.iterdir())
    assert len(moved_files) == 1
    # Originals: one stays, one was moved
    assert sum(1 for p in (a, b) if p.exists()) == 1


def test_handle_move_requires_review_folder(tmp_path: Path) -> None:
    config = DuplicateConfig(action=DuplicateAction.MOVE.value, review_folder=None)
    with pytest.raises(ValueError):
        handle_duplicates(
            __import__("vibechek.duplicates", fromlist=["DuplicateReport"]).DuplicateReport(),
            config,
        )


# ---------- Chromaprint similarity ----------


def test_fingerprint_similarity_identical_returns_one() -> None:
    """Two identical fingerprints have similarity 1.0."""
    from vibechek.duplicates import fingerprint_similarity
    fp = [0x12345678, 0xABCDEF01, 0xDEADBEEF]
    assert fingerprint_similarity(fp, fp) == 1.0


def test_fingerprint_similarity_complement_returns_zero() -> None:
    """All-bits-flipped fingerprints have similarity 0.0."""
    from vibechek.duplicates import fingerprint_similarity
    a = [0x00000000, 0x00000000]
    b = [0xFFFFFFFF, 0xFFFFFFFF]
    assert fingerprint_similarity(a, b) == 0.0


def test_fingerprint_similarity_partial_match() -> None:
    """One differing bit in 32 → similarity ≈ 31/32."""
    from vibechek.duplicates import fingerprint_similarity
    a = [0x00000000]
    b = [0x00000001]  # 1 bit set
    sim = fingerprint_similarity(a, b)
    assert abs(sim - (31 / 32)) < 1e-9


def test_fingerprint_similarity_handles_different_lengths() -> None:
    """Truncates to the shorter list — no IndexError on misaligned inputs."""
    from vibechek.duplicates import fingerprint_similarity
    a = [0x12345678, 0xABCDEF01]
    b = [0x12345678]
    # Compares only the overlapping prefix (1 int) → identical
    assert fingerprint_similarity(a, b) == 1.0


def test_fingerprint_similarity_empty_returns_zero() -> None:
    from vibechek.duplicates import fingerprint_similarity
    assert fingerprint_similarity([], []) == 0.0
    assert fingerprint_similarity([1], []) == 0.0


def test_audio_fingerprint_raw_parses_fpcalc_output(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """audio_fingerprint_raw returns a list of unsigned 32-bit ints."""
    import subprocess as _subprocess

    from vibechek import duplicates

    fake_stdout = "DURATION=120\nFINGERPRINT=305419896,-559038737,3735928559\n"
    fake = _subprocess.CompletedProcess(["fpcalc"], 0, fake_stdout, "")
    monkeypatch.setattr(duplicates.subprocess, "run", lambda *a, **kw: fake)

    f = tmp_path / "track.mp3"
    f.write_bytes(b"x")
    raw = duplicates.audio_fingerprint_raw(f, "fpcalc")
    assert raw == [305419896, (-559038737) & 0xFFFFFFFF, 3735928559]


def test_chromaprint_threshold_clusters_similar_fingerprints(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """find_duplicates groups files whose raw fingerprints are similar enough.

    Verifies the chromaprint_similarity_threshold field is actually consulted
    — the bug was that the threshold was dead code.
    """
    from vibechek import duplicates
    from vibechek.config import DuplicateConfig

    # Two files: byte-different so MD5 catches nothing, but raw fingerprints
    # differ by only ~3% of bits — should cluster at threshold=0.85, not at 0.99.
    a = tmp_path / "a.mp3"
    b = tmp_path / "b.mp3"
    c = tmp_path / "c.mp3"
    a.write_bytes(b"a-content")
    b.write_bytes(b"b-content")
    c.write_bytes(b"c-content-totally-different")

    # At least `_MIN_ALIGN_OVERLAP` frames: shorter fingerprints are refused
    # outright now (a 1-frame read scores 1.0 against every bucket-mate).
    _pad = [0x0F0F0F0F, 0x33333333, 0x55555555, 0x77777777, 0x99999999]
    fps = {
        str(a): [0x12345678, 0xABCDEF01, 0xDEADBEEF, *_pad],
        str(b): [0x12345678, 0xABCDEF01, 0xDEADBEEE, *_pad],  # 1 bit different
        str(c): [0x00000000, 0x11111111, 0x22222222, *_pad],  # very different
    }

    monkeypatch.setattr(duplicates, "ensure_fpcalc", lambda on_progress=None: "fpcalc")

    def fake_raw(path, _cmd, duration=120):
        return fps.get(str(path))

    monkeypatch.setattr(duplicates, "audio_fingerprint_raw", fake_raw)

    # Threshold 0.85: a + b should cluster; c stands alone.
    cfg = DuplicateConfig(
        use_md5=False,
        use_chromaprint=True,
        chromaprint_similarity_threshold=0.85,
    )
    report = duplicates.find_duplicates(tmp_path, cfg)
    assert len(report.audio_duplicates) == 1
    group = report.audio_duplicates[0]
    all_paths = {group.keep.path, *(d.path for d in group.duplicates)}
    assert all_paths == {str(a), str(b)}

    # Threshold 0.999: a + b differ in 1 bit / 256 bits → 0.9961, below 0.999.
    cfg_strict = DuplicateConfig(
        use_md5=False,
        use_chromaprint=True,
        chromaprint_similarity_threshold=0.999,
    )
    report_strict = duplicates.find_duplicates(tmp_path, cfg_strict)
    assert len(report_strict.audio_duplicates) == 0


def test_fingerprint_similarity_matches_offset_transcode() -> None:
    """A fingerprint shifted by a few frames (encoder delay) still scores high.

    The old index-0-only comparison lined up frame i of one against frame i of
    the other, so even a few-frame shift between two otherwise-identical tracks
    collapsed similarity. Sliding alignment recovers it.
    """
    from vibechek.duplicates import _aligned_similarity, fingerprint_similarity

    base = [(0x11111111 * i) & 0xFFFFFFFF for i in range(1, 41)]
    # `shifted` is `base` delayed by 3 frames (3 junk frames prepended) — the
    # acoustic content is identical, just offset, as a transcode/encoder-delay
    # pair would be.
    junk = [0xDEADBEEF, 0xCAFEBABE, 0x0BADF00D]
    shifted = [*junk, *base]

    # The OLD behaviour (offset-0 only) lines up junk against base → poor score.
    naive = _aligned_similarity(base, shifted)
    assert naive < 0.9
    # The NEW sliding alignment finds the 3-frame offset → perfect overlap.
    aligned = fingerprint_similarity(base, shifted)
    assert aligned == 1.0


def test_chromaprint_buckets_on_multiple_probes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Two transcodes whose FIRST sub-fingerprint differs still get compared.

    The old single-first-subfingerprint bucketing filed them in different
    buckets and never compared them — a missed duplicate. Multi-probe bucketing
    files each track under several leading sub-fingerprints, so agreement on any
    later probe puts them in a shared bucket.
    """
    from vibechek import duplicates
    from vibechek.config import DuplicateConfig

    a = tmp_path / "a.mp3"
    b = tmp_path / "b.mp3"
    a.write_bytes(b"a-content")
    b.write_bytes(b"b-content")

    # Identical from frame 1 onward, but frame 0 differs (as a transcode can
    # perturb the very first frame). They share probe keys at indices >= 1.
    tail = [(0x01020304 * i) & 0xFFFFFFFF for i in range(1, 40)]
    fps = {
        str(a): [0xAAAAAAAA, *tail],
        str(b): [0xBBBBBBBB, *tail],  # different first frame, identical tail
    }

    monkeypatch.setattr(duplicates, "ensure_fpcalc", lambda on_progress=None: "fpcalc")
    monkeypatch.setattr(
        duplicates, "audio_fingerprint_raw",
        lambda path, _cmd, duration=120: fps.get(str(path)),
    )

    cfg = DuplicateConfig(
        use_md5=False,
        use_chromaprint=True,
        chromaprint_similarity_threshold=0.90,
    )
    report = duplicates.find_duplicates(tmp_path, cfg)
    assert len(report.audio_duplicates) == 1
    group = report.audio_duplicates[0]
    all_paths = {group.keep.path, *(d.path for d in group.duplicates)}
    assert all_paths == {str(a), str(b)}


def test_a_one_frame_fingerprint_never_bridges_unrelated_tracks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """REGRESSION: `fingerprint_similarity` scores offset 0 WITHOUT the
    min-overlap guard, and bucket membership is defined by `raw[0]` equality — so
    a one-frame fingerprint (a short stinger, a truncated rip) compared exactly
    the one frame the bucket already guarantees is equal and scored 1.0 against
    every bucket-mate. Greedy single-link clustering then pulled unrelated full
    tracks into one duplicate group through it."""
    from vibechek import duplicates
    from vibechek.config import DuplicateConfig

    riser = tmp_path / "00-riser.flac"
    a = tmp_path / "Artist A - Song One.mp3"
    b = tmp_path / "Artist B - Other Tune.mp3"
    for i, f in enumerate((riser, a, b)):
        f.write_bytes(f"distinct-bytes-{i}".encode())

    shared_first = 0xAAAAAAAA
    tail_a = [(i * 0x01020304) & 0xFFFFFFFF for i in range(1, 20)]
    tail_b = [~x & 0xFFFFFFFF for x in tail_a]      # bit-complement: nothing in common
    fps = {
        str(riser): [shared_first],                 # one frame — the bridge
        str(a): [shared_first, *tail_a],
        str(b): [shared_first, *tail_b],
    }
    monkeypatch.setattr(duplicates, "ensure_fpcalc", lambda on_progress=None: "fpcalc")
    monkeypatch.setattr(
        duplicates, "audio_fingerprint_raw",
        lambda path, _cmd, duration=120: fps.get(str(path)),
    )

    report = duplicates.find_duplicates(
        tmp_path, DuplicateConfig(use_md5=False, use_chromaprint=True))

    assert report.audio_duplicates == []


def test_find_duplicates_similarity_threshold_param_used(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """The `similarity_threshold` parameter is honored when config is left default.

    Contract with the RPC layer: find_duplicates exposes `similarity_threshold`
    and uses it as the match cutoff. When the config carries the default
    threshold, the explicit parameter wins.
    """
    from vibechek import duplicates
    from vibechek.config import DuplicateConfig

    a = tmp_path / "a.mp3"
    b = tmp_path / "b.mp3"
    a.write_bytes(b"a-content")
    b.write_bytes(b"b-content")

    # a + b differ in 1 bit / 256 bits → similarity ≈ 0.9961. (Eight frames, not
    # three: a fingerprint shorter than `_MIN_ALIGN_OVERLAP` is no longer
    # clustered at all.)
    _pad = [0x0F0F0F0F, 0x33333333, 0x55555555, 0x77777777, 0x99999999]
    fps = {
        str(a): [0x12345678, 0xABCDEF01, 0xDEADBEEF, *_pad],
        str(b): [0x12345678, 0xABCDEF01, 0xDEADBEEE, *_pad],
    }
    monkeypatch.setattr(duplicates, "ensure_fpcalc", lambda on_progress=None: "fpcalc")
    monkeypatch.setattr(
        duplicates, "audio_fingerprint_raw",
        lambda path, _cmd, duration=120: fps.get(str(path)),
    )

    # config at the default 0.95 → the explicit param decides the cutoff.
    cfg = DuplicateConfig(use_md5=False, use_chromaprint=True)  # default 0.95

    # Loose explicit threshold → they cluster.
    loose = duplicates.find_duplicates(tmp_path, cfg, similarity_threshold=0.85)
    assert len(loose.audio_duplicates) == 1

    # Strict explicit threshold above their similarity → no cluster.
    strict = duplicates.find_duplicates(tmp_path, cfg, similarity_threshold=0.999)
    assert len(strict.audio_duplicates) == 0


# ---------- Error surface ----------


def test_handle_duplicates_error_messages_surface_to_caller(tmp_path: Path) -> None:
    """The summary must include `error_messages` so the GUI can show them.

    Errors were just a count — the toast said "errors — see report"
    but there was no report. The summary now includes a list of message strings.
    """
    from vibechek import duplicates as dd

    # Build a report referencing files that don't exist on disk → every move
    # attempt fails with "file not found".
    missing_a = tmp_path / "ghost-a.mp3"
    missing_b = tmp_path / "ghost-b.mp3"
    keeper = dd.FileInfo(
        path=str(tmp_path / "keeper.mp3"),
        filename="keeper.mp3", size_bytes=1, size_mb=0.001,
    )
    group = dd.DuplicateGroup(
        method="md5",
        key="deadbeef",
        keep=keeper,
        duplicates=[
            dd.FileInfo(path=str(missing_a), filename="ghost-a.mp3",
                        size_bytes=1, size_mb=0.001),
            dd.FileInfo(path=str(missing_b), filename="ghost-b.mp3",
                        size_bytes=1, size_mb=0.001),
        ],
        recoverable_mb=0.002,
    )
    report = dd.DuplicateReport(exact_duplicates=[group])
    review = tmp_path / "review"
    cfg = DuplicateConfig(
        action=DuplicateAction.MOVE.value, review_folder=review,
    )
    summary = dd.handle_duplicates(report, cfg)

    assert summary["errors"] == 2
    assert "error_messages" in summary
    assert len(summary["error_messages"]) == 2
    # Each message names the missing file path so the user has actionable info.
    assert any("ghost-a.mp3" in m for m in summary["error_messages"])
    assert any("ghost-b.mp3" in m for m in summary["error_messages"])
    # All "not found" since the source paths didn't exist
    assert all("not found" in m for m in summary["error_messages"])


def test_send2trash_is_a_real_dependency() -> None:
    """The GUI ships a Trash action (DuplicatesView -> handle_duplicates
    action='trash'); its backend late-imports send2trash. That import failing
    at runtime means a shipped button that always errors — which happened once
    because send2trash wasn't declared anywhere. CI installs [dev] on top of
    the base deps, so this import failing here = the dependency regressed."""
    import send2trash  # noqa: F401

    pyproject = (Path(__file__).parent.parent / "pyproject.toml").read_text(
        encoding="utf-8"
    )
    try:
        import tomllib  # stdlib only on Python 3.11+; CI also runs 3.10
    except ModuleNotFoundError:
        import re

        assert re.search(r'^\s*"send2trash', pyproject, re.MULTILINE), (
            "send2trash must be in [project.dependencies]"
        )
    else:
        deps = tomllib.loads(pyproject)["project"]["dependencies"]
        assert any(d.startswith("send2trash") for d in deps), (
            "send2trash must be in [project.dependencies] — the GUI Trash "
            "action needs it at runtime, not just in the dev venv"
        )


def test_handle_duplicates_trash_action(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The permanent-removal branch, previously entirely untested: keeper
    guard holds, dupes go to send2trash, the manifest journal records them,
    summary counts deleted, and a missing file is an error not a crash."""
    from vibechek import journal as journal_mod
    from vibechek.duplicates import DuplicateGroup, DuplicateReport

    monkeypatch.setattr(journal_mod, "JOURNALS_DIR", tmp_path / "journals")

    for name in ("a.mp3", "b.mp3", "c.mp3"):
        (tmp_path / name).write_bytes(b"x" * 1000)
    a, b, c = (_fi(str(tmp_path / n)) for n in ("a.mp3", "b.mp3", "c.mp3"))
    gone = _fi(str(tmp_path / "gone.mp3"))  # never created on disk
    report = DuplicateReport()
    # b is a DUPLICATE in group 1 but the KEEPER of group 2 — the keeper guard
    # must protect it from trashing exactly as it protects it from moving.
    report.audio_duplicates = [
        DuplicateGroup(method="chromaprint", key="g1", keep=a, duplicates=[b], recoverable_mb=0.0),
        DuplicateGroup(method="chromaprint", key="g2", keep=b, duplicates=[c, gone], recoverable_mb=0.0),
    ]

    trashed: list[str] = []
    import send2trash as send2trash_mod
    monkeypatch.setattr(send2trash_mod, "send2trash", lambda p: trashed.append(p))

    cfg = DuplicateConfig(action="trash", use_md5=False, use_chromaprint=True)
    summary = handle_duplicates(report, cfg)

    assert trashed == [str(tmp_path / "c.mp3")]
    assert summary["deleted"] == 1
    assert summary["errors"] == 1  # the missing file
    assert any("gone.mp3" in m for m in summary["error_messages"])
    # Keepers untouched on disk.
    assert (tmp_path / "a.mp3").exists()
    assert (tmp_path / "b.mp3").exists()
    # The transparency manifest exists and records the trashed file.
    jp = summary.get("journal_path")
    assert jp, "trash must write a manifest journal"
    text = Path(jp).read_text(encoding="utf-8")
    assert "c.mp3" in text and "trash" in text


# ---------- Per-file results + journal honesty ----------


def _report_with_one_dupe(tmp_path: Path):
    """A one-group report over two real files: keeper `a.mp3`, dupe `b.mp3`."""
    from vibechek import duplicates as dd

    keep = tmp_path / "a.mp3"
    dupe = tmp_path / "b.mp3"
    keep.write_bytes(b"same bytes")
    dupe.write_bytes(b"same bytes")
    group = dd.DuplicateGroup(
        method="md5",
        key="deadbeef",
        keep=dd.FileInfo(path=str(keep), filename="a.mp3", size_bytes=10, size_mb=0.0),
        duplicates=[
            dd.FileInfo(path=str(dupe), filename="b.mp3", size_bytes=10, size_mb=0.0),
        ],
        recoverable_mb=0.0,
    )
    return dd.DuplicateReport(exact_duplicates=[group]), keep, dupe


def test_handle_duplicates_move_reports_moved_pairs(tmp_path: Path) -> None:
    """MOVE returns `moved_pairs` the way organize does: [[src, dst], ...].

    Counts alone left the RPC layer unable to rewrite the saved analysis, so a
    moved duplicate kept its pre-move path and dropped out of tagging, organize
    and the conflict queue.
    """
    from vibechek import duplicates as dd

    report, _keep, dupe = _report_with_one_dupe(tmp_path)
    review = tmp_path / "review"
    cfg = DuplicateConfig(action=DuplicateAction.MOVE.value, review_folder=review)

    summary = dd.handle_duplicates(report, cfg)

    assert summary["moved"] == 1
    assert summary["deleted_paths"] == []
    assert len(summary["moved_pairs"]) == 1
    src, dst = summary["moved_pairs"][0]
    # src is the caller's spelling; dst is where the file really landed.
    assert src == str(dupe)
    assert Path(dst).exists() and Path(dst).parent == review
    assert not dupe.exists()


def test_handle_duplicates_move_omits_files_that_errored(tmp_path: Path) -> None:
    """A file that failed to move is NOT in `moved_pairs`.

    It is still on disk at its old path — listing it would make the RPC layer
    rewrite a good row into a path that holds nothing.
    """
    from vibechek import duplicates as dd

    report, _keep, dupe = _report_with_one_dupe(tmp_path)
    ghost = dd.FileInfo(
        path=str(tmp_path / "gone.mp3"), filename="gone.mp3",
        size_bytes=10, size_mb=0.0,
    )
    report.exact_duplicates[0].duplicates.append(ghost)
    cfg = DuplicateConfig(
        action=DuplicateAction.MOVE.value, review_folder=tmp_path / "review",
    )

    summary = dd.handle_duplicates(report, cfg)

    assert summary["errors"] == 1
    assert [p[0] for p in summary["moved_pairs"]] == [str(dupe)]


def test_handle_duplicates_trash_reports_deleted_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TRASH returns `deleted_paths` — only the files really sent to the bin."""
    from vibechek import duplicates as dd

    report, _keep, dupe = _report_with_one_dupe(tmp_path)
    ghost = dd.FileInfo(
        path=str(tmp_path / "gone.mp3"), filename="gone.mp3",
        size_bytes=10, size_mb=0.0,
    )
    report.exact_duplicates[0].duplicates.append(ghost)

    import send2trash as send2trash_mod
    monkeypatch.setattr(send2trash_mod, "send2trash", lambda p: None)

    cfg = DuplicateConfig(action=DuplicateAction.TRASH.value)
    summary = dd.handle_duplicates(report, cfg)

    assert summary["deleted"] == 1
    assert summary["errors"] == 1          # the ghost
    assert summary["deleted_paths"] == [str(dupe)]
    assert summary["moved_pairs"] == []


def test_report_action_still_carries_the_per_file_keys(tmp_path: Path) -> None:
    """The `report` action touches nothing, but the shape stays stable — the
    consumers read these keys unconditionally."""
    from vibechek import duplicates as dd

    report, _keep, _dupe = _report_with_one_dupe(tmp_path)
    summary = dd.handle_duplicates(report, DuplicateConfig(action="report"))

    assert summary["moved_pairs"] == []
    assert summary["deleted_paths"] == []


def test_handle_duplicates_flags_an_incomplete_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A move the journal could not record makes `journal_incomplete` True.

    Reverting such a journal reports a clean success while the unrecorded files
    stay where the dedupe put them — the GUI must be able to warn first
    (mirrors OrganizeStats.journal_incomplete).
    """
    from vibechek import duplicates as dd
    from vibechek import journal as _journal

    report, _keep, _dupe = _report_with_one_dupe(tmp_path)
    real_start = _journal.start_journal

    def _start_then_fill_the_disk(kind, root=None):
        j = real_start(kind, root=root)
        original_write = j._fh.write

        def _write(s):
            if '"move"' in s:
                raise OSError(28, "No space left on device")
            return original_write(s)

        j._fh.write = _write
        return j

    monkeypatch.setattr(_journal, "start_journal", _start_then_fill_the_disk)
    summary = dd.handle_duplicates(
        report,
        DuplicateConfig(
            action=DuplicateAction.MOVE.value, review_folder=tmp_path / "review",
        ),
    )

    assert summary["moved"] == 1           # the move itself succeeded
    assert summary["journal_incomplete"] is True


def test_a_healthy_dedupe_does_not_claim_an_incomplete_journal(
    tmp_path: Path,
) -> None:
    from vibechek import duplicates as dd

    report, _keep, _dupe = _report_with_one_dupe(tmp_path)
    summary = dd.handle_duplicates(
        report,
        DuplicateConfig(
            action=DuplicateAction.MOVE.value, review_folder=tmp_path / "review",
        ),
    )

    assert summary["journal_path"] is not None
    assert summary["journal_incomplete"] is False


def test_cancel_after_the_loops_still_stops_the_scan(tmp_path: Path) -> None:
    """A cancel that lands after the per-file loops must not yield a report.

    With both phases off (or a cancel arriving between the last file and the
    return) the scan used to hand back a completed-looking, near-duplicate-blind
    report — indistinguishable from a clean library, and the caller would act
    on it.
    """
    from vibechek import cancellation
    from vibechek import duplicates as dd

    (tmp_path / "a.mp3").write_bytes(b"x")

    cancellation.begin("dedupe")
    try:
        cancellation.cancel()
        with pytest.raises(cancellation.CancelledError):
            dd.find_duplicates(
                tmp_path,
                DuplicateConfig(use_md5=False, use_chromaprint=False),
            )
    finally:
        cancellation.end()
