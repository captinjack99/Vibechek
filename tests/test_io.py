"""Tests for vibechek.io.atomic_write_json.

The whole point of this module is crash-safety, so the tests deliberately
simulate failure during the write and verify the destination file is
preserved (or absent) rather than left half-written.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import pytest

from vibechek.io import atomic_write_json


def test_writes_json_payload(tmp_path: Path) -> None:
    target = tmp_path / "out.json"
    atomic_write_json(target, {"hello": "world", "count": 7})
    raw = json.loads(target.read_text(encoding="utf-8"))
    assert raw == {"hello": "world", "count": 7}


def test_removes_partial_file_on_success(tmp_path: Path) -> None:
    target = tmp_path / "out.json"
    atomic_write_json(target, {"x": 1})
    # No `.partial` should linger after a successful write.
    assert not (tmp_path / "out.json.partial").exists()
    assert target.exists()


def test_overwrites_existing_file(tmp_path: Path) -> None:
    target = tmp_path / "out.json"
    target.write_text('{"old": true}', encoding="utf-8")
    atomic_write_json(target, {"new": True})
    assert json.loads(target.read_text(encoding="utf-8")) == {"new": True}


def test_preserves_existing_file_when_write_fails(tmp_path: Path) -> None:
    """The whole point of atomic_write_json: a mid-write crash MUST NOT
    leave the destination file truncated. The original content stays put."""
    target = tmp_path / "out.json"
    target.write_text('{"good": "data"}', encoding="utf-8")

    # Force the rename to fail. The partial-file write succeeds; then we
    # die at Path.replace(). The destination should still have its original
    # contents.
    with mock.patch.object(Path, "replace", side_effect=OSError("simulated")):
        with pytest.raises(OSError, match="simulated"):
            atomic_write_json(target, {"new": "data"})

    # Destination still has original content
    assert json.loads(target.read_text(encoding="utf-8")) == {"good": "data"}
    # Partial cleaned up
    assert not (tmp_path / "out.json.partial").exists()


def test_no_partial_left_after_serialization_failure(tmp_path: Path) -> None:
    """If json.dumps itself raises, we shouldn't create a partial file at all."""
    target = tmp_path / "out.json"

    class Unjsonable:
        pass

    with pytest.raises(TypeError):
        atomic_write_json(target, {"bad": Unjsonable()})

    assert not target.exists()
    assert not (tmp_path / "out.json.partial").exists()


def test_supports_path_objects_in_payload_via_caller_serialization(tmp_path: Path) -> None:
    """We don't auto-serialize Path — caller must. This just confirms strings
    round-trip cleanly (the common case after `_to_jsonable`).
    """
    target = tmp_path / "out.json"
    atomic_write_json(target, {"path": str(tmp_path / "x.mp3")})
    assert json.loads(target.read_text(encoding="utf-8"))["path"].endswith("x.mp3")


def test_sort_keys_forwarded(tmp_path: Path) -> None:
    target = tmp_path / "out.json"
    atomic_write_json(target, {"b": 1, "a": 2}, sort_keys=True)
    raw = target.read_text(encoding="utf-8")
    # "a" appears before "b" in sorted output
    assert raw.index('"a"') < raw.index('"b"')


def test_ensure_ascii_false_preserves_unicode(tmp_path: Path) -> None:
    """The 32 MB analysis JSON contains track titles with non-ASCII chars
    (Japanese, accented Latin, emoji). ensure_ascii=False keeps them readable.
    """
    target = tmp_path / "out.json"
    atomic_function_data = {"title": "Café — Naïve résumé 日本語"}
    atomic_write_json(target, atomic_function_data, ensure_ascii=False)
    raw = target.read_text(encoding="utf-8")
    assert "Café" in raw
    assert "日本語" in raw


def test_partial_uses_sibling_path_for_atomic_rename(tmp_path: Path) -> None:
    """The partial file MUST live on the same volume as the destination so
    the final rename is atomic. We can't directly test atomicity on a
    cross-process kill, but we can verify the partial path is a sibling.
    """
    target = tmp_path / "subdir" / "out.json"
    target.parent.mkdir()
    # Capture what `replace` was called with by patching it on Path.
    captured = {}
    original_replace = Path.replace

    def spy_replace(self, dst):
        captured["src"] = self
        captured["dst"] = dst
        return original_replace(self, dst)

    with mock.patch.object(Path, "replace", spy_replace):
        atomic_write_json(target, {"k": "v"})

    assert captured["src"].parent == target.parent
    assert captured["src"].name == "out.json.partial"
