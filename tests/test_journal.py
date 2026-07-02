"""Tests for vibechek.journal — the organize/dedupe undo journal.

Covers the append-only writer, crash-recovery parsing (truncated last line),
the list view, and the revert logic including the don't-clobber and
trash-not-revertible guards.
"""

from __future__ import annotations

import pytest

from vibechek import journal


@pytest.fixture(autouse=True)
def _isolate_journals(tmp_path, monkeypatch):
    """Point JOURNALS_DIR at a temp dir so tests don't touch real user data."""
    jdir = tmp_path / "journals"
    monkeypatch.setattr(journal, "JOURNALS_DIR", jdir)
    return jdir


def test_writer_records_moves_and_completes(_isolate_journals) -> None:
    j = journal.start_journal(journal.KIND_ORGANIZE, root="/lib")
    j.record_move("/lib/a.mp3", "/lib/House/a.mp3")
    j.record_move("/lib/b.mp3", "/lib/Techno/b.mp3")
    j.close()
    assert j.entries == 2
    assert j.path.exists()

    header, entries = journal._read_journal(j.path)
    assert header["kind"] == "organize"
    assert header["root"] == "/lib"
    assert [e["action"] for e in entries] == ["move", "move"]
    assert entries[0]["src"] == "/lib/a.mp3"
    assert entries[0]["dst"] == "/lib/House/a.mp3"


def test_read_journal_skips_truncated_last_line(_isolate_journals) -> None:
    """A crash mid-write leaves a partial final line — the reader must skip it,
    not raise, so the recovered prefix is still usable."""
    j = journal.start_journal(journal.KIND_DEDUPE_MOVE, root="/review")
    j.record_move("/lib/dupe.mp3", "/review/dupe.mp3")
    j.close()
    # Simulate a crash: append a truncated JSON line.
    with open(j.path, "a", encoding="utf-8") as f:
        f.write('{"action": "move", "src": "/lib/x.mp3", "ds')  # no newline, cut off
    header, entries = journal._read_journal(j.path)
    assert header["kind"] == "dedupe_move"
    assert len(entries) == 1  # the complete entry survives, partial skipped


def test_list_journals_tallies_counts(_isolate_journals) -> None:
    j = journal.start_journal(journal.KIND_ORGANIZE, root="/lib")
    j.record_move("/lib/a.mp3", "/lib/House/a.mp3")
    j.close()
    jt = journal.start_journal(journal.KIND_DEDUPE_TRASH, root=None)
    jt.record_trash("/lib/junk.mp3")
    jt.record_trash("/lib/junk2.mp3")
    jt.close()

    listed = journal.list_journals()
    by_kind = {item["kind"]: item for item in listed}
    assert by_kind["organize"]["move_count"] == 1
    assert by_kind["organize"]["trash_count"] == 0
    assert by_kind["dedupe_trash"]["trash_count"] == 2


def test_revert_moves_files_back(_isolate_journals, tmp_path) -> None:
    lib = tmp_path / "lib"
    lib.mkdir()
    src = lib / "a.mp3"
    src.write_bytes(b"audio")
    dst = lib / "House" / "a.mp3"
    dst.parent.mkdir(parents=True)

    # Simulate the organize move, recording it.
    j = journal.start_journal(journal.KIND_ORGANIZE, root=str(lib))
    src.rename(dst)
    j.record_move(src, dst)
    j.close()
    assert not src.exists() and dst.exists()

    summary = journal.revert_journal(j.path)
    assert summary["reverted"] == 1
    assert summary["errors"] == 0
    assert src.exists() and not dst.exists()
    assert src.read_bytes() == b"audio"
    # (current, restored) pairs let the GUI rewrite its in-memory track paths
    # back — without them every post-undo Preview/tag-apply pointed at the
    # dead destination path until a manual re-scan.
    assert summary["reverted_pairs"] == [(str(dst), str(src))]


def test_revert_skips_when_origin_occupied(_isolate_journals, tmp_path) -> None:
    """Don't clobber: if something already sits at the original path, leave the
    moved file where it is and count it skipped."""
    lib = tmp_path / "lib"
    lib.mkdir()
    src = lib / "a.mp3"
    dst = lib / "House" / "a.mp3"
    dst.parent.mkdir(parents=True)
    dst.write_bytes(b"moved")
    src.write_bytes(b"NEW FILE at original path")  # origin now occupied

    j = journal.start_journal(journal.KIND_ORGANIZE, root=str(lib))
    j.record_move(src, dst)
    j.close()

    summary = journal.revert_journal(j.path)
    assert summary["reverted"] == 0
    assert summary["skipped"] == 1
    # Neither file clobbered.
    assert src.read_bytes() == b"NEW FILE at original path"
    assert dst.read_bytes() == b"moved"
    # A skipped move must NOT claim a path rewrite — the file didn't move.
    assert summary["reverted_pairs"] == []


def test_revert_skips_missing_destination(_isolate_journals, tmp_path) -> None:
    lib = tmp_path / "lib"
    lib.mkdir()
    j = journal.start_journal(journal.KIND_ORGANIZE, root=str(lib))
    j.record_move(lib / "a.mp3", lib / "House" / "gone.mp3")  # dst never created
    j.close()
    summary = journal.revert_journal(j.path)
    assert summary["reverted"] == 0
    assert summary["skipped"] == 1


def test_revert_reports_trash_as_not_revertible(_isolate_journals, tmp_path) -> None:
    j = journal.start_journal(journal.KIND_DEDUPE_TRASH, root=None)
    j.record_trash(tmp_path / "junk.mp3")
    j.close()
    summary = journal.revert_journal(j.path)
    assert summary["trashed_not_reverted"] == 1
    assert summary["reverted"] == 0


def test_revert_unwinds_newest_first(_isolate_journals, tmp_path) -> None:
    """Nested moves unwind newest-first so each destination parent is valid."""
    lib = tmp_path / "lib"
    lib.mkdir()
    a_src = lib / "a.mp3"
    a_src.write_bytes(b"a")
    a_dst = lib / "House" / "Deep House" / "a.mp3"
    a_dst.parent.mkdir(parents=True)
    a_src.rename(a_dst)

    j = journal.start_journal(journal.KIND_ORGANIZE, root=str(lib))
    j.record_move(a_src, a_dst)
    j.close()

    summary = journal.revert_journal(j.path)
    assert summary["reverted"] == 1
    assert a_src.exists()


def test_start_journal_degrades_to_noop_on_unwritable_dir(tmp_path, monkeypatch) -> None:
    """If the journals dir can't be created, start_journal returns a no-op
    writer rather than raising — the operation it records still runs."""
    # Point at a path under a FILE (mkdir will fail).
    blocker = tmp_path / "blocker"
    blocker.write_text("not a dir")
    monkeypatch.setattr(journal, "JOURNALS_DIR", blocker / "journals")
    j = journal.start_journal(journal.KIND_ORGANIZE)
    j.record_move("/x", "/y")  # must not raise
    j.close()
    assert j.entries == 0  # nothing recorded, but no exception
