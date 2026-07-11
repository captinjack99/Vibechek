"""Tests for the model-download mirror fallback path.

`_download_from_mirrors` walks a tuple of base URLs, trying each in turn so
a single domain outage doesn't permanently break installs. We mock
`_download_with_progress` to control success/failure per URL.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest


def test_download_from_mirrors_uses_first_url_when_it_works(tmp_path: Path) -> None:
    from vibechek import model_download as analyzer

    dest = tmp_path / "model.pb"
    calls: list[str] = []

    def fake_download(url, dest_path, label, on_progress=None, **kwargs):
        calls.append(url)
        dest_path.write_bytes(b"x" * 200_000)  # valid-size .pb file

    with patch.object(analyzer, "_download_with_progress", side_effect=fake_download):
        analyzer._download_from_mirrors(
            ["https://primary/foo.pb", "https://mirror/foo.pb"],
            dest, "model.pb",
        )

    assert calls == ["https://primary/foo.pb"], "second mirror shouldn't be tried"
    assert dest.exists()


def test_download_from_mirrors_falls_through_on_first_failure(tmp_path: Path) -> None:
    """The whole point: when primary is down, fall to mirror."""
    from vibechek import model_download as analyzer

    dest = tmp_path / "model.pb"
    calls: list[str] = []

    def fake_download(url, dest_path, label, on_progress=None, **kwargs):
        calls.append(url)
        if "primary" in url:
            raise RuntimeError("primary mirror returned 503")
        dest_path.write_bytes(b"x" * 200_000)

    with patch.object(analyzer, "_download_with_progress", side_effect=fake_download):
        analyzer._download_from_mirrors(
            ["https://primary/foo.pb", "https://mirror/foo.pb"],
            dest, "model.pb",
        )

    assert calls == ["https://primary/foo.pb", "https://mirror/foo.pb"]
    assert dest.exists()


def test_download_from_mirrors_raises_when_all_mirrors_fail(tmp_path: Path) -> None:
    from vibechek import model_download as analyzer

    dest = tmp_path / "model.pb"

    def fake_download(url, dest_path, label, on_progress=None, **kwargs):
        raise RuntimeError(f"{url} returned 500")

    with patch.object(analyzer, "_download_with_progress", side_effect=fake_download):
        with pytest.raises(RuntimeError, match="All 2 mirror"):
            analyzer._download_from_mirrors(
                ["https://primary/foo.pb", "https://mirror/foo.pb"],
                dest, "model.pb",
            )


def test_download_from_mirrors_rejects_empty_url_list(tmp_path: Path) -> None:
    from vibechek import model_download as analyzer

    with pytest.raises(ValueError, match="No mirror URLs"):
        analyzer._download_from_mirrors([], tmp_path / "x.pb", "x.pb")


def test_env_var_override_replaces_default_mirrors(monkeypatch: pytest.MonkeyPatch) -> None:
    """VIBECHEK_MODELS_URL replaces the entire default tuple (no mixing)."""
    monkeypatch.setenv("VIBECHEK_MODELS_URL", "https://my-mirror.example/models")
    # Re-import to pick up the env var. The mirror tuple is computed at import
    # in vibechek.model_download now (analyzer re-exports it), so reload THAT.
    import importlib

    import vibechek.model_download
    importlib.reload(vibechek.model_download)

    try:
        assert vibechek.model_download.MODEL_BASE_URLS == ("https://my-mirror.example/models",)
        assert vibechek.model_download.MODEL_BASE_URL == "https://my-mirror.example/models"
    finally:
        # Restore module state for other tests — re-reload without the env var
        monkeypatch.delenv("VIBECHEK_MODELS_URL", raising=False)
        importlib.reload(vibechek.model_download)


def test_no_env_var_uses_default_mirror_tuple() -> None:
    """Default config trusts UPF first, then the GitHub Release mirror."""
    from vibechek import model_download as analyzer

    assert "essentia.upf.edu" in analyzer.MODEL_BASE_URLS[0]
    assert len(analyzer.MODEL_BASE_URLS) >= 2, \
        "should have a fallback mirror after the primary"


# ---------------------------------------------------------------------------
# WP8: the failure summary must not blame the network for disk-full / checksum
# ---------------------------------------------------------------------------


def test_download_failure_hint_disk_full(tmp_path: Path) -> None:
    """ENOSPC gets a 'free disk space' hint (retry is futile until freed), not
    the old blanket 'Check your network and retry'."""
    from vibechek import model_download as md

    errors = ["genre.pb: [Errno 28] No space left on device: '/x'"]
    hint = md._download_failure_hint(errors, tmp_path)
    assert "disk space" in hint.lower()
    assert str(tmp_path) in hint
    assert "network" not in hint.lower()


def test_download_failure_hint_checksum(tmp_path: Path) -> None:
    """A SHA256 mismatch says 're-fetch', not 'check your network'."""
    from vibechek import model_download as md

    errors = ["genre.pb: Model file genre.pb failed SHA256 check: expected abc…, got def…."]
    hint = md._download_failure_hint(errors, tmp_path)
    assert "corrupted" in hint.lower() and "re-fetch" in hint.lower()
    assert "network" not in hint.lower()


def test_download_failure_hint_network_default(tmp_path: Path) -> None:
    """A genuine connectivity failure keeps the network hint."""
    from vibechek import model_download as md

    errors = ["genre.pb: <urlopen error [Errno -3] Temporary failure in name resolution>"]
    hint = md._download_failure_hint(errors, tmp_path)
    assert "network" in hint.lower()


def test_download_failure_hint_disk_takes_precedence(tmp_path: Path) -> None:
    """Mixed failures: disk-full wins (retrying is pointless until space frees)."""
    from vibechek import model_download as md

    errors = [
        "a.pb: some network blip",
        "b.pb: [Errno 28] No space left on device",
        "c.pb: failed SHA256 check",
    ]
    hint = md._download_failure_hint(errors, tmp_path)
    assert "disk space" in hint.lower()


# ---------------------------------------------------------------------------
# SHA256 content-hash pinning (`verify_model_sha256` / `MODEL_SHA256`)
# ---------------------------------------------------------------------------


def test_verify_model_sha256_noop_when_expected_none(tmp_path: Path) -> None:
    """Empty MODEL_SHA256 table is the documented default; verify must
    return cleanly so callers can wire it in unconditionally."""
    from vibechek import model_download as analyzer

    bogus = tmp_path / "anything.pb"
    bogus.write_bytes(b"contents")
    # Should not raise, should not read the file unnecessarily.
    analyzer.verify_model_sha256(bogus, None)
    analyzer.verify_model_sha256(bogus, "")


def test_verify_model_sha256_accepts_correct_hash(tmp_path: Path) -> None:
    import hashlib

    from vibechek import model_download as analyzer

    target = tmp_path / "model.pb"
    body = b"fake model weights payload"
    target.write_bytes(body)
    expected = hashlib.sha256(body).hexdigest()
    # No raise.
    analyzer.verify_model_sha256(target, expected)


def test_verify_model_sha256_rejects_mismatch(tmp_path: Path) -> None:
    from vibechek import model_download as analyzer

    target = tmp_path / "model.pb"
    target.write_bytes(b"these bytes do not match the expected hash")
    with pytest.raises(RuntimeError, match="SHA256"):
        analyzer.verify_model_sha256(target, "0" * 64)


def test_verify_model_sha256_error_mentions_remediation(tmp_path: Path) -> None:
    """The error message should tell the user how to recover."""
    from vibechek import model_download as analyzer

    target = tmp_path / "model.pb"
    target.write_bytes(b"bad")
    with pytest.raises(RuntimeError, match="verify-models"):
        analyzer.verify_model_sha256(target, "f" * 64)


def test_model_sha256_table_is_dict_of_dicts() -> None:
    """Schema check: MODEL_SHA256[name][suffix] -> hex string. Empty by
    default until the release-build pass populates it; this test just
    locks the shape so a future PR can't accidentally change it to a flat
    dict."""
    from vibechek import model_download as analyzer

    assert isinstance(analyzer.MODEL_SHA256, dict)
    for name, suffixes in analyzer.MODEL_SHA256.items():
        assert isinstance(suffixes, dict), f"{name} entry must be dict"
        for suffix, digest in suffixes.items():
            assert suffix in ("pb", "json"), f"unknown suffix {suffix!r}"
            assert isinstance(digest, str) and len(digest) == 64, \
                f"{name}.{suffix} digest must be 64-char hex"
