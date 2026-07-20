"""Tests for vibechek.fpcalc_provision — zero-setup fpcalc auto-provisioning.

Everything is mocked at the module seams (download / verify / platform / staged
resolution / autoheal). No test performs a real network download.
"""

from __future__ import annotations

import io
import os
import re
import zipfile
from pathlib import Path

import pytest

from vibechek import config
from vibechek import fpcalc_provision as fp

_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def _fake_zip(member_basename: str, payload: bytes) -> bytes:
    """Build an in-memory zip mirroring the official layout: the binary nested
    under a single top-level release directory."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(f"chromaprint-fpcalc-1.5.1-test/{member_basename}", payload)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Pins — provenance/shape guard
# ---------------------------------------------------------------------------


def test_platform_pins_are_well_formed() -> None:
    """All three platform pins are 64-hex SHA256s over real release assets, with
    a sane archive kind + binary member. Guards a copy-paste/typo regression in
    the hardcoded pins without needing the network."""
    assets = {
        "windows": fp._WINDOWS,
        "macos": fp._MACOS,
        "linux": fp._LINUX,
    }
    seen: set[str] = set()
    for label, a in assets.items():
        assert _HEX64.match(a.sha256), f"{label}: sha256 must be 64 lowercase hex"
        assert a.sha256 not in seen, f"{label}: duplicate pin (wrong copy-paste?)"
        seen.add(a.sha256)
        assert a.archive in {"zip", "targz"}, label
        assert a.member in {"fpcalc", "fpcalc.exe"}, label
        assert a.asset.startswith("chromaprint-fpcalc-1.5.1-"), label
        assert a.asset.endswith((".zip", ".tar.gz")), label
    assert len(seen) == 3, "three distinct platform pins expected"


# ---------------------------------------------------------------------------
# Resolution order: PATH > staged > provision
# ---------------------------------------------------------------------------


def test_resolve_prefers_path_over_staged(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(fp, "find_fpcalc", lambda: "SYS/fpcalc")
    # Stage a decoy — PATH must still win.
    staged = fp.staged_fpcalc_path()
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_bytes(b"decoy")
    os.chmod(staged, 0o755)
    assert fp.resolve_fpcalc() == "SYS/fpcalc"


def test_resolve_uses_staged_when_no_path(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(fp, "find_fpcalc", lambda: None)
    staged = fp.staged_fpcalc_path()
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_bytes(b"staged")
    os.chmod(staged, 0o755)
    assert fp.resolve_fpcalc() == str(staged)


def test_resolve_none_when_absent(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(fp, "find_fpcalc", lambda: None)
    assert fp.resolve_fpcalc() is None


def test_ensure_returns_path_without_provisioning(monkeypatch, tmp_path: Path) -> None:
    """When fpcalc resolves from PATH, ensure_fpcalc must NOT touch the network."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(fp, "find_fpcalc", lambda: "SYS/fpcalc")

    def no_download(*a, **k):
        raise AssertionError("must not download when PATH already resolves")

    monkeypatch.setattr(fp, "_download_from_mirrors", no_download)
    assert fp.ensure_fpcalc() == "SYS/fpcalc"


# ---------------------------------------------------------------------------
# Provision success
# ---------------------------------------------------------------------------


def test_ensure_provisions_stages_and_returns_path(monkeypatch, tmp_path: Path) -> None:
    """Both PATH and staged miss → download + verify + extract, staging the
    binary and returning its path. Progress is announced in plain-user words."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(fp, "find_fpcalc", lambda: None)
    monkeypatch.setattr(fp, "_autoheal_disabled", lambda: False)

    binname = fp._binary_name()
    asset = fp._PlatformAsset(
        asset="chromaprint-fpcalc-1.5.1-test.zip",
        sha256="0" * 64,
        archive="zip",
        member=binname,
    )
    monkeypatch.setattr(fp, "_platform_asset", lambda: asset)

    payload = _fake_zip(binname, b"#!/fake-fpcalc\n")

    def fake_download(urls, dest, label, on_progress=None):
        # Simulate a successful mirror fetch into the archive dest.
        Path(dest).write_bytes(payload)

    monkeypatch.setattr(fp, "_download_from_mirrors", fake_download)
    # Skip the real checksum (payload is a synthetic archive, not the pinned one).
    monkeypatch.setattr(fp, "verify_model_sha256", lambda p, e: None)

    messages: list[str] = []
    path = fp.ensure_fpcalc(on_progress=lambda c, t, m: messages.append(m))

    assert path == str(fp.staged_fpcalc_path())
    assert fp.staged_fpcalc_path().read_bytes() == b"#!/fake-fpcalc\n"
    # Scratch archive cleaned up — only the extracted binary remains.
    assert not (fp.tools_dir() / asset.asset).exists()
    # Language rule: user-visible strings say "audio fingerprint tool", never
    # the bare binary name.
    assert any("audio fingerprint tool" in m for m in messages)
    assert all("fpcalc" not in m.lower() for m in messages)


# ---------------------------------------------------------------------------
# Provision failure → classified reason
# ---------------------------------------------------------------------------


def _arm_missing(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(fp, "find_fpcalc", lambda: None)
    monkeypatch.setattr(fp, "_autoheal_disabled", lambda: False)
    asset = fp._PlatformAsset(
        asset="x.zip", sha256="0" * 64, archive="zip", member=fp._binary_name(),
    )
    monkeypatch.setattr(fp, "_platform_asset", lambda: asset)
    monkeypatch.setattr(fp, "verify_model_sha256", lambda p, e: None)
    return asset


def test_ensure_network_failure_classified(monkeypatch, tmp_path: Path) -> None:
    _arm_missing(monkeypatch, tmp_path)

    def boom(urls, dest, label, on_progress=None):
        raise RuntimeError("All 1 mirror(s) failed for x.zip. Errors:\n  timed out")

    monkeypatch.setattr(fp, "_download_from_mirrors", boom)
    with pytest.raises(fp.FpcalcProvisionError) as ei:
        fp.ensure_fpcalc()
    assert ei.value.reason == "the download didn't complete (check your connection)"


def test_ensure_disk_full_classified(monkeypatch, tmp_path: Path) -> None:
    _arm_missing(monkeypatch, tmp_path)

    def boom(urls, dest, label, on_progress=None):
        raise OSError("[Errno 28] No space left on device")

    monkeypatch.setattr(fp, "_download_from_mirrors", boom)
    with pytest.raises(fp.FpcalcProvisionError) as ei:
        fp.ensure_fpcalc()
    assert ei.value.reason == "there wasn't enough disk space"


def test_ensure_checksum_failure_classified_and_cleans_up(
    monkeypatch, tmp_path: Path,
) -> None:
    asset = _arm_missing(monkeypatch, tmp_path)

    def fake_download(urls, dest, label, on_progress=None):
        Path(dest).write_bytes(b"poisoned")

    monkeypatch.setattr(fp, "_download_from_mirrors", fake_download)

    def bad_verify(path, expected):
        raise RuntimeError(f"Model file {Path(path).name} failed SHA256 check: ...")

    monkeypatch.setattr(fp, "verify_model_sha256", bad_verify)

    with pytest.raises(fp.FpcalcProvisionError) as ei:
        fp.ensure_fpcalc()
    assert ei.value.reason == "the download failed a safety check"
    # The poisoned archive must not linger (next run re-downloads clean).
    assert not (fp.tools_dir() / asset.asset).exists()
    assert not fp.staged_fpcalc_path().exists()


# ---------------------------------------------------------------------------
# Skip paths (opt-out / unsupported) → None + honest reason
# ---------------------------------------------------------------------------


def test_ensure_noautoheal_skips_provisioning(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(fp, "find_fpcalc", lambda: None)
    # Real opt-out convention (delegates to wsl._autoheal_disabled via the env).
    monkeypatch.setenv("VIBECHEK_NO_AUTOHEAL", "1")

    def no_download(*a, **k):
        raise AssertionError("provisioning must be skipped under NO_AUTOHEAL")

    monkeypatch.setattr(fp, "_download_from_mirrors", no_download)
    assert fp.ensure_fpcalc() is None
    assert fp.fpcalc_skip_reason() == "automatic setup is turned off"


def test_ensure_unsupported_platform_skips(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(fp, "find_fpcalc", lambda: None)
    monkeypatch.setattr(fp, "_autoheal_disabled", lambda: False)
    monkeypatch.setattr(fp, "_platform_asset", lambda: None)

    def no_download(*a, **k):
        raise AssertionError("no asset for this platform → must not download")

    monkeypatch.setattr(fp, "_download_from_mirrors", no_download)
    assert fp.ensure_fpcalc() is None
    assert fp.fpcalc_skip_reason() == "this system has no automatic download available"


# ---------------------------------------------------------------------------
# find_staged_fpcalc
# ---------------------------------------------------------------------------


def test_find_staged_fpcalc_present_and_absent(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    assert fp.find_staged_fpcalc() is None  # nothing staged yet
    staged = fp.staged_fpcalc_path()
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_bytes(b"bin")
    os.chmod(staged, 0o755)
    assert fp.find_staged_fpcalc() == str(staged)
