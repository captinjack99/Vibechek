"""Tests for vibechek.wsl.

Focus on the pure logic: path translation, distro list parsing, _shell_quote,
WSLStatus computed properties, _wsl_run decoding. Real subprocess calls are
mocked so these tests run anywhere — Linux, macOS, or Windows.
"""

from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

from vibechek.wsl import (
    DistroInfo,
    WSLStatus,
    _parse_distro_list,
    _shell_quote,
    _wsl_run,
    to_dict,
    win_to_wsl_path,
    wsl_to_win_path,
)


# ---------------------------------------------------------------------------
# Path translation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "win,wsl",
    [
        ("C:\\Users\\Jack\\Music", "/mnt/c/Users/Jack/Music"),
        ("C:/Users/Jack/Music", "/mnt/c/Users/Jack/Music"),
        ("D:\\Tracks", "/mnt/d/Tracks"),
        ("z:\\foo\\bar\\baz", "/mnt/z/foo/bar/baz"),
    ],
)
def test_win_to_wsl_path_translates(win: str, wsl: str) -> None:
    assert win_to_wsl_path(win) == wsl


def test_win_to_wsl_path_passthrough_for_non_windows() -> None:
    # Already WSL-shaped or not a drive path → return as-is
    assert win_to_wsl_path("/mnt/c/foo") == "/mnt/c/foo"
    assert win_to_wsl_path("/home/user") == "/home/user"
    assert win_to_wsl_path("") == ""


@pytest.mark.parametrize(
    "wsl,win",
    [
        ("/mnt/c/Users/Jack/Music", "C:\\Users\\Jack\\Music"),
        ("/mnt/d/Tracks", "D:\\Tracks"),
        ("/mnt/c", "C:\\"),
        ("/mnt/c/", "C:\\"),
    ],
)
def test_wsl_to_win_path_translates(wsl: str, win: str) -> None:
    assert wsl_to_win_path(wsl) == win


def test_wsl_to_win_path_passthrough() -> None:
    assert wsl_to_win_path("/home/user") == "/home/user"
    assert wsl_to_win_path("") == ""


def test_path_translation_round_trip() -> None:
    original = "C:\\Users\\Jack\\Music\\track.mp3"
    assert wsl_to_win_path(win_to_wsl_path(original)) == original


# ---------------------------------------------------------------------------
# _shell_quote
# ---------------------------------------------------------------------------


def test_shell_quote_empty() -> None:
    assert _shell_quote("") == "''"


@pytest.mark.parametrize(
    "raw",
    ["foo", "abc123", "path/to/file", "key=value", "a-b_c.d:e"],
)
def test_shell_quote_unquoted_safe_chars(raw: str) -> None:
    # Safe chars (alnum, _ . / = : -) don't need quoting
    assert _shell_quote(raw) == raw


def test_shell_quote_wraps_special_chars() -> None:
    quoted = _shell_quote("hello world")
    assert quoted == "'hello world'"


def test_shell_quote_escapes_single_quotes() -> None:
    # The POSIX trick: end the quote, escaped-quote literal, reopen quote.
    quoted = _shell_quote("it's")
    assert quoted == "'it'\"'\"'s'"


def test_shell_quote_handles_dollar_and_backtick() -> None:
    quoted = _shell_quote("$HOME")
    assert quoted.startswith("'") and quoted.endswith("'")
    assert "$HOME" in quoted


# ---------------------------------------------------------------------------
# _parse_distro_list
# ---------------------------------------------------------------------------


def test_parse_distro_list_typical_output() -> None:
    stdout = (
        "  NAME                STATE     VERSION\n"
        "* Ubuntu-24.04        Running   2\n"
        "  Debian              Stopped   2\n"
    )
    distros = _parse_distro_list(stdout)
    assert len(distros) == 2

    ubuntu = distros[0]
    assert ubuntu.name == "Ubuntu-24.04"
    assert ubuntu.state == "Running"
    assert ubuntu.version == "2"
    assert ubuntu.is_default is True

    debian = distros[1]
    assert debian.name == "Debian"
    assert debian.state == "Stopped"
    assert debian.is_default is False


def test_parse_distro_list_no_distros() -> None:
    # Header only
    assert _parse_distro_list("  NAME  STATE  VERSION\n") == []


def test_parse_distro_list_empty_string() -> None:
    assert _parse_distro_list("") == []


def test_parse_distro_list_partial_row() -> None:
    # If a row has fewer than 3 fields, we still capture the name.
    stdout = "  NAME  STATE  VERSION\n  Ubuntu-24.04\n"
    distros = _parse_distro_list(stdout)
    assert len(distros) == 1
    assert distros[0].name == "Ubuntu-24.04"


# ---------------------------------------------------------------------------
# WSLStatus computed properties
# ---------------------------------------------------------------------------


def test_wsl_status_can_run_vibechek_true_when_both_installed() -> None:
    s = WSLStatus(
        is_windows=True,
        wsl_available=True,
        wsl_feature_enabled=True,
        distros=[
            DistroInfo(name="Ubuntu-24.04", vibechek_installed=True, essentia_installed=True),
        ],
    )
    assert s.can_run_vibechek is True


def test_wsl_status_can_run_vibechek_false_when_only_one_installed() -> None:
    s = WSLStatus(
        is_windows=True,
        wsl_available=True,
        wsl_feature_enabled=True,
        distros=[
            DistroInfo(name="Ubuntu-24.04", vibechek_installed=True, essentia_installed=False),
        ],
    )
    assert s.can_run_vibechek is False


def test_wsl_status_can_run_vibechek_empty_distros() -> None:
    s = WSLStatus(is_windows=True, wsl_available=True, wsl_feature_enabled=True)
    assert s.can_run_vibechek is False


def test_wsl_status_usable_distro_prefers_default() -> None:
    s = WSLStatus(
        is_windows=True,
        wsl_available=True,
        wsl_feature_enabled=True,
        distros=[
            DistroInfo(name="Debian", vibechek_installed=True, essentia_installed=True),
            DistroInfo(
                name="Ubuntu-24.04",
                vibechek_installed=True,
                essentia_installed=True,
                is_default=True,
            ),
        ],
    )
    assert s.usable_distro == "Ubuntu-24.04"


def test_wsl_status_usable_distro_falls_back_to_first_ready() -> None:
    s = WSLStatus(
        is_windows=True,
        wsl_available=True,
        wsl_feature_enabled=True,
        distros=[
            DistroInfo(name="Debian", vibechek_installed=True, essentia_installed=True),
            DistroInfo(name="Ubuntu-24.04", vibechek_installed=False, essentia_installed=False),
        ],
    )
    assert s.usable_distro == "Debian"


def test_wsl_status_usable_distro_none_when_no_ready() -> None:
    s = WSLStatus(
        is_windows=True,
        wsl_available=True,
        wsl_feature_enabled=True,
        distros=[DistroInfo(name="Ubuntu-24.04")],
    )
    assert s.usable_distro is None


# ---------------------------------------------------------------------------
# to_dict
# ---------------------------------------------------------------------------


def test_to_dict_includes_computed_props() -> None:
    s = WSLStatus(
        is_windows=True,
        wsl_available=True,
        wsl_feature_enabled=True,
        distros=[
            DistroInfo(
                name="Ubuntu-24.04",
                vibechek_installed=True,
                essentia_installed=True,
                is_default=True,
            ),
        ],
    )
    d = to_dict(s)
    assert d["can_run_vibechek"] is True
    assert d["usable_distro"] == "Ubuntu-24.04"
    assert d["is_windows"] is True
    assert len(d["distros"]) == 1


# ---------------------------------------------------------------------------
# _wsl_run encoding handling
# ---------------------------------------------------------------------------


def test_wsl_run_decodes_utf16le_output() -> None:
    """wsl.exe outputs UTF-16 LE — _wsl_run should strip the BOM and null bytes."""
    fake_stdout = "Hello".encode("utf-16-le")
    fake = subprocess.CompletedProcess([], 0, fake_stdout, b"")
    with patch("vibechek.wsl.subprocess.run", return_value=fake):
        result = _wsl_run(["wsl", "--status"])
    assert result.stdout == "Hello"
    assert result.returncode == 0


def test_wsl_run_handles_utf8_output() -> None:
    fake = subprocess.CompletedProcess([], 0, b"plain utf-8", b"")
    with patch("vibechek.wsl.subprocess.run", return_value=fake):
        result = _wsl_run(["wsl", "--status"])
    # utf-16-le of "plain utf-8" happens to decode to garbage, but we tolerate;
    # the important thing is decoding doesn't crash.
    assert isinstance(result.stdout, str)


def test_wsl_run_returns_returncode() -> None:
    fake = subprocess.CompletedProcess([], 1, b"", b"")
    with patch("vibechek.wsl.subprocess.run", return_value=fake):
        result = _wsl_run(["wsl", "--status"])
    assert result.returncode == 1


# ---------------------------------------------------------------------------
# detect_wsl — only the no-op branch on non-Windows; mock on Windows path
# ---------------------------------------------------------------------------


def test_detect_wsl_non_windows_returns_empty_status(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("vibechek.wsl.IS_WINDOWS", False)
    from vibechek.wsl import detect_wsl

    status = detect_wsl()
    assert status.is_windows is False
    assert status.wsl_available is False
    assert status.distros == []


def test_detect_wsl_missing_wsl_exe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("vibechek.wsl.IS_WINDOWS", True)
    monkeypatch.setattr("vibechek.wsl.shutil.which", lambda _name: None)
    from vibechek.wsl import detect_wsl

    status = detect_wsl()
    assert status.is_windows is True
    assert status.wsl_available is False


def test_detect_wsl_status_feature_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """If wsl --status returns non-zero, feature is disabled."""
    monkeypatch.setattr("vibechek.wsl.IS_WINDOWS", True)
    monkeypatch.setattr("vibechek.wsl.shutil.which", lambda _name: "C:\\Windows\\wsl.exe")
    fake = subprocess.CompletedProcess([], 1, "", "")
    monkeypatch.setattr("vibechek.wsl._wsl_run", lambda *_a, **_k: fake)
    from vibechek.wsl import detect_wsl

    status = detect_wsl(quick=True)
    assert status.wsl_available is True
    assert status.wsl_feature_enabled is False


def test_detect_wsl_quick_mode_skips_probes(monkeypatch: pytest.MonkeyPatch) -> None:
    """quick=True should never call _probe_distro."""
    monkeypatch.setattr("vibechek.wsl.IS_WINDOWS", True)
    monkeypatch.setattr("vibechek.wsl.shutil.which", lambda _name: "C:\\Windows\\wsl.exe")

    status_output = subprocess.CompletedProcess([], 0, "ok", "")
    list_output = subprocess.CompletedProcess(
        [],
        0,
        "  NAME       STATE    VERSION\n* Ubuntu-24.04  Running  2\n",
        "",
    )
    calls: list[tuple] = []

    def fake_run(cmd, **kwargs):
        calls.append(tuple(cmd))
        if "--status" in cmd:
            return status_output
        if "--list" in cmd:
            return list_output
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr("vibechek.wsl._wsl_run", fake_run)

    probe_called = []
    monkeypatch.setattr(
        "vibechek.wsl._probe_distro",
        lambda *_a, **_k: probe_called.append(True),
    )

    from vibechek.wsl import detect_wsl
    status = detect_wsl(quick=True)
    assert status.wsl_feature_enabled is True
    assert status.default_distro == "Ubuntu-24.04"
    assert probe_called == []


# ---------------------------------------------------------------------------
# Path-translation edge cases (paths the user might actually have)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "win,wsl",
    [
        # Spaces — common in Windows user paths like "My Drive"
        ("C:\\Users\\Jack\\My Drive\\Vibechek", "/mnt/c/Users/Jack/My Drive/Vibechek"),
        # Mixed separators
        ("C:\\a/mixed\\slashes.mp3", "/mnt/c/a/mixed/slashes.mp3"),
        # Special chars that don't need quoting in paths themselves
        ("C:\\Path with (parens) & chars!.mp3", "/mnt/c/Path with (parens) & chars!.mp3"),
        # Drive root with backslash
        ("C:\\", "/mnt/c/"),
    ],
)
def test_win_to_wsl_edge_cases(win: str, wsl: str) -> None:
    assert win_to_wsl_path(win) == wsl


def test_win_to_wsl_drive_only_passes_through() -> None:
    # "C:" without a separator is ambiguous (CWD on drive). We leave it alone.
    assert win_to_wsl_path("C:") == "C:"


def test_win_to_wsl_unc_paths_pass_through() -> None:
    # UNC paths (\\server\share\...) don't have a clean /mnt/c/-style equivalent
    # in WSL. We leave them alone rather than producing a broken path.
    assert win_to_wsl_path(r"\\server\share\file.mp3") == r"\\server\share\file.mp3"


def test_round_trip_paths_with_spaces() -> None:
    original = "C:\\Users\\Jack\\My Drive\\Music\\track 1.mp3"
    assert wsl_to_win_path(win_to_wsl_path(original)) == original


def test_round_trip_paths_with_non_ascii() -> None:
    # Non-ASCII paths (umlauts, Polish, etc.) round-trip cleanly.
    original = "D:\\Müsik\\trąck.mp3"
    assert wsl_to_win_path(win_to_wsl_path(original)) == original


# ---------------------------------------------------------------------------
# Engine GPU probe — native (no WSL)
# ---------------------------------------------------------------------------


def test_engine_gpu_native_returns_native_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    """On non-Windows hosts, distro=None falls back to native resources.detect()."""
    from vibechek.wsl import EngineGpuInfo, probe_engine_gpu

    # Bypass the cache so the test is deterministic
    monkeypatch.setattr("vibechek.wsl._ENGINE_GPU_CACHE", {})

    # Force a clean reply from resources.detect()
    from vibechek import resources

    fake = resources.SystemResources(
        platform="Linux-test",
        cpu_count=8,
        memory_total_mb=16384,
        memory_available_mb=8192,
        gpu_available=False,
        gpu_devices=[],
        cuda_runtime=None,
    )
    monkeypatch.setattr(resources, "detect", lambda: fake)

    info = probe_engine_gpu(None, force=True)
    assert isinstance(info, EngineGpuInfo)
    assert info.engine == "native"
    assert info.ok is True
    assert info.gpu_available is False
    assert info.devices == []


def test_engine_gpu_native_reports_devices(monkeypatch: pytest.MonkeyPatch) -> None:
    from vibechek.wsl import probe_engine_gpu
    from vibechek import resources

    monkeypatch.setattr("vibechek.wsl._ENGINE_GPU_CACHE", {})

    fake = resources.SystemResources(
        platform="Linux-test",
        cpu_count=8,
        memory_total_mb=16384,
        memory_available_mb=8192,
        gpu_available=True,
        gpu_devices=[
            resources.GpuDevice(name="NVIDIA GeForce RTX 4070", backend="cuda", memory_mb=8192),
        ],
        cuda_runtime="535.183",
    )
    monkeypatch.setattr(resources, "detect", lambda: fake)

    info = probe_engine_gpu(None, force=True)
    assert info.engine == "native"
    assert info.ok is True
    assert info.gpu_available is True
    assert info.gpu_count == 1
    assert info.devices[0].name == "NVIDIA GeForce RTX 4070"
    assert info.devices[0].backend == "cuda"
    assert info.nvidia_driver == "535.183"
    assert info.nvidia_smi_available is True


def test_engine_gpu_caches_results(monkeypatch: pytest.MonkeyPatch) -> None:
    """Second call within TTL hits the cache."""
    from vibechek.wsl import probe_engine_gpu
    from vibechek import resources

    monkeypatch.setattr("vibechek.wsl._ENGINE_GPU_CACHE", {})

    call_count = {"n": 0}

    def counted_detect():
        call_count["n"] += 1
        return resources.SystemResources(
            platform="x", cpu_count=1, memory_total_mb=None,
            memory_available_mb=None, gpu_available=False, gpu_devices=[],
            cuda_runtime=None,
        )

    monkeypatch.setattr(resources, "detect", counted_detect)

    probe_engine_gpu(None)
    probe_engine_gpu(None)  # should hit cache
    probe_engine_gpu(None)  # should hit cache
    assert call_count["n"] == 1


def test_engine_gpu_force_bypasses_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    from vibechek.wsl import probe_engine_gpu
    from vibechek import resources

    monkeypatch.setattr("vibechek.wsl._ENGINE_GPU_CACHE", {})

    call_count = {"n": 0}

    def counted_detect():
        call_count["n"] += 1
        return resources.SystemResources(
            platform="x", cpu_count=1, memory_total_mb=None,
            memory_available_mb=None, gpu_available=False, gpu_devices=[],
            cuda_runtime=None,
        )

    monkeypatch.setattr(resources, "detect", counted_detect)

    probe_engine_gpu(None)
    probe_engine_gpu(None, force=True)  # bypasses cache
    assert call_count["n"] == 2


def test_engine_gpu_info_to_dict_is_jsonable() -> None:
    """The wire form must round-trip through json so the GUI can read it."""
    import json
    from vibechek.wsl import EngineGpuDevice, EngineGpuInfo, engine_gpu_info_to_dict

    info = EngineGpuInfo(
        engine="wsl",
        distro="Ubuntu-24.04",
        ok=True,
        gpu_available=True,
        gpu_count=1,
        devices=[EngineGpuDevice(name="RTX 4070", backend="cuda", compute_capability="8.9")],
        tf_version="2.5.0",
        tf_built_with_cuda=True,
        nvidia_driver="591.94",
        nvidia_smi_available=True,
        probed_at=1234567.0,
    )
    d = engine_gpu_info_to_dict(info)
    # Must be JSON-serializable
    s = json.dumps(d)
    rt = json.loads(s)
    assert rt["engine"] == "wsl"
    assert rt["distro"] == "Ubuntu-24.04"
    assert rt["devices"][0]["name"] == "RTX 4070"
    assert rt["devices"][0]["compute_capability"] == "8.9"
    # New fields added when we discovered the "hardware-visible but missing libs" state
    assert "gpu_hardware_visible" in rt
    assert "missing_cuda_libs" in rt


def test_engine_gpu_info_distinguishes_skipped_from_available() -> None:
    """The data model captures the three states we care about."""
    from vibechek.wsl import EngineGpuDevice, EngineGpuInfo

    # State: HW visible but TF skipped registering (missing CUDA libs)
    skipped = EngineGpuInfo(
        engine="wsl",
        distro="Ubuntu",
        ok=True,  # probe succeeded
        gpu_available=False,  # TF won't use it
        gpu_hardware_visible=True,
        missing_cuda_libs=["libcudnn.so.8", "libcublas.so.11"],
        nvidia_driver="591.94",
        nvidia_smi_available=True,
    )
    assert skipped.ok is True
    assert skipped.gpu_available is False
    assert skipped.gpu_hardware_visible is True
    assert len(skipped.missing_cuda_libs) == 2

    # State: GPU fully usable
    usable = EngineGpuInfo(
        engine="native", ok=True, gpu_available=True,
        gpu_hardware_visible=True, gpu_count=1,
        devices=[EngineGpuDevice(name="RTX 4070", backend="cuda")],
    )
    assert usable.gpu_available is True
    assert not usable.missing_cuda_libs

    # State: No GPU at all
    none = EngineGpuInfo(engine="native", ok=True)
    assert none.gpu_available is False
    assert none.gpu_hardware_visible is False
    assert not none.missing_cuda_libs


# ---------------------------------------------------------------------------
# CUDA library installer translation
# ---------------------------------------------------------------------------


def test_cuda_libs_translation_to_apt_packages() -> None:
    """Every lib in the typical 'missing' set should map to a known apt pkg."""
    from vibechek.wsl import _CUDA_APT_PACKAGES_BY_LIB

    # The libs essentia's bundled TF 2.5 tries to dlopen on a fresh WSL Ubuntu
    typical_missing = [
        "libcublas.so.11", "libcublasLt.so.11",
        "libcufft.so.10", "libcusparse.so.11", "libcudnn.so.8",
    ]
    packages: set[str] = set()
    for lib in typical_missing:
        assert lib in _CUDA_APT_PACKAGES_BY_LIB, f"{lib} has no apt mapping"
        packages.update(_CUDA_APT_PACKAGES_BY_LIB[lib])
    # Critical libs all map to *something* installable
    assert "libcudnn8" in packages
    assert any("libcublas" in p for p in packages)


def test_cuda_lib_map_prefers_meta_package() -> None:
    """All CUDA toolkit libs should try `cuda-libraries-11-8` first.

    Per-lib packages like `libcufft-11-8` aren't reachable on some
    Ubuntu/keyring combinations (a real user hit `E: Unable to locate
    package libcufft-11-8`). The NVIDIA meta-package pulls in everything
    in one shot and is always present in the cuda-keyring repo.
    """
    from vibechek.wsl import _CUDA_APT_PACKAGES_BY_LIB

    toolkit_libs = [
        "libcublas.so.11", "libcublasLt.so.11",
        "libcufft.so.10", "libcurand.so.10",
        "libcusolver.so.11", "libcusparse.so.11",
    ]
    for lib in toolkit_libs:
        fallbacks = _CUDA_APT_PACKAGES_BY_LIB[lib]
        assert fallbacks[0] == "cuda-libraries-11-8", \
            f"{lib} should try cuda-libraries-11-8 first; got {fallbacks}"


def test_install_cuda_libs_in_wsl_no_packages_for_unknown_libs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the user passes libs we don't know how to install, return a useful error."""
    from vibechek.wsl import install_cuda_libs_in_wsl

    # Pretend we're on Windows so the early non-Windows return doesn't fire
    monkeypatch.setattr("vibechek.wsl.IS_WINDOWS", True)
    monkeypatch.setattr("vibechek.wsl.shutil.which", lambda _: "C:\\fake\\wsl.exe")

    result = install_cuda_libs_in_wsl("Ubuntu", ["libimaginary.so.99"])
    assert result["ok"] is False
    assert "libimaginary.so.99" in result["error"]


def test_libcudnn_has_fallback_packages() -> None:
    """libcudnn8 should have fallbacks because it's not in NVIDIA's CUDA repo.

    The user's bug: CUDA install exits 100 because libcudnn8 isn't in the
    cuda-keyring repo. We now try nvidia-cudnn (Ubuntu multiverse) as a
    fallback.
    """
    from vibechek.wsl import _CUDA_APT_PACKAGES_BY_LIB
    cudnn_packages = _CUDA_APT_PACKAGES_BY_LIB["libcudnn.so.8"]
    assert len(cudnn_packages) >= 2, \
        f"libcudnn.so.8 needs fallback packages (NVIDIA CUDA repo doesn't ship it). Got: {cudnn_packages}"
    assert "libcudnn8" in cudnn_packages
    assert "nvidia-cudnn" in cudnn_packages


def test_build_try_chain_produces_valid_bash() -> None:
    """The bash try-chain generator emits something we can sanity-check."""
    from vibechek.wsl import _build_try_chain

    chain, packages = _build_try_chain(
        ["libcudnn.so.8", "libcublas.so.11"]
    )
    # Each lib should produce an apt-get install line per fallback
    assert "apt-get install" in chain
    assert "libcudnn8" in chain
    assert "nvidia-cudnn" in chain  # fallback
    assert "libcublas-11-8" in chain
    # The bash variables we use for tracking installed vs failed
    assert "INSTALLED_THIS" in chain
    assert "FAILED_LIBS" in chain
    # All packages we'd attempt
    assert "libcudnn8" in packages
    assert "libcublas-11-8" in packages


def test_build_try_chain_handles_unknown_libs_gracefully() -> None:
    """Unknown libs get a WARN line, not an exception."""
    from vibechek.wsl import _build_try_chain

    chain, packages = _build_try_chain(["libnotreal.so.999", "libcublas.so.11"])
    assert "WARN" in chain
    assert "libnotreal.so.999" in chain
    # Known libs still produce install commands
    assert "libcublas-11-8" in chain
    # Unknown lib's "package" isn't in the set
    assert not any("libnotreal" in p for p in packages)


# ---------------------------------------------------------------------------
# Native (Linux/macOS) install module
# ---------------------------------------------------------------------------


def test_native_venv_probe_reports_supported_on_unix(monkeypatch: pytest.MonkeyPatch) -> None:
    """probe_native_venv reports supported=True on Linux/macOS, False on Windows."""
    from vibechek import native_install

    # Force the supported flag for the test
    monkeypatch.setattr(native_install, "IS_SUPPORTED", True)
    status = native_install.probe_native_venv()
    assert status.supported is True
    assert status.venv_dir.endswith(".vibechek/venv") or status.venv_dir.endswith(".vibechek\\venv")
    # On a clean test sandbox, neither vibechek nor essentia is installed
    assert status.vibechek_installed is False
    assert status.essentia_installed is False


def test_native_venv_probe_returns_supported_false_on_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from vibechek import native_install

    monkeypatch.setattr(native_install, "IS_SUPPORTED", False)
    status = native_install.probe_native_venv()
    assert status.supported is False
    assert status.essentia_installed is False
    assert status.vibechek_installed is False


def test_install_essentia_native_unsupported_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    """install_essentia_native returns a clean error on unsupported platforms."""
    from vibechek import native_install

    monkeypatch.setattr(native_install, "IS_SUPPORTED", False)
    result = native_install.install_essentia_native()
    assert result["ok"] is False
    assert "not supported" in result["error"].lower()


def test_native_venv_status_to_dict_is_jsonable() -> None:
    """The wire form round-trips through json so the GUI can read it."""
    import json
    from vibechek.native_install import NativeVenvStatus, to_dict

    status = NativeVenvStatus(
        supported=True,
        venv_dir="/home/user/.vibechek/venv",
        venv_python="/home/user/.vibechek/venv/bin/python",
        venv_vibechek="/home/user/.vibechek/venv/bin/vibechek",
        essentia_installed=True,
        essentia_version="2.1b6.dev1110",
        vibechek_installed=True,
        vibechek_version="0.3.0-beta.1",
    )
    d = to_dict(status)
    s = json.dumps(d)
    rt = json.loads(s)
    assert rt["essentia_installed"] is True
    assert rt["vibechek_version"] == "0.3.0-beta.1"
