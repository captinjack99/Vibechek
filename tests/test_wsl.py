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
    UnsupportedWslPathError,
    WSLStatus,
    _detect_wsl_encoding_order,
    _parse_distro_list,
    _shell_quote,
    _user_bootstrap,
    _wsl_run,
    to_dict,
    win_to_wsl_path,
    wsl_to_win_path,
)


@pytest.mark.parametrize("engine", ["essentia_tf", "onnx"])
def test_user_bootstrap_force_reinstalls_vibechek(engine: str) -> None:
    """Re-running 'Set up WSL' must clear version drift. A plain
    `pip install --upgrade git+...` does NOT re-pull a VCS install once a
    version is present, so the bootstrap must force-reinstall the package.
    Regression: 'Set up now' left a stale beta.9 in WSL while the drift error
    told the user to do exactly that."""
    script = _user_bootstrap(engine)
    assert "git+https://github.com/papapew/Vibechek.git" in script
    assert "--force-reinstall" in script, (
        f"{engine} bootstrap must force-reinstall vibechek to fix drift on re-run"
    )
    # Force-reinstall is scoped to the package (deps handled by the prior line)
    # so a re-run doesn't drag essentia-tensorflow down again.
    assert "--no-deps" in script

# ---------------------------------------------------------------------------
# Path translation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "win,wsl",
    [
        ("C:\\Users\\dj\\Music", "/mnt/c/Users/dj/Music"),
        ("C:/Users/dj/Music", "/mnt/c/Users/dj/Music"),
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
        ("/mnt/c/Users/dj/Music", "C:\\Users\\dj\\Music"),
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
    original = "C:\\Users\\dj\\Music\\track.mp3"
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


def test_parse_distro_list_spaced_distro_name() -> None:
    # A distro NAME can contain spaces ("Ubuntu 22.04 LTS"). The trailing two
    # columns are always STATE and VERSION; everything before them is the name.
    stdout = (
        "  NAME                STATE     VERSION\n"
        "* Ubuntu 22.04 LTS    Running   2\n"
        "  Debian              Stopped   2\n"
    )
    distros = _parse_distro_list(stdout)
    assert len(distros) == 2

    spaced = distros[0]
    assert spaced.name == "Ubuntu 22.04 LTS"
    assert spaced.state == "Running"
    assert spaced.version == "2"
    assert spaced.is_default is True

    assert distros[1].name == "Debian"
    assert distros[1].state == "Stopped"
    assert distros[1].version == "2"


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
        ("C:\\Users\\dj\\My Drive\\Vibechek", "/mnt/c/Users/dj/My Drive/Vibechek"),
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


def test_win_to_wsl_unc_paths_raise() -> None:
    # UNC paths (\\server\share\...) don't have a /mnt/c/-style equivalent in
    # WSL (the share isn't mounted there), so we refuse them with a clear,
    # actionable error instead of passing a broken path through to analyze.
    with pytest.raises(UnsupportedWslPathError) as exc:
        win_to_wsl_path(r"\\server\share\file.mp3")
    msg = str(exc.value).lower()
    assert "network" in msg or "unc" in msg
    assert "local" in msg  # tells the user the fix: copy to a local drive
    # UnsupportedWslPathError must be a ValueError so existing handlers catch it.
    assert isinstance(exc.value, ValueError)


@pytest.mark.parametrize(
    "unc",
    [
        r"\\NAS\Music\track.mp3",
        r"\\192.168.1.10\share\a.flac",
        "//server/share/file.mp3",          # forward-slash UNC form
        r"\\?\UNC\server\share\file.mp3",    # long-path UNC form
    ],
)
def test_win_to_wsl_unc_variants_raise(unc: str) -> None:
    with pytest.raises(UnsupportedWslPathError):
        win_to_wsl_path(unc)


def test_win_to_wsl_longpath_drive_prefix_is_stripped() -> None:
    # `\\?\C:\...` is a long-path-prefixed *drive* path, NOT a UNC share. We
    # strip the prefix and translate it normally rather than rejecting it.
    assert win_to_wsl_path(r"\\?\C:\Music\track.mp3") == "/mnt/c/Music/track.mp3"


def test_win_to_wsl_strips_trailing_dot_and_space() -> None:
    # Windows silently drops trailing dots/spaces per path component; Linux
    # treats them literally, so the translated path must match Windows' view.
    assert win_to_wsl_path("C:\\Foo.\\Bar ") == "/mnt/c/Foo/Bar"
    assert win_to_wsl_path("C:\\Music \\track.mp3 ") == "/mnt/c/Music/track.mp3"


def test_round_trip_paths_with_spaces() -> None:
    original = "C:\\Users\\dj\\My Drive\\Music\\track 1.mp3"
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
    from vibechek import resources
    from vibechek.wsl import probe_engine_gpu

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
    from vibechek import resources
    from vibechek.wsl import probe_engine_gpu

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
    from vibechek import resources
    from vibechek.wsl import probe_engine_gpu

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


def test_cuda_bootstrap_does_not_mention_placeholders_in_comments() -> None:
    """Regression: a comment that mentioned `__TRY_CHAIN__` got its tail
    spliced into bash by str.replace(), producing `fi substitution.` as a
    literal token in the generated script (bash: line 158: syntax error
    near unexpected token `substitution.'). Comments must never mention
    the placeholder string by name.
    """
    from vibechek.wsl import _CUDA_LIBS_BOOTSTRAP

    # Find every occurrence of the placeholder and ensure each one is on a
    # line that's NOT a comment (i.e. the real substitution site).
    for i, line in enumerate(_CUDA_LIBS_BOOTSTRAP.splitlines(), 1):
        if "__TRY_CHAIN__" in line:
            stripped = line.lstrip()
            assert not stripped.startswith("#"), (
                f"Line {i} mentions __TRY_CHAIN__ in a comment: {line!r}. "
                f"str.replace() would splice the chain into the comment and "
                f"corrupt the script. Rename or remove the placeholder reference."
            )


def test_generated_cuda_script_parses_as_bash() -> None:
    """End-to-end: the script we generate is syntactically valid bash.

    Catches *any* future bash-generation bug (mis-balanced if/fi, unclosed
    quotes, accidental command-substitution). Skipped if we can't find a
    `bash` interpreter on the host — Windows CI runners often don't have one.
    """
    import shutil as _shutil
    import subprocess as _subprocess

    from vibechek.wsl import _CUDA_LIBS_BOOTSTRAP, _build_try_chain

    bash = _shutil.which("bash")
    if not bash:
        pytest.skip("bash not available on this host; skipping syntax check")

    chain, _ = _build_try_chain([
        "libcublas.so.11", "libcublasLt.so.11", "libcufft.so.10",
        "libcusparse.so.11", "libcudnn.so.8",
    ])
    script = _CUDA_LIBS_BOOTSTRAP.replace("__TRY_CHAIN__", chain, 1)

    # `bash -n` parses but doesn't execute. Non-zero rc = syntax error.
    result = _subprocess.run(
        [bash, "-n"],
        input=script.encode("utf-8"),
        capture_output=True, timeout=10,
    )
    assert result.returncode == 0, (
        f"Generated CUDA install script has a bash syntax error.\n"
        f"stderr: {result.stderr.decode('utf-8', errors='replace')}"
    )


def test_repair_wsl_shim_returns_not_windows_off_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    """repair_wsl_shim short-circuits on non-Windows hosts."""
    from vibechek.wsl import repair_wsl_shim
    monkeypatch.setattr("vibechek.wsl.IS_WINDOWS", False)
    result = repair_wsl_shim("Ubuntu")
    assert result["ok"] is False
    assert "windows" in result["error"].lower()


def test_repair_wsl_shim_handles_missing_wsl_exe(monkeypatch: pytest.MonkeyPatch) -> None:
    """If wsl.exe isn't on PATH, repair returns a clean error not an exception."""
    from vibechek.wsl import repair_wsl_shim
    monkeypatch.setattr("vibechek.wsl.IS_WINDOWS", True)
    monkeypatch.setattr("vibechek.wsl.shutil.which", lambda _: None)
    result = repair_wsl_shim("Ubuntu")
    assert result["ok"] is False
    assert "wsl.exe" in result["error"].lower()


def test_install_cuda_libs_bootstrap_does_not_patch_python_shim() -> None:
    """Regression: beta.6-9 inserted `. cuda-env.sh` into the venv's vibechek
    shim, which is a Python entry point — bash syntax inside Python = SyntaxError
    on every run. The bootstrap must NEVER contain code that mutates the shim
    in a way that injects shell syntax."""
    from vibechek.wsl import _CUDA_LIBS_PIP_BOOTSTRAP

    # The old (broken) pattern: `awk -v env=$ENV_FILE` followed by writes back
    # to the shim. The new bootstrap must contain NO awk-rewrite-into-shim block.
    # It IS allowed to grep-and-strip the line (the in-bootstrap repair).
    forbidden_patterns = [
        # Adding a sourcing line to a Python script — what bit us:
        "print \". \" env",
        "echo \". $ENV_FILE\" >>",
        "echo \"source",
    ]
    for pattern in forbidden_patterns:
        assert pattern not in _CUDA_LIBS_PIP_BOOTSTRAP, (
            f"CUDA bootstrap contains a forbidden shim-mutation pattern: "
            f"{pattern!r}. See the beta.10 bug — bash injected into a Python "
            f"entry point breaks every subsequent run with SyntaxError."
        )


def test_resolve_cuda_packages_routes_to_cu11_for_tf25_soname() -> None:
    """TF 2.5 (essentia's bundled version) dlopens libcublas.so.11, libcudnn.so.8.
    Those .so suffixes route to the cu11 wheel set."""
    from vibechek.wsl import _resolve_cuda_packages

    pkgs, unknown = _resolve_cuda_packages([
        "libcublas.so.11", "libcublasLt.so.11",
        "libcufft.so.10", "libcusparse.so.11", "libcudnn.so.8",
    ])
    assert unknown == []
    assert all(p.endswith("-cu11") for p in pkgs), \
        f"Expected all cu11 packages, got {pkgs}"
    assert "nvidia-cublas-cu11" in pkgs
    assert "nvidia-cudnn-cu11" in pkgs


def test_resolve_cuda_packages_routes_to_cu12_for_tf213_soname() -> None:
    """When essentia upgrades to TF 2.13+, it'll dlopen libcublas.so.12,
    libcudnn.so.9. Those route to cu12 wheels."""
    from vibechek.wsl import _resolve_cuda_packages

    pkgs, unknown = _resolve_cuda_packages([
        "libcublas.so.12", "libcudnn.so.9", "libcufft.so.11",
    ])
    assert unknown == []
    assert all(p.endswith("-cu12") for p in pkgs), \
        f"Expected all cu12 packages, got {pkgs}"


def test_resolve_cuda_packages_handles_unknown_libs() -> None:
    from vibechek.wsl import _resolve_cuda_packages
    pkgs, unknown = _resolve_cuda_packages(["libimaginary.so.99", "libcublas.so.11"])
    assert "nvidia-cublas-cu11" in pkgs
    assert "libimaginary.so.99" in unknown


def test_cuda_libs_have_pip_wheel_mapping() -> None:
    """Every CUDA lib essentia needs maps to a PyPI nvidia wheel.

    The pip wheels are the *primary* install path (apt is gone): they work on
    every Linux distribution without requiring NVIDIA's apt repo to be
    correctly registered.
    """
    from vibechek.wsl import _CUDA_PIP_PACKAGES_BY_LIB

    required = [
        "libcublas.so.11", "libcublasLt.so.11",
        "libcufft.so.10", "libcurand.so.10",
        "libcusolver.so.11", "libcusparse.so.11",
        "libcudnn.so.8",
    ]
    for lib in required:
        wheel = _CUDA_PIP_PACKAGES_BY_LIB.get(lib)
        assert wheel is not None, f"No pip wheel mapping for {lib}"
        assert wheel.startswith("nvidia-") and wheel.endswith("-cu11"), (
            f"Wheel name {wheel!r} doesn't follow the nvidia-<lib>-cu11 pattern"
        )


def test_cuda_pip_bootstrap_has_single_placeholder() -> None:
    """The __PACKAGES__ placeholder must appear exactly once in the bootstrap.

    We hit this bug in beta.5: the bootstrap had __PACKAGES__ in both the
    echo line and the pip install line, count=1 replace only fixed the first,
    pip got 'install __PACKAGES__' literally and errored out.
    """
    from vibechek.wsl import _CUDA_LIBS_PIP_BOOTSTRAP

    count = _CUDA_LIBS_PIP_BOOTSTRAP.count("__PACKAGES__")
    assert count == 1, (
        f"_CUDA_LIBS_PIP_BOOTSTRAP must have exactly 1 __PACKAGES__ "
        f"occurrence (so count=1 replace is safe); found {count}. "
        f"If you need it multiple times, set a bash variable once and reuse it."
    )


def test_generated_pip_cuda_script_parses_as_bash() -> None:
    """The new pip bootstrap is syntactically valid bash."""
    import shutil as _shutil
    import subprocess as _subprocess

    from vibechek.wsl import _CUDA_LIBS_PIP_BOOTSTRAP

    bash = _shutil.which("bash")
    if not bash:
        pytest.skip("bash not available on this host")

    script = _CUDA_LIBS_PIP_BOOTSTRAP.replace(
        "__PACKAGES__",
        "nvidia-cublas-cu11 nvidia-cudnn-cu11",
        1,
    )
    result = _subprocess.run(
        [bash, "-n"],
        input=script.encode("utf-8"),
        capture_output=True, timeout=10,
    )
    assert result.returncode == 0, (
        f"Pip CUDA bootstrap has a bash syntax error.\n"
        f"stderr: {result.stderr.decode('utf-8', errors='replace')}"
    )


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


def test_native_venv_probe_reports_supported_on_unix(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """probe_native_venv reports supported=True on Linux/macOS, False on Windows."""
    from vibechek import native_install

    # Force the supported flag for the test
    monkeypatch.setattr(native_install, "IS_SUPPORTED", True)
    # Point the probe at an empty tmp dir so the "nothing installed" assertions are
    # deterministic regardless of whether the dev machine happens to have a real
    # ~/.vibechek/venv (otherwise this flakes on any machine that does).
    monkeypatch.setattr(native_install, "VENV_DIR", tmp_path / ".vibechek" / "venv")
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


def test_native_install_run_with_progress_cancellation_terminates_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Setting the cancellation flag mid-run kills the child process.

    Previously the install subprocesses ran with no cancellation
    polling, so a Cancel click left them running for up to 30 minutes while
    the long-op lock stayed held. The watchdog now fires within 500ms.
    """
    import sys as _sys
    import time as _time

    from vibechek import cancellation
    from vibechek.native_install import _run_with_progress

    cancellation.end()  # clean slate
    cancellation.begin("install-test")

    # A child process that just sleeps — would normally run for the full
    # 10-second timeout. With cancellation, it should die within ~1s.
    sleeper = [_sys.executable, "-c", "import time; time.sleep(10)"]

    # Fire the cancel from a background thread once the child has had a
    # moment to spawn.
    import threading as _threading

    def _trigger_cancel() -> None:
        _time.sleep(0.2)
        cancellation.cancel()

    _threading.Thread(target=_trigger_cancel, daemon=True).start()

    start = _time.time()
    rc, tail = _run_with_progress(sleeper, on_progress=lambda _line: None, timeout=10)
    elapsed = _time.time() - start

    # The watchdog polls every 500ms — should fire well before the 10s timeout.
    assert elapsed < 5.0, f"Cancellation took {elapsed:.1f}s; expected < 5s"
    # rc == -1 is the "killed" sentinel from the helper
    assert rc == -1
    assert any("Cancelled by user" in line for line in tail), \
        f"Expected 'Cancelled by user' marker in tail; got {tail[-3:]}"
    cancellation.end()


def test_native_install_run_subprocess_cancellable_terminates_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Short-running cancellable wrapper also honors mid-run cancellation."""
    import sys as _sys
    import threading as _threading
    import time as _time

    from vibechek import cancellation
    from vibechek.native_install import _run_subprocess_cancellable

    cancellation.end()
    cancellation.begin("install-test")

    sleeper = [_sys.executable, "-c", "import time; time.sleep(10)"]

    def _trigger() -> None:
        _time.sleep(0.2)
        cancellation.cancel()

    _threading.Thread(target=_trigger, daemon=True).start()

    start = _time.time()
    rc, _stdout, _stderr, cancelled = _run_subprocess_cancellable(sleeper, timeout=10)
    elapsed = _time.time() - start

    assert elapsed < 5.0
    assert cancelled is True
    assert rc == -1 or rc != 0  # killed; exact code is OS-dependent
    cancellation.end()


def test_install_essentia_native_returns_cancelled_dict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When cancellation fires during venv creation, install returns ok=False, cancelled=True."""
    from vibechek import cancellation, native_install

    cancellation.end()
    cancellation.begin("install-test")

    # Force the supported flag and a fake host python
    monkeypatch.setattr(native_install, "IS_SUPPORTED", True)
    monkeypatch.setattr(native_install, "_find_host_python", lambda: "python3")
    # Pretend the venv doesn't exist yet so we go through step 1
    monkeypatch.setattr(
        native_install.Path,
        "exists",
        lambda self: False,
        raising=False,
    )

    # Replace the cancellable subprocess wrapper to simulate a cancelled run
    def fake_run(args, timeout):
        cancellation.cancel()
        return -1, "", "killed", True

    monkeypatch.setattr(native_install, "_run_subprocess_cancellable", fake_run)

    result = native_install.install_essentia_native()
    assert result["ok"] is False
    assert result.get("cancelled") is True
    cancellation.end()


# ---------------------------------------------------------------------------
# WSL install cancellation
# ---------------------------------------------------------------------------


def test_wsl_install_cancellation_watchdog_kills_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_start_cancellation_watchdog terminates a long-running child on cancel.

    The watchdog is the shared piece behind install_wsl, install_vibechek_in_wsl,
    install_cuda_libs_in_wsl, and repair_wsl_shim.
    """
    import subprocess as _subprocess
    import sys as _sys
    import threading as _threading
    import time as _time

    from vibechek import cancellation
    from vibechek.wsl import _start_cancellation_watchdog

    cancellation.end()
    cancellation.begin("install-test")

    proc = _subprocess.Popen(
        [_sys.executable, "-c", "import time; time.sleep(10)"],
        stdout=_subprocess.PIPE, stderr=_subprocess.PIPE,
    )

    cancel_done, state = _start_cancellation_watchdog(proc)

    def _trigger() -> None:
        _time.sleep(0.2)
        cancellation.cancel()

    _threading.Thread(target=_trigger, daemon=True).start()

    start = _time.time()
    proc.wait(timeout=10)
    elapsed = _time.time() - start
    cancel_done.set()

    assert elapsed < 5.0
    assert state["v"] is True
    assert proc.returncode is not None and proc.returncode != 0
    cancellation.end()


def test_wsl_install_wsl_returns_cancelled_dict(monkeypatch: pytest.MonkeyPatch) -> None:
    """install_wsl surfaces cancellation as `cancelled: True`."""
    import sys as _sys
    import threading as _threading
    import time as _time

    from vibechek import cancellation
    from vibechek import wsl as wsl_mod

    cancellation.end()
    cancellation.begin("install-test")
    monkeypatch.setattr(wsl_mod, "IS_WINDOWS", True)

    real_popen = wsl_mod.subprocess.Popen

    def fake_popen(args, **kwargs):
        # Replace the PowerShell command with a long-running sleep so the
        # watchdog actually has something to kill.
        sleep_cmd = [_sys.executable, "-c", "import time; time.sleep(10)"]
        return real_popen(
            sleep_cmd,
            stdin=kwargs.get("stdin"),
            stdout=kwargs.get("stdout"),
            stderr=kwargs.get("stderr"),
            text=kwargs.get("text", False),
            encoding=kwargs.get("encoding"),
            errors=kwargs.get("errors"),
        )

    monkeypatch.setattr(wsl_mod.subprocess, "Popen", fake_popen)

    def _trigger() -> None:
        _time.sleep(0.3)
        cancellation.cancel()

    _threading.Thread(target=_trigger, daemon=True).start()

    start = _time.time()
    result = wsl_mod.install_wsl("Ubuntu-24.04")
    elapsed = _time.time() - start

    assert elapsed < 5.0, f"install_wsl took {elapsed:.1f}s after cancel"
    assert result["ok"] is False
    assert result.get("cancelled") is True
    cancellation.end()


@pytest.mark.parametrize(
    "evil_distro",
    [
        "Ubuntu'; Remove-Item C:\\ -Recurse -Force; '",  # classic injection
        "Ubuntu' ; calc ; '",
        "Ubuntu`nwhoami",                                  # newline
        "Ubuntu 24.04",                                    # space
        "Ubuntu$(whoami)",                                 # subshell
        "",                                                # empty
    ],
)
def test_install_wsl_rejects_unsafe_distro(
    evil_distro: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """install_wsl refuses distro names with shell metacharacters.

    The name is interpolated into a PowerShell command behind a UAC
    elevation; a quote would break out and run arbitrary code. Validation
    must reject BEFORE any subprocess is spawned.
    """
    from vibechek import wsl as wsl_mod

    monkeypatch.setattr(wsl_mod, "IS_WINDOWS", True)

    # If validation is bypassed and a subprocess is launched, fail loudly.
    def _boom(*_a, **_k):  # pragma: no cover - must not be reached
        raise AssertionError("install_wsl spawned a process for an unsafe distro")

    monkeypatch.setattr(wsl_mod.subprocess, "Popen", _boom)

    result = wsl_mod.install_wsl(evil_distro)
    assert result["ok"] is False
    assert "invalid distro" in result["error"].lower()


def test_install_wsl_accepts_valid_distro_name() -> None:
    """A well-formed distro name passes the validation guard.

    We only check the guard, not the full install: monkeypatch isn't needed
    because on non-Windows CI install_wsl returns early before validation —
    so assert the regex the guard uses accepts the canonical names directly.
    """
    from vibechek.wsl import _VALID_DISTRO_RE

    for ok_name in ("Ubuntu-24.04", "Debian", "kali-linux", "openSUSE-Tumbleweed"):
        assert _VALID_DISTRO_RE.match(ok_name), ok_name


# ---------------------------------------------------------------------------
# _wsl_run encoding detection
# ---------------------------------------------------------------------------


def test_detect_wsl_encoding_prefers_utf8_when_no_nul_bytes() -> None:
    # A UTF-8 wsl.exe build emits plain ASCII/UTF-8 with no interleaved NULs.
    assert _detect_wsl_encoding_order(b"Ubuntu-24.04 Running 2\n")[0] == "utf-8"


def test_detect_wsl_encoding_prefers_utf16_for_interleaved_nuls() -> None:
    # Classic wsl.exe UTF-16-LE output: each ASCII char followed by \x00.
    utf16 = "Ubuntu".encode("utf-16-le")
    assert _detect_wsl_encoding_order(utf16)[0] == "utf-16-le"


def test_detect_wsl_encoding_honors_boms() -> None:
    assert _detect_wsl_encoding_order(b"\xff\xfeU\x00")[0] == "utf-16-le"
    assert _detect_wsl_encoding_order(b"\xef\xbb\xbfhello")[0] == "utf-8"


def test_wsl_run_decodes_utf8_build_output() -> None:
    # Regression: a UTF-8 wsl.exe build must not be mis-decoded as UTF-16.
    payload = b"Ubuntu-24.04 LTS\n"
    completed = subprocess.CompletedProcess(["wsl"], 0, payload, b"")
    with patch("vibechek.wsl.subprocess.run", return_value=completed):
        result = _wsl_run(["wsl", "--list"])
    assert "Ubuntu-24.04 LTS" in result.stdout


def test_wsl_run_decodes_utf16_build_output() -> None:
    payload = "Ubuntu\n".encode("utf-16-le")
    completed = subprocess.CompletedProcess(["wsl"], 0, payload, b"")
    with patch("vibechek.wsl.subprocess.run", return_value=completed):
        result = _wsl_run(["wsl", "--list"])
    assert "Ubuntu" in result.stdout


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
