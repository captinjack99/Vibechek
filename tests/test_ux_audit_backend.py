"""Tests for the whole-machine UX-audit backend fixes (WP-D/G/H/I/J).

Covers the shared user-facing error shape, the `.wslconfig` memory self-heal,
the analyzer's plain headlines + kinds, the one WSL-missing handler, the CLAP
checkpoint self-heal, and the native-install clean reinstall. Everything mocks
at the process/network/WSL seams — no real WSL, no real downloads.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from vibechek import analyzer, native_install, rpc, wsl
from vibechek.config import AnalysisConfig
from vibechek.errors import UserFacingError

# ---------------------------------------------------------------------------
# Shared error shape + dispatcher serialization
# ---------------------------------------------------------------------------


def test_user_facing_error_to_error_data() -> None:
    e = UserFacingError(
        "Plain headline.",
        detail="rc=1 stderr blah",
        kind="retryable",
        options={"can_install_wsl": True},
    )
    data = e.to_error_data()
    assert data == {
        "kind": "retryable",
        "headline": "Plain headline.",
        "detail": "rc=1 stderr blah",
        "can_install_wsl": True,
    }
    # str(exc) is the plain headline, so legacy `str(e)` callers stay readable.
    assert str(e) == "Plain headline."


def test_user_facing_error_defaults() -> None:
    e = UserFacingError("Just a headline.")
    assert e.kind == "fatal"
    assert e.to_error_data() == {"kind": "fatal", "headline": "Just a headline."}


def test_dispatch_serializes_user_facing_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A handler raising UserFacingError → an error frame whose message is the
    headline and whose `data` carries kind/headline/detail + options."""
    frames: list[dict] = []
    monkeypatch.setattr(rpc, "_write_message", frames.append)

    def boom(_params: dict) -> dict:
        raise UserFacingError(
            "Analysis stopped unexpectedly.",
            detail="rc=1",
            kind="retryable",
            options={"can_open_doctor": True},
        )

    monkeypatch.setitem(rpc.METHODS, "boom", boom)
    rpc._dispatch({"jsonrpc": "2.0", "id": 9, "method": "boom", "params": {}})

    err = next(f for f in frames if f.get("id") == 9)["error"]
    assert err["code"] == rpc.APP_ERROR
    assert err["message"] == "Analysis stopped unexpectedly."
    assert err["data"]["kind"] == "retryable"
    assert err["data"]["headline"] == "Analysis stopped unexpectedly."
    assert err["data"]["detail"] == "rc=1"
    assert err["data"]["can_open_doctor"] is True


# ---------------------------------------------------------------------------
# WP-D2 — .wslconfig memory read/bump self-heal
# ---------------------------------------------------------------------------


def test_wslconfig_target_math() -> None:
    # 32 GB host → min(24, 24) = 24 GB.
    assert wsl._choose_wslconfig_memory_target_mb(32768) == 24 * 1024
    # 16 GB host → min(12, 8) = 8 GB (Windows keeps its 8 GB floor).
    assert wsl._choose_wslconfig_memory_target_mb(16384) == 8 * 1024
    # Too small / unmeasurable → None (don't invent a value).
    assert wsl._choose_wslconfig_memory_target_mb(8192) is None
    assert wsl._choose_wslconfig_memory_target_mb(None) is None
    assert wsl._choose_wslconfig_memory_target_mb(0) is None


@pytest.mark.parametrize(
    ("value", "mb"),
    [("8GB", 8192), ("8192MB", 8192), ("16G", 16384), ("1TB", 1024 * 1024),
     ("bogus", None), (None, None)],
)
def test_parse_wsl_size(value: str | None, mb: int | None) -> None:
    assert wsl._parse_wsl_size_to_mb(value) == mb


def test_bump_wslconfig_creates_cleanly(tmp_path: Path) -> None:
    p = tmp_path / ".wslconfig"
    out = wsl.bump_wslconfig_memory(path=p, target_mb=24 * 1024)
    assert out["ok"] is True and out["changed"] is True
    assert out["new"] == "24GB" and out["restart_required"] is True
    assert p.read_text(encoding="utf-8") == "[wsl2]\nmemory=24GB\n"


def test_bump_wslconfig_preserves_unrelated_content(tmp_path: Path) -> None:
    """The read-modify-write MUST keep every other line (comments, other keys,
    other sections) — only the memory= value changes."""
    p = tmp_path / ".wslconfig"
    p.write_text(
        "# my custom config\n"
        "[wsl2]\n"
        "processors=4\n"
        "memory=8GB\n"
        "swap=0\n"
        "\n"
        "[experimental]\n"
        "autoMemoryReclaim=gradual\n",
        encoding="utf-8",
    )
    out = wsl.bump_wslconfig_memory(path=p, target_mb=24 * 1024)
    assert out["ok"] is True and out["old"] == "8GB" and out["new"] == "24GB"
    text = p.read_text(encoding="utf-8")
    assert "# my custom config" in text
    assert "processors=4" in text
    assert "swap=0" in text
    assert "[experimental]" in text
    assert "autoMemoryReclaim=gradual" in text
    assert "memory=24GB" in text
    assert "memory=8GB" not in text


def test_bump_wslconfig_inserts_when_no_memory_key(tmp_path: Path) -> None:
    p = tmp_path / ".wslconfig"
    p.write_text("[wsl2]\nprocessors=4\n", encoding="utf-8")
    out = wsl.bump_wslconfig_memory(path=p, target_mb=16 * 1024)
    text = p.read_text(encoding="utf-8")
    assert "processors=4" in text
    assert "memory=16GB" in text
    assert out["old"] is None and out["new"] == "16GB"


def test_bump_wslconfig_appends_wsl2_section_when_absent(tmp_path: Path) -> None:
    p = tmp_path / ".wslconfig"
    p.write_text("[experimental]\nsparseVhd=true\n", encoding="utf-8")
    out = wsl.bump_wslconfig_memory(path=p, target_mb=16 * 1024)
    text = p.read_text(encoding="utf-8")
    assert "[experimental]" in text and "sparseVhd=true" in text
    assert "[wsl2]" in text and "memory=16GB" in text
    assert out["changed"] is True


def test_bump_wslconfig_never_shrinks(tmp_path: Path) -> None:
    p = tmp_path / ".wslconfig"
    p.write_text("[wsl2]\nmemory=48GB\n", encoding="utf-8")
    out = wsl.bump_wslconfig_memory(path=p, target_mb=24 * 1024)
    assert out["ok"] is True and out["changed"] is False
    assert out["restart_required"] is False
    assert p.read_text(encoding="utf-8") == "[wsl2]\nmemory=48GB\n"  # untouched


def test_read_wslconfig_memory_absent(tmp_path: Path) -> None:
    out = wsl.read_wslconfig_memory(path=tmp_path / ".wslconfig")
    assert out == {"ok": True, "path": str(tmp_path / ".wslconfig"),
                   "exists": False, "memory": None, "memory_mb": None}


def test_bump_wslconfig_unmeasurable_host_is_honest(tmp_path: Path) -> None:
    out = wsl.bump_wslconfig_memory(path=tmp_path / ".wslconfig", host_total_mb=0)
    assert out["ok"] is False
    assert out["kind"] == "fatal"
    assert "headline" in out and "restart_required" in out
    assert out["restart_required"] is False


def test_increase_wsl_memory_rpc_registered_and_delegates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert "increase_wsl_memory" in rpc.METHODS
    sentinel = {"ok": True, "changed": True, "old": "8GB", "new": "24GB",
                "restart_required": True}
    monkeypatch.setattr(wsl, "bump_wslconfig_memory", lambda: sentinel)
    assert rpc._increase_wsl_memory({}) == sentinel


# ---------------------------------------------------------------------------
# WP-H1 — one shared "WSL missing" handler + phantom-pointer removal
# ---------------------------------------------------------------------------


def test_wsl_missing_error_shape() -> None:
    out = wsl.wsl_missing_error()
    assert out["ok"] is False
    assert out["kind"] == "fatal"
    assert out["can_install_wsl"] is True
    assert "isn't installed" in out["headline"]
    # The raw string is DEMOTED to detail / kept for logs, not the headline.
    assert "wsl.exe" not in out["headline"]
    assert out["error"] == "wsl.exe not found"


def test_all_six_wsl_missing_sites_use_the_shared_handler() -> None:
    """Grep-count guard: the `wsl.exe not found` literal survives in exactly one
    place (the shared handler), and all six call sites route through it."""
    src = Path(wsl.__file__).read_text(encoding="utf-8")
    # The literal appears only inside wsl_missing_error (+ its docstring mention).
    assert src.count('"wsl.exe not found"') == 1
    assert src.count("return wsl_missing_error()") == 6


def test_phantom_set_up_wsl_pointer_removed_from_analyzer() -> None:
    """The 'Set up WSL' Settings control never existed in the UI — the analyzer
    must not point users at it (WP-H1)."""
    src = Path(analyzer.__file__).read_text(encoding="utf-8")
    assert "Set up WSL" not in src


# ---------------------------------------------------------------------------
# WP-G — analyzer plain headlines + kinds (managed-venv path)
# ---------------------------------------------------------------------------


def _cfg(tmp_path: Path) -> AnalysisConfig:
    return AnalysisConfig(models_dir=tmp_path / "models")


def test_g2_managed_venv_exit_nonzero_is_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(args, on_stderr_line=None, engine="essentia_tf"):
        return subprocess.CompletedProcess(args, 1, "boom stdout", "")

    monkeypatch.setattr(native_install, "run_vibechek_in_native_venv", fake_run)
    with pytest.raises(UserFacingError) as ei:
        analyzer._analyze_via_native_venv(
            tmp_path, _cfg(tmp_path), None, None, 0, None,
        )
    assert ei.value.kind == "retryable"
    assert ei.value.headline == "Analysis stopped unexpectedly."
    # exit code / stdout demoted to detail, never the headline.
    assert "1" in (ei.value.detail or "") and "boom" in (ei.value.detail or "")


def test_g4_managed_venv_bad_json_is_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    out_file = tmp_path / "out.json"
    out_file.write_text("not json at all", encoding="utf-8")

    def fake_run(args, on_stderr_line=None, engine="essentia_tf"):
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(native_install, "run_vibechek_in_native_venv", fake_run)
    with pytest.raises(UserFacingError) as ei:
        analyzer._analyze_via_native_venv(
            tmp_path, _cfg(tmp_path), None, out_file, 0, None,
        )
    assert ei.value.kind == "retryable"
    assert ei.value.headline == "Analysis results were corrupted while saving."


def test_g2_managed_venv_no_output_is_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    out_file = tmp_path / "empty.json"
    out_file.write_text("", encoding="utf-8")

    def fake_run(args, on_stderr_line=None, engine="essentia_tf"):
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(native_install, "run_vibechek_in_native_venv", fake_run)
    with pytest.raises(UserFacingError) as ei:
        analyzer._analyze_via_native_venv(
            tmp_path, _cfg(tmp_path), None, out_file, 0, None,
        )
    assert ei.value.kind == "retryable"
    assert "didn't produce any results" in ei.value.headline


# ---------------------------------------------------------------------------
# WP-I1/I3 — CLAP checkpoint self-heal (delete + re-download) at load
# ---------------------------------------------------------------------------


def test_reheal_checkpoint_redownloads_and_replaces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from vibechek import clap_genre

    ckpt = tmp_path / "music_clap.pt"
    ckpt.write_bytes(b"corrupt")

    calls = {"verify": 0, "download": 0}

    def fake_verify(path, expected):
        calls["verify"] += 1
        # First verify (the cached corrupt file) fails; after download, passes.
        if calls["verify"] == 1:
            raise RuntimeError("SHA256 mismatch")

    def fake_download(urls, dest, label=None, on_progress=None):
        calls["download"] += 1
        Path(dest).write_bytes(b"good bytes")

    monkeypatch.setattr("vibechek.model_download.verify_model_sha256", fake_verify)
    monkeypatch.setattr(analyzer, "_download_from_mirrors", fake_download)

    assert clap_genre._reheal_checkpoint(ckpt) is True
    assert calls["download"] == 1
    assert ckpt.read_bytes() == b"good bytes"  # corrupt file replaced


def test_load_clap_model_triggers_reheal_on_integrity_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from vibechek import clap_genre

    ckpt = tmp_path / "music_clap.pt"
    ckpt.write_bytes(b"corrupt")

    def fake_verify(path, expected):
        raise RuntimeError("SHA256 mismatch")

    reheal_calls = {"n": 0}

    def fake_reheal(path):
        reheal_calls["n"] += 1
        return False  # re-download didn't fix it → honest error

    monkeypatch.setattr("vibechek.model_download.verify_model_sha256", fake_verify)
    monkeypatch.setattr(clap_genre, "_reheal_checkpoint", fake_reheal)
    monkeypatch.setattr(clap_genre, "clap_checkpoint_path", lambda: ckpt)
    monkeypatch.delenv("VIBECHEK_NO_AUTOHEAL", raising=False)

    with pytest.raises(UserFacingError) as ei:
        clap_genre.load_clap_model(checkpoint=None)
    assert reheal_calls["n"] == 1  # self-heal was attempted
    assert ei.value.headline == "The advanced genre model file is damaged."
    assert ei.value.kind == "retryable"
    # No CLI reference in the headline (WP-I3).
    assert "verify-models" not in ei.value.headline
    assert "verify-models" not in (ei.value.detail or "")


def test_load_clap_model_no_autoheal_skips_reheal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from vibechek import clap_genre

    ckpt = tmp_path / "music_clap.pt"
    ckpt.write_bytes(b"corrupt")

    def fake_verify(path, expected):
        raise RuntimeError("SHA256 mismatch")

    def fake_reheal(path):  # pragma: no cover - must NOT be called
        raise AssertionError("reheal must not run when autoheal is disabled")

    monkeypatch.setattr("vibechek.model_download.verify_model_sha256", fake_verify)
    monkeypatch.setattr(clap_genre, "_reheal_checkpoint", fake_reheal)
    monkeypatch.setattr(clap_genre, "clap_checkpoint_path", lambda: ckpt)
    monkeypatch.setenv("VIBECHEK_NO_AUTOHEAL", "1")

    with pytest.raises(UserFacingError) as ei:
        clap_genre.load_clap_model(checkpoint=None)
    assert "VIBECHEK_NO_AUTOHEAL" in (ei.value.detail or "")


# ---------------------------------------------------------------------------
# WP-I2 — both setup paths force-redownload on SHA failure
# ---------------------------------------------------------------------------


def test_wsl_clap_setup_script_reverifies_cached_file() -> None:
    """Bash-side (WP-I2): the script must SHA-check the CACHED checkpoint, not
    only re-fetch when it's empty (`[ ! -s ]`) — else a corrupt-but-full-size
    file was silently kept and 're-run setup' was a no-op."""
    script = wsl._clap_setup_script("venv", "http://x/ckpt", "d" * 64)
    # It verifies the cached file's hash (against $CKPT, not just $CKPT.partial).
    assert "sha256sum -c" in script
    assert "$CKPT" in script and "NEED_DOWNLOAD" in script
    # The active empty-only guard statement is gone (it only survives in a
    # comment explaining the old bug).
    assert 'if [ ! -s "$CKPT" ]; then' not in script
    # The cached-file check runs sha256sum against the final path, not just the
    # partial download.
    assert 'sha256sum -c - >/dev/null 2>&1' in script


def test_native_clap_setup_rejects_corrupt_cached_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Native-side (WP-I2): a corrupt-but-full-size cached checkpoint is deleted
    and re-downloaded, not reused."""
    monkeypatch.setattr(native_install, "IS_SUPPORTED", True)
    monkeypatch.setattr(native_install, "_CLAP_MIN_CKPT_BYTES", 4)
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

    clap_dir = tmp_path / ".vibechek" / "clap"
    clap_dir.mkdir(parents=True)
    ckpt = clap_dir / "music_clap.pt"
    ckpt.write_bytes(b"corrupt-but-big-enough")

    py = tmp_path / "python"
    py.write_text("")
    monkeypatch.setattr(native_install, "_genre_venv_python", lambda engine: (py, None))
    monkeypatch.setattr(native_install, "_run_with_progress", lambda *a, **k: (0, []))
    monkeypatch.setattr(
        native_install, "_run_subprocess_cancellable",
        lambda *a, **k: (0, "clap import ok", "", False),
    )

    verify_calls = {"n": 0}

    def fake_verify(path, expected):
        verify_calls["n"] += 1
        # The cached file fails; the freshly downloaded one passes.
        if verify_calls["n"] == 1:
            raise RuntimeError("SHA256 mismatch")

    downloaded = {"n": 0}

    def fake_download(urls, dest, label=None, on_progress=None):
        downloaded["n"] += 1
        Path(dest).write_bytes(b"good-download")

    monkeypatch.setattr("vibechek.model_download.verify_model_sha256", fake_verify)
    monkeypatch.setattr(analyzer, "_download_from_mirrors", fake_download)

    out = native_install.setup_clap_native()
    assert out["ok"] is True
    assert downloaded["n"] == 1  # corrupt cache was NOT reused


# ---------------------------------------------------------------------------
# WP-J1 — native install verify failure → clean reinstall once (no loop)
# ---------------------------------------------------------------------------


def test_native_install_verify_failure_does_clean_reinstall_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    vd = tmp_path / "venv"
    monkeypatch.setattr(native_install, "IS_SUPPORTED", True)
    monkeypatch.setattr(native_install, "_venv_dir", lambda engine: vd)
    monkeypatch.setattr(native_install, "_find_host_python", lambda: "python")
    monkeypatch.setattr(native_install, "_run_with_progress", lambda *a, **k: (0, []))

    calls = {"venv_create": 0, "verify": 0}

    def fake_subproc(args, timeout=None):
        if "venv" in args:  # `python -m venv <vd>` — recreate the skeleton
            calls["venv_create"] += 1
            (vd / "bin").mkdir(parents=True, exist_ok=True)
            for name in ("python", "python3", "vibechek"):
                (vd / "bin" / name).write_text("")
            return (0, "", "", False)
        # Every verify call (--version / import essentia) fails.
        calls["verify"] += 1
        return (1, "", "still broken", False)

    monkeypatch.setattr(native_install, "_run_subprocess_cancellable", fake_subproc)

    out = native_install.install_essentia_native()
    assert out["ok"] is False
    assert out["headline"] == "Setup finished, but the analysis engine still isn't working."
    assert out["kind"] == "fatal"
    # Reinstalled EXACTLY once (initial build + one clean rebuild) — no loop.
    assert calls["venv_create"] == 2
