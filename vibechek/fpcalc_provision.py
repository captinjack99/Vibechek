"""Zero-setup provisioning for ``fpcalc`` (Chromaprint's fingerprint CLI).

WHY THIS EXISTS
---------------
Dedupe's audio-fingerprint phase — the one that catches *near*-duplicates
(re-encodes, different bitrates, re-tags of the same master) — shells out to
``fpcalc``. When ``fpcalc`` was missing, that phase silently no-op'd and the
GUI dead-ended at "Fingerprint scan skipped — fpcalc not found" with no way to
fix it. The user was left to discover, download, and install a command-line
tool by hand: exactly the manual-setup dead end the zero-setup doctrine forbids
(detect -> SELF-HEAL -> run). This module heals the condition instead: it
fetches the official, unmodified Chromaprint release binary for the current
platform on demand, verifies a pinned SHA256, and stages it under the app data
dir so the fingerprint phase just works — no user action, no banner (except an
honest one if the *download itself* fails, and even then it retries next run).

The download is small (~1.5-2.7 MB), so unlike the multi-GB opt-in model
checkpoints this is provisioned AUTOMATICALLY inside the dedupe flow rather than
behind a "download?" prompt.

LICENSING
---------
Chromaprint / ``fpcalc`` is LGPL-2.1. We do NOT vendor it into our installer.
We fetch the *unmodified* official release binary from the AcoustID project on
demand and run it as a standalone executable (the LGPL's use-as-a-tool case),
which keeps our AGPL distribution clean of LGPL object code. Upstream project:
https://github.com/acoustid/chromaprint
"""

from __future__ import annotations

import logging
import os
import platform
import sys
import tarfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

from vibechek import config
from vibechek.model_download import (
    _download_from_mirrors,
    _fmt_bytes,
    verify_model_sha256,
)
from vibechek.utils import ProgressCallback, find_fpcalc, report_progress

log = logging.getLogger(__name__)

# Pinned to the official AcoustID Chromaprint release we fetch from. The
# fpcalc CLI + protocol have been stable at 1.5.1 since 2021; a version bump
# means re-hashing the three assets below.
_FPCALC_VERSION = "1.5.1"
_RELEASE_BASE = (
    f"https://github.com/acoustid/chromaprint/releases/download/v{_FPCALC_VERSION}"
)


@dataclass(frozen=True)
class _PlatformAsset:
    """Which release asset to fetch for one platform, and how to open it."""

    asset: str          # release-asset filename under _RELEASE_BASE
    sha256: str         # pinned digest of that exact asset (see provenance below)
    archive: str        # "zip" | "targz"
    member: str         # basename of the binary INSIDE the archive


# PROVENANCE — pinned 2026-07-20. AcoustID publishes no digests alongside its
# release assets, so (exactly as the CLAP checkpoint was pinned) each official
# v1.5.1 asset was downloaded from the release above and hashed here. Verified
# on this box: the windows-x86_64 binary extracts and `fpcalc -version` reports
# "fpcalc version 1.5.1". A post-download SHA256 mismatch deletes the file and
# aborts provisioning — we never run an unverified binary.
#
#   windows-x86_64.zip     36b478e16aa69f757f376645db0d436073a42c0097b6bb2677109e7835b59bbc
#   macos-universal.tar.gz d4d8faff4b5f7c558d9be053da47804f9501eaa6c2f87906a9f040f38d61c860
#   linux-x86_64.tar.gz    4d7433a7f778e5946d7225230681cbcd634e153316ecac87c538c33ac32387a5
_WINDOWS = _PlatformAsset(
    asset=f"chromaprint-fpcalc-{_FPCALC_VERSION}-windows-x86_64.zip",
    sha256="36b478e16aa69f757f376645db0d436073a42c0097b6bb2677109e7835b59bbc",
    archive="zip",
    member="fpcalc.exe",
)
_MACOS = _PlatformAsset(
    asset=f"chromaprint-fpcalc-{_FPCALC_VERSION}-macos-universal.tar.gz",
    sha256="d4d8faff4b5f7c558d9be053da47804f9501eaa6c2f87906a9f040f38d61c860",
    archive="targz",
    member="fpcalc",
)
_LINUX = _PlatformAsset(
    asset=f"chromaprint-fpcalc-{_FPCALC_VERSION}-linux-x86_64.tar.gz",
    sha256="4d7433a7f778e5946d7225230681cbcd634e153316ecac87c538c33ac32387a5",
    archive="targz",
    member="fpcalc",
)


class FpcalcProvisionError(RuntimeError):
    """Provisioning was attempted but failed.

    ``reason`` is a short, plain-user phrase (no "fpcalc", no stack detail) that
    slots into the GUI banner: "couldn't set up the audio fingerprint tool
    (<reason>)". The full technical cause stays in ``args[0]`` / the logs.
    """

    def __init__(self, message: str, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


def _binary_name() -> str:
    """The on-disk name of the fpcalc executable for this OS."""
    return "fpcalc.exe" if sys.platform == "win32" else "fpcalc"


def tools_dir() -> Path:
    """App-data directory where the staged binary lives (``<DATA_DIR>/tools``).

    Reads ``config.DATA_DIR`` at call time (not import time) so a test can point
    it at a scratch dir via the VIBECHEK data-dir override without this module
    caching a stale path.
    """
    return config.DATA_DIR / "tools"


def staged_fpcalc_path() -> Path:
    """Where a provisioned fpcalc binary is (or would be) staged."""
    return tools_dir() / _binary_name()


def find_staged_fpcalc() -> str | None:
    """Return the staged fpcalc path if one is present and runnable, else None.

    This is resolution step (b): a previous provision left a binary in the app
    data dir. We do NOT re-hash it on every dedupe (that cost was paid at
    extraction time); an existence + executable check is enough. On Windows
    ``os.access(X_OK)`` is effectively an existence check, which is fine.
    """
    p = staged_fpcalc_path()
    try:
        if p.is_file() and os.access(p, os.X_OK):
            return str(p)
    except OSError:
        pass
    return None


def resolve_fpcalc() -> str | None:
    """Resolution steps (a) + (b) only — NO provisioning, NO network.

    PATH wins over a staged copy (a system/user-managed fpcalc is preferred and
    may be newer). Returns None when neither is present, which is the signal to
    the caller that provisioning (step c) is needed.
    """
    on_path = find_fpcalc()
    if on_path:
        return on_path
    return find_staged_fpcalc()


def _autoheal_disabled() -> bool:
    """Whether the user opted out of automatic environment repair.

    Delegates to the single source of truth for the ``VIBECHEK_NO_AUTOHEAL``
    convention (wsl.py) so provisioning honours the same opt-out as every other
    self-heal. Imported lazily to keep this module import-light for the CLI /
    doctor — wsl.py is only pulled in when we're actually about to provision.
    """
    from vibechek.wsl import _autoheal_disabled as _wsl_autoheal_disabled  # noqa: PLC0415

    return _wsl_autoheal_disabled()


def _platform_asset() -> _PlatformAsset | None:
    """Pick the release asset for the current platform, or None if unsupported.

    We only ship pins for the three official prebuilt targets (Windows x86_64,
    macOS universal, Linux x86_64). An unsupported platform (e.g. Linux on ARM)
    returns None so the caller banners honestly rather than 404-looping.
    """
    if sys.platform == "win32":
        return _WINDOWS
    if sys.platform == "darwin":
        # The macOS asset is a universal (arm64 + x86_64) binary, so it serves
        # both Apple Silicon and Intel Macs with one pin.
        return _MACOS
    if sys.platform.startswith("linux") and platform.machine().lower() in {
        "x86_64", "amd64",
    }:
        return _LINUX
    return None


def fpcalc_skip_reason() -> str:
    """Plain-language reason provisioning was SKIPPED (not attempted).

    Distinct from a provisioning *failure*: skip = we deliberately didn't try
    (auto-heal turned off, or no prebuilt binary for this platform). The caller
    uses this for the honest banner when ``ensure_fpcalc`` returns None.
    """
    if _autoheal_disabled():
        return "automatic setup is turned off"
    if _platform_asset() is None:
        return "this system has no automatic download available"
    # Shouldn't be reached (ensure_fpcalc would have provisioned), but never
    # leave the banner reason empty.
    return "the audio fingerprint tool isn't installed"


def _find_member(names: list[str], basename: str) -> str | None:
    """Return the archive member whose basename matches, or None.

    The official archives wrap the binary in a single top-level directory
    (``chromaprint-fpcalc-1.5.1-<plat>/fpcalc[.exe]``), so we match on the
    trailing path component rather than a hardcoded full path — robust to a
    future layout tweak.
    """
    for name in names:
        # Archive paths always use forward slashes; splitting on "/" gives the
        # basename regardless of the host OS.
        if name.rstrip("/").split("/")[-1] == basename:
            return name
    return None


def _extract_binary(archive_path: Path, asset: _PlatformAsset, dest: Path) -> None:
    """Extract just the fpcalc binary from ``archive_path`` to ``dest``.

    We read the single member's bytes and write them ourselves (never
    ``extractall``) so a maliciously-crafted archive can't path-traverse out of
    the tools dir — dest is always the one path we control. chmod +x on POSIX so
    the extracted binary is runnable.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    if asset.archive == "zip":
        with zipfile.ZipFile(archive_path) as zf:
            member = _find_member(zf.namelist(), asset.member)
            if member is None:
                raise FpcalcProvisionError(
                    f"{asset.member} not found in {asset.asset}",
                    reason="the downloaded file was not in the expected format",
                )
            data = zf.read(member)
    else:
        with tarfile.open(archive_path, "r:gz") as tf:
            member = _find_member(tf.getnames(), asset.member)
            if member is None:
                raise FpcalcProvisionError(
                    f"{asset.member} not found in {asset.asset}",
                    reason="the downloaded file was not in the expected format",
                )
            extracted = tf.extractfile(member)
            if extracted is None:
                raise FpcalcProvisionError(
                    f"could not read {member} from {asset.asset}",
                    reason="the downloaded file was not in the expected format",
                )
            with extracted:
                data = extracted.read()

    # Atomic-ish write: temp then replace, so a crash mid-write never leaves a
    # truncated binary that would fail to run on the next dedupe.
    tmp = dest.with_suffix(dest.suffix + ".partial")
    tmp.write_bytes(data)
    if os.name != "nt":
        os.chmod(tmp, 0o755)
    tmp.replace(dest)


def _classify_failure(err: Exception) -> str:
    """Map a download/extract exception to a plain-user banner reason.

    Same substring-classification approach as wsl.py's ``_explain_install_failure``
    and model_download's ``_download_failure_hint`` — the distinction matters
    because "retry" is the right advice for a network blip, futile for a full
    disk, and different again for a corrupted download.
    """
    blob = str(err).lower()
    if "no space left on device" in blob or "errno 28" in blob:
        return "there wasn't enough disk space"
    if "failed sha256 check" in blob or "checksum" in blob:
        return "the download failed a safety check"
    return "the download didn't complete (check your connection)"


def ensure_fpcalc(on_progress: ProgressCallback | None = None) -> str | None:
    """Return a usable fpcalc path, provisioning it on demand if needed.

    Resolution order:
      (a) ``fpcalc`` on PATH / well-known locations (a user- or system-managed
          install always wins).
      (b) a previously staged binary under ``<DATA_DIR>/tools``.
      (c) neither present -> download the pinned official release asset for this
          platform, verify its SHA256, extract the binary into the tools dir,
          and return it.

    Returns the path on success. Returns ``None`` when provisioning is
    deliberately SKIPPED — ``VIBECHEK_NO_AUTOHEAL`` is set, or there is no
    prebuilt binary for this platform (see ``fpcalc_skip_reason``). Raises
    ``FpcalcProvisionError`` (with a plain-language ``reason``) when a provision
    was ATTEMPTED but failed (network / disk / checksum / bad archive), so the
    caller can tell "skipped" from "tried and failed" and banner accordingly.
    """
    # (a) + (b): detection always runs, even under NO_AUTOHEAL — opting out of
    # auto-repair must never blind us to an fpcalc that's already available.
    resolved = resolve_fpcalc()
    if resolved:
        return resolved

    # (c): provision. Honour the opt-out first (detection above already ran).
    if _autoheal_disabled():
        log.info("fpcalc missing and VIBECHEK_NO_AUTOHEAL set — skipping provision.")
        return None

    asset = _platform_asset()
    if asset is None:
        log.info(
            "fpcalc missing and no prebuilt binary for %s/%s — skipping provision.",
            sys.platform, platform.machine(),
        )
        return None

    dest = staged_fpcalc_path()
    archive_path = tools_dir() / asset.asset
    archive_path.parent.mkdir(parents=True, exist_ok=True)

    # Announce up-front — the small one-time download should be visible in the
    # same progress channel the dedupe scan already uses, in plain-user words
    # (the doctrine: clamps/fallbacks/heals are GUI-visible, not silent).
    report_progress(
        on_progress, 0, 1,
        "Setting up the audio fingerprint tool (one-time, ~2 MB)…",
    )

    def _bytes_progress(done: int, total: int) -> None:
        report_progress(
            on_progress, done, max(total, done),
            f"Setting up the audio fingerprint tool "
            f"({_fmt_bytes(done)}/{_fmt_bytes(total)})",
        )

    try:
        # Reuse the model downloader's mirror/retry/cancellation/.partial
        # machinery — one battle-tested download path for the whole app.
        _download_from_mirrors(
            [f"{_RELEASE_BASE}/{asset.asset}"],
            archive_path,
            label=asset.asset,
            on_progress=_bytes_progress,
        )
        # Never run an unverified binary: a poisoned mirror or a bit-rotted
        # download is deleted and the provision aborts.
        verify_model_sha256(archive_path, asset.sha256)
        _extract_binary(archive_path, asset, dest)
    except FpcalcProvisionError:
        raise
    except Exception as e:  # noqa: BLE001 — classify everything into a banner reason
        # A failed verify leaves a poisoned archive around; drop it so the next
        # run re-downloads clean rather than re-failing on the cached bad file.
        if "failed sha256 check" in str(e).lower():
            archive_path.unlink(missing_ok=True)
        log.warning("fpcalc provisioning failed: %s", e)
        raise FpcalcProvisionError(str(e), reason=_classify_failure(e)) from e
    finally:
        # The archive is scratch — the extracted binary is what we keep.
        archive_path.unlink(missing_ok=True)

    log.info("Provisioned fpcalc %s into %s", _FPCALC_VERSION, dest)
    report_progress(on_progress, 1, 1, "Audio fingerprint tool ready")
    return str(dest)


__all__ = [
    "FpcalcProvisionError",
    "ensure_fpcalc",
    "find_staged_fpcalc",
    "fpcalc_skip_reason",
    "resolve_fpcalc",
    "staged_fpcalc_path",
    "tools_dir",
]
