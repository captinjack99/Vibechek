"""Tests for vibechek.native_install — the managed-venv disk probe.

`probe_native_venv` is pure disk inspection, so we build venv skeletons under
tmp_path and point the module at them. The Unix-layout cases are the
regression lock for the `lib/python3.*` glob bug: `Path.glob()` only expands
wildcards in the pattern argument, so the old code — which put the wildcard in
the *parent* path — never matched the Unix site-packages layout, and
Linux/macOS always reported essentia as not installed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from vibechek import native_install


def _make_fake_venv(root: Path, layout: str, dist_info: str | None) -> Path:
    """Build a minimal venv skeleton: a python binary + one dist-info dir."""
    vd = root / "venv"
    if layout == "unix":
        (vd / "bin").mkdir(parents=True)
        (vd / "bin" / "python3").write_text("#!/bin/sh\n")
        sp = vd / "lib" / "python3.12" / "site-packages"
    else:  # windows
        (vd / "Scripts").mkdir(parents=True)
        (vd / "Scripts" / "python.exe").write_text("")
        sp = vd / "Lib" / "site-packages"
    sp.mkdir(parents=True)
    if dist_info is not None:
        (sp / dist_info).mkdir()
    return vd


@pytest.mark.parametrize(
    ("layout", "dist_info", "version"),
    [
        # essentia-tensorflow in the default venv — the layout the desktop app
        # actually creates on Linux/macOS (and the one the old glob missed).
        ("unix", "essentia_tensorflow-2.1b6.dev1110.dist-info", "2.1b6.dev1110"),
        # plain essentia, as installed into the ONNX venv
        ("unix", "essentia-2.1b6.dev1110.dist-info", "2.1b6.dev1110"),
        # Windows venv layout (kept symmetric for tests/future-proofing)
        ("windows", "essentia_tensorflow-2.1b6.dev1110.dist-info", "2.1b6.dev1110"),
    ],
)
def test_probe_detects_essentia_in_site_packages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    layout: str,
    dist_info: str,
    version: str,
) -> None:
    vd = _make_fake_venv(tmp_path, layout, dist_info)
    monkeypatch.setattr(native_install, "IS_SUPPORTED", True)
    monkeypatch.setattr(native_install, "VENV_DIR", vd)

    status = native_install.probe_native_venv()

    assert status.essentia_installed is True
    assert status.essentia_version == version


def test_probe_reports_missing_essentia(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A venv with packages but no essentia dist-info reads as not installed."""
    vd = _make_fake_venv(tmp_path, "unix", "click-8.1.7.dist-info")
    monkeypatch.setattr(native_install, "IS_SUPPORTED", True)
    monkeypatch.setattr(native_install, "VENV_DIR", vd)

    status = native_install.probe_native_venv()

    assert status.essentia_installed is False
    assert status.essentia_version is None


def test_probe_handles_absent_venv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No venv on disk → clean 'nothing installed' status, no exception."""
    monkeypatch.setattr(native_install, "IS_SUPPORTED", True)
    monkeypatch.setattr(native_install, "VENV_DIR", tmp_path / "missing-venv")

    status = native_install.probe_native_venv()

    assert status.venv_python is None
    assert status.essentia_installed is False


# ---------------------------------------------------------------------------
# ML-stack install ceiling: the GPU wheel set is multi-GB and must get the
# 2 h wall-clock (live-verified: the 15 min ceiling killed a real CUDA-stack
# install mid-download on an ordinary connection). CPU sets keep 30 min.
# ---------------------------------------------------------------------------


def _run_install_capturing_ml_step(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    engine: str,
    has_nvidia: bool,
) -> tuple[list[str], int]:
    """Drive install_essentia_native with every subprocess stubbed; return the
    (package list, timeout) the ML-stack pip step was invoked with."""
    vd = _make_fake_venv(tmp_path, "unix", None)
    # engine="onnx" targets the sibling venv-onnx — give it a skeleton too so
    # the venv-create step is skipped for both engines.
    onnx_vd = vd.parent / "venv-onnx"
    (onnx_vd / "bin").mkdir(parents=True)
    (onnx_vd / "bin" / "python3").write_text("#!/bin/sh\n")
    monkeypatch.setattr(native_install, "IS_SUPPORTED", True)
    # Pin the platform to Linux so the package-selection branch under test is
    # reachable regardless of the CI host OS. The ONNX branch checks `if IS_MAC`
    # FIRST and forces CPU `onnxruntime` on macOS — so without this the
    # nvidia-smi GPU path is unreachable on a macOS runner and the GPU-stack
    # ceiling assertion fails there (the GPU wheel set only exists on Linux+NVIDIA
    # anyway). Was a real macOS-only CI red.
    monkeypatch.setattr(native_install, "IS_MAC", False)
    monkeypatch.setattr(native_install, "IS_LINUX", True)
    monkeypatch.setattr(native_install, "VENV_DIR", vd)
    monkeypatch.setattr(native_install, "_find_host_python", lambda: "/usr/bin/python3")
    monkeypatch.setattr(
        native_install.shutil, "which",
        lambda name: "/usr/bin/nvidia-smi" if (name == "nvidia-smi" and has_nvidia) else None,
    )
    monkeypatch.setattr(
        native_install, "_run_subprocess_cancellable",
        lambda args, timeout: (0, "stub-version", "", False),
    )

    captured: dict = {}

    def _fake_run_with_progress(args: list[str], on_progress, timeout: int):
        # The ML-stack step is the only `pip install` whose args carry an
        # essentia package (the pip/wheel upgrade and the vibechek step don't).
        if any(str(a).startswith("essentia") for a in args):
            captured["packages"] = args[args.index("install") + 1:]
            captured["timeout"] = timeout
        return 0, []

    monkeypatch.setattr(native_install, "_run_with_progress", _fake_run_with_progress)

    result = native_install.install_essentia_native(engine=engine)
    assert result.get("ok") is True, result
    return captured["packages"], captured["timeout"]


def test_ml_install_gpu_stack_gets_two_hour_ceiling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    packages, timeout = _run_install_capturing_ml_step(
        tmp_path, monkeypatch, engine="onnx", has_nvidia=True,
    )
    assert "onnxruntime-gpu" in packages
    assert any(p.startswith("nvidia-") for p in packages)
    assert timeout == 60 * 120


def test_ml_install_cpu_onnx_keeps_thirty_min_ceiling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    packages, timeout = _run_install_capturing_ml_step(
        tmp_path, monkeypatch, engine="onnx", has_nvidia=False,
    )
    assert "onnxruntime" in packages
    assert not any(p.startswith("nvidia-") for p in packages)
    assert timeout == 60 * 30


def test_ml_install_essentia_tf_keeps_thirty_min_ceiling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    packages, timeout = _run_install_capturing_ml_step(
        tmp_path, monkeypatch, engine="essentia_tf", has_nvidia=False,
    )
    assert "essentia-tensorflow" in packages
    assert timeout == 60 * 30


# ---------------------------------------------------------------------------
# Native genre-engine setups (CLAP / resolver) — the Linux/macOS analogs of the
# WSL scripts. All subprocess/download seams mocked; platform-independent.
# ---------------------------------------------------------------------------


def _genre_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, venv_exists: bool = True
) -> Path:
    """Point IS_SUPPORTED/VENV_DIR/Path.home at a tmp skeleton."""
    monkeypatch.setattr(native_install, "IS_SUPPORTED", True)
    vd = tmp_path / "venv"
    if venv_exists:
        (vd / "bin").mkdir(parents=True)
        (vd / "bin" / "python3").write_text("#!/bin/sh\n")
    monkeypatch.setattr(native_install, "VENV_DIR", vd)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    return vd


def test_setup_clap_native_requires_engine_venv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _genre_env(tmp_path, monkeypatch, venv_exists=False)
    out = native_install.setup_clap_native()
    assert out["ok"] is False
    assert "engine setup" in out["error"]


def test_setup_clap_native_happy_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CPU torch index first, laion-clap installed, checkpoint downloaded to
    ~/.vibechek/clap/, import-verify run — ok:True."""
    _genre_env(tmp_path, monkeypatch)
    monkeypatch.setattr(native_install, "_CLAP_MIN_CKPT_BYTES", 10)

    pip_calls: list[list[str]] = []

    def _fake_run_with_progress(args, on_progress, timeout, env=None):
        pip_calls.append(list(args))
        return 0, []

    monkeypatch.setattr(native_install, "_run_with_progress", _fake_run_with_progress)
    monkeypatch.setattr(
        native_install, "_run_subprocess_cancellable",
        lambda args, timeout: (0, "clap import ok", "", False),
    )

    downloaded: dict = {}

    def _fake_download(urls, dest, label, on_progress=None):
        downloaded["url"] = urls[0]
        Path(dest).write_bytes(b"x" * 64)

    import vibechek.analyzer as analyzer_mod
    monkeypatch.setattr(analyzer_mod, "_download_from_mirrors", _fake_download)
    # The setup verifies the download against the real checkpoint pin; point
    # the pin at the fake bytes so the flow-under-test proceeds.
    import hashlib

    import vibechek.clap_genre as clap_mod
    monkeypatch.setattr(
        clap_mod, "_CHECKPOINT_SHA256", hashlib.sha256(b"x" * 64).hexdigest(),
    )

    out = native_install.setup_clap_native()

    assert out["ok"] is True, out
    # First pip call tries the CPU wheel index (CLAP is pinned to CPU).
    assert "--index-url" in pip_calls[0]
    assert "torch" in pip_calls[0]
    assert any("laion-clap" in c for c in pip_calls[1])
    assert "huggingface.co" in downloaded["url"]
    # The URL must reference an immutable revision, never the mutable `main`
    # ref (the checkpoint is a torch pickle — executable on load).
    assert "/resolve/main/" not in downloaded["url"]
    assert (tmp_path / ".vibechek" / "clap" / "music_clap.pt").is_file()


def test_setup_clap_native_rejects_checkpoint_with_wrong_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A downloaded checkpoint whose SHA256 doesn't match the pin must fail the
    setup AND be deleted — a torch pickle is executable on load, so a size
    floor alone must never admit it."""
    _genre_env(tmp_path, monkeypatch)
    monkeypatch.setattr(native_install, "_CLAP_MIN_CKPT_BYTES", 10)
    monkeypatch.setattr(
        native_install, "_run_with_progress", lambda *a, **k: (0, []),
    )
    monkeypatch.setattr(
        native_install, "_run_subprocess_cancellable",
        lambda args, timeout: (0, "clap import ok", "", False),
    )

    import vibechek.analyzer as analyzer_mod
    monkeypatch.setattr(
        analyzer_mod, "_download_from_mirrors",
        lambda urls, dest, label, on_progress=None: Path(dest).write_bytes(b"evil" * 16),
    )
    # Real pin left in place — the fake bytes cannot match it.

    out = native_install.setup_clap_native()
    assert out["ok"] is False
    assert "sha256" in out["error"].lower()
    assert not (tmp_path / ".vibechek" / "clap" / "music_clap.pt").exists()


def test_setup_clap_native_reuses_existing_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _genre_env(tmp_path, monkeypatch)
    monkeypatch.setattr(native_install, "_CLAP_MIN_CKPT_BYTES", 10)
    ckpt = tmp_path / ".vibechek" / "clap" / "music_clap.pt"
    ckpt.parent.mkdir(parents=True)
    ckpt.write_bytes(b"y" * 64)

    monkeypatch.setattr(
        native_install, "_run_with_progress", lambda *a, **k: (0, []),
    )
    monkeypatch.setattr(
        native_install, "_run_subprocess_cancellable",
        lambda args, timeout: (0, "clap import ok", "", False),
    )

    def _must_not_download(*a, **k):  # pragma: no cover - the assertion IS the test
        raise AssertionError("checkpoint re-downloaded despite a valid cached file")

    import vibechek.analyzer as analyzer_mod
    monkeypatch.setattr(analyzer_mod, "_download_from_mirrors", _must_not_download)

    out = native_install.setup_clap_native()
    assert out["ok"] is True, out


def test_setup_clap_native_falls_back_to_default_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Some platforms lack cpu-index wheels — the plain-index retry must run."""
    _genre_env(tmp_path, monkeypatch)
    monkeypatch.setattr(native_install, "_CLAP_MIN_CKPT_BYTES", 10)

    pip_calls: list[list[str]] = []

    def _fake_run_with_progress(args, on_progress, timeout, env=None):
        pip_calls.append(list(args))
        # Fail ONLY the cpu-index torch attempt.
        return (1, ["no matching distribution"]) if "--index-url" in args else (0, [])

    monkeypatch.setattr(native_install, "_run_with_progress", _fake_run_with_progress)
    monkeypatch.setattr(
        native_install, "_run_subprocess_cancellable",
        lambda args, timeout: (0, "clap import ok", "", False),
    )

    import vibechek.analyzer as analyzer_mod
    monkeypatch.setattr(
        analyzer_mod, "_download_from_mirrors",
        lambda urls, dest, label, on_progress=None: Path(dest).write_bytes(b"x" * 64),
    )
    import hashlib

    import vibechek.clap_genre as clap_mod
    monkeypatch.setattr(
        clap_mod, "_CHECKPOINT_SHA256", hashlib.sha256(b"x" * 64).hexdigest(),
    )

    out = native_install.setup_clap_native()
    assert out["ok"] is True, out
    torch_calls = [c for c in pip_calls if "torch" in c]
    assert len(torch_calls) == 2  # cpu-index attempt + plain-index fallback
    assert "--index-url" in torch_calls[0]
    assert "--index-url" not in torch_calls[1]


def test_setup_resolver_native_requires_engine_venv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _genre_env(tmp_path, monkeypatch, venv_exists=False)
    out = native_install.setup_resolver_native()
    assert out["ok"] is False
    assert "engine setup" in out["error"]


def test_setup_resolver_native_reuses_installed_ollama_and_pulls_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With ~/ollama/bin/ollama already present: no download, server ensured,
    `ollama pull <model>` run with OLLAMA_HOST pinned."""
    _genre_env(tmp_path, monkeypatch)
    ollama_bin = tmp_path / "ollama" / "bin" / "ollama"
    ollama_bin.parent.mkdir(parents=True)
    ollama_bin.write_text("#!/bin/sh\n")

    pulls: list[tuple[list[str], dict | None]] = []

    def _fake_run_with_progress(args, on_progress, timeout, env=None):
        pulls.append((list(args), env))
        return 0, ["success"]

    monkeypatch.setattr(native_install, "_run_with_progress", _fake_run_with_progress)

    import vibechek.genre_web as genre_web_mod
    monkeypatch.setattr(genre_web_mod, "ensure_backend", lambda *a, **k: True)

    def _must_not_download(*a, **k):  # pragma: no cover
        raise AssertionError("ollama re-downloaded despite an existing install")

    import vibechek.analyzer as analyzer_mod
    monkeypatch.setattr(analyzer_mod, "_download_from_mirrors", _must_not_download)

    out = native_install.setup_resolver_native(model="qwen2.5:0.5b")
    assert out["ok"] is True, out
    pull_call = next(c for c, _env in pulls if "pull" in c)
    assert "qwen2.5:0.5b" in pull_call
    pull_env = next(env for c, env in pulls if "pull" in c)
    assert pull_env is not None and pull_env.get("OLLAMA_HOST") == "127.0.0.1:11434"


def test_setup_resolver_native_fails_when_server_never_comes_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _genre_env(tmp_path, monkeypatch)
    ollama_bin = tmp_path / "ollama" / "bin" / "ollama"
    ollama_bin.parent.mkdir(parents=True)
    ollama_bin.write_text("#!/bin/sh\n")

    monkeypatch.setattr(native_install, "_run_with_progress", lambda *a, **k: (0, []))

    import vibechek.genre_web as genre_web_mod
    monkeypatch.setattr(genre_web_mod, "ensure_backend", lambda *a, **k: False)

    out = native_install.setup_resolver_native()
    assert out["ok"] is False
    assert "server" in out["error"].lower()


@pytest.mark.parametrize(
    ("is_mac", "machine", "expected_fragment", "expected_kind"),
    [
        (True, "arm64", "ollama-darwin.tgz", "tgz"),
        (False, "x86_64", "ollama-linux-amd64.tar.zst", "tar.zst"),
        (False, "aarch64", "ollama-linux-arm64.tar.zst", "tar.zst"),
    ],
)
def test_ollama_tarball_picks_platform_asset(
    monkeypatch: pytest.MonkeyPatch,
    is_mac: bool,
    machine: str,
    expected_fragment: str,
    expected_kind: str,
) -> None:
    import platform as platform_stdlib

    monkeypatch.setattr(native_install, "IS_MAC", is_mac)
    monkeypatch.setattr(platform_stdlib, "machine", lambda: machine)

    url, kind, sha256 = native_install._ollama_tarball()
    assert expected_fragment in url
    assert kind == expected_kind
    # The pin must match the WSL setup's release so both paths install the
    # same build.
    from vibechek.wsl import _OLLAMA_RELEASE, _OLLAMA_TARBALL_SHA256
    assert _OLLAMA_RELEASE in url
    # Every selectable asset must carry a content pin — the tarball is
    # unpacked and executed, so an unpinned platform would silently skip
    # verification.
    assert sha256 == _OLLAMA_TARBALL_SHA256[expected_fragment]
    assert sha256 and len(sha256) == 64
