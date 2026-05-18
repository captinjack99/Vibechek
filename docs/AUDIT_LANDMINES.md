# Vibechek landmine audit

A systematic enumeration of "doomed to fail on a real user's machine" patterns
in the current codebase. Sorted by risk, highest first. Each entry calls out a
single concrete failure mode — not opinions about style.

Scope:
- Python sidecar (`vibechek/*.py`)
- Rust shell (`ui/src-tauri/src/*.rs`)
- TS hooks/stores (`ui/src/hooks/*`, `ui/src/stores/*`)

Out of scope (known, documented limitations): AGPL bundling constraints,
Windows lacking a native essentia wheel (we route through WSL on purpose).

---

## 1. ~~macOS install of essentia-tensorflow will silently fail~~ — FALSE ALARM

**Risk: ~~HIGH~~ → resolved, no action needed.**

**Correction.** Audit agent misread the PyPI index. Direct check of
<https://pypi.org/simple/essentia-tensorflow/> confirms macOS wheels DO exist
for both Intel (`macosx_14_0_x86_64`) and Apple Silicon (`macosx_15_0_arm64`),
covering cpython 3.9-3.13. `pip install essentia-tensorflow` works on Macs.

The remaining concern, kept for the record:

**What breaks.** `vibechek/native_install.py:274` runs
`pip install essentia-tensorflow` into the managed venv. The
`essentia-tensorflow` distribution on PyPI ships **Linux x86_64 / Linux aarch64
manylinux wheels only** — there is no `macosx_*_arm64` or `macosx_*_x86_64`
wheel for it. On any Mac, pip will either:

1. Fall back to building from source (will fail — needs TensorFlow C++ headers,
   eigen, gaia2, taglib, libsamplerate, fftw, and a working clang toolchain
   with `homebrew`-installed deps), or
2. Resolve to a much older Linux-only release that gets skipped, then return
   `No matching distribution found for essentia-tensorflow`.

The Mac user will sit through ~30s of pip noise, then get a generic
"essentia-tensorflow install exited with 1" toast. There is no Mac-specific
explanation. Our README claims "macOS & Linux. Vibechek creates a hermetic
Python venv … ~3-5 minutes later it's running" — that is false on macOS.

**Where in code.**
- `vibechek/native_install.py:188-281` — `install_essentia_native`
- `vibechek/native_install.py:46-49` — `IS_MAC = sys.platform == "darwin"` /
  `IS_SUPPORTED = IS_MAC or IS_LINUX`
- `pyproject.toml:41` — `essentia-tensorflow>=2.1b6.dev1110; platform_system != 'Windows'`
- `README.md:83` — false claim
- `docs/INSTALL.md:44-55` — same claim

**Recommended fix.**
- Probe PyPI for an actual matching wheel before attempting install. If none,
  surface a *specific* Mac error explaining the situation and pointing at
  `brew install essentia` (community formula) or the build-from-source path.
- Drop `IS_MAC` from `IS_SUPPORTED` until we have a real Mac story, OR
  short-circuit `install_essentia_native()` on Darwin with a clear "not
  available on macOS yet" message.
- Update `README.md`, `docs/INSTALL.md`, and the Preflight dialog copy.

**How to verify it's broken.** On any Mac (arm64 or intel),
`pip install essentia-tensorflow` into a fresh venv. Watch it fail.

---

## 2. `apply_gpu_preference` is set on the parent process, not the spawn args

**Risk: HIGH**

**What breaks.** `vibechek/analyzer.py:751-752` in `_worker_init` calls
`load_models(...)` which calls `apply_gpu_preference(use_gpu)`. That sets
`os.environ["CUDA_VISIBLE_DEVICES"]` *in the worker process*. With
`multiprocessing.get_context("spawn")` (analyzer.py:913), the workers inherit
the parent's environment block at fork time — but `CUDA_VISIBLE_DEVICES` set
inside the worker only affects *that worker's children*, not TensorFlow that
runs in the same worker process.

This actually mostly works because TF reads the env var on first
`tf.config.list_physical_devices` call, which is after `apply_gpu_preference`
runs in the same worker. BUT: if anything in the import chain of `essentia`,
`numpy`, or the `_worker_analyze` path touches CUDA before `apply_gpu_preference`
runs, the env var is too late. We've already been bitten by this exact ordering
problem (see the `resources._gpu_devices_from_tensorflow` regression in the
problem statement) and have not added a regression test.

Also, on Windows the WSL path does not enforce GPU preference at all — the
`--gpu off` flag is forwarded to `vibechek` inside WSL but the wrapper bash
also `. cuda-env.sh`'s the lib path *before* `vibechek` runs (wsl.py:1533).
That's fine for `auto`/`on`, but with `--gpu off` we still leak the env file's
LD_LIBRARY_PATH into the worker.

**Where in code.**
- `vibechek/analyzer.py:368-379` — sets env *inside* worker init
- `vibechek/wsl.py:1530-1535` — sources `cuda-env.sh` unconditionally
- `vibechek/resources.py:185-200` — `apply_gpu_preference`

**Recommended fix.**
- Pass `CUDA_VISIBLE_DEVICES` through `spawn_ctx.Pool(env=...)` (requires using
  `Process(..., env=...)` via a custom initializer, or set it in the
  spawn-child's earliest entry — before `_worker_init` runs).
- In `run_vibechek_in_wsl`, only source `cuda-env.sh` when `use_gpu != "off"`.

**How to verify it's broken.** Set `--gpu off`, run analyze, and either watch
nvidia-smi or set `TF_LOG_DEVICE_PLACEMENT=1`. You will see TF still
enumerating GPUs in some worker processes.

---

## 3. NVIDIA CUDA wheel mapping pins to cu11 — TF in essentia 2.1 may have shifted

**Risk: HIGH**

**What breaks.** `vibechek/wsl.py:1104-1113` hardcodes a `.so → pip package`
map for CUDA 11.x runtime wheels (`nvidia-cuda-runtime-cu11`,
`nvidia-cublas-cu11`, `nvidia-cudnn-cu11`). These wheels exist on PyPI today,
but:

1. `essentia-tensorflow>=2.1b6.dev1110` is a dev build whose bundled TF version
   floats. A future pin could bundle TF 2.13+ which needs cu12 (cuBLAS .12,
   cuDNN .8 inside cu12). When that happens, we install the cu11 wheels — the
   missing-libs probe still reports `libcublas.so.11` because that's what TF
   2.5 wanted, but TF 2.13 dlopens `libcublas.so.12` instead and our installer
   "succeeds" while doing nothing useful.
2. The `cu11` wheel set on PyPI was retired by NVIDIA for newer Pythons in
   late 2024. On Python 3.13 inside Ubuntu 24.04 (which `recommended_distro`
   sends users to today!), `pip install nvidia-cudnn-cu11` returns
   `No matching distribution found`. We catch the error but the user is stuck
   with no path forward — our error message just says "pip install failed".
3. `_CUDA_PIP_PACKAGES_BY_LIB` is keyed on exact .so version suffix
   (`libcudart.so.11.0`). A TF upgrade that changes the suffix
   (`libcudart.so.12`) breaks the lookup; we fall through to the "no PyPI
   wheels mapped for" error.

**Where in code.**
- `vibechek/wsl.py:1104-1125`
- `vibechek/wsl.py:1134-1209` — bootstrap script
- `vibechek/wsl.py:1360-1373` — `unknown = [lib for lib in missing_libs if lib not in ...]`

**Recommended fix.**
- Detect the TF major version from the engine GPU probe and pick cu11 vs cu12
  wheel sets accordingly. The probe already reports `tf_version`.
- Add a fallback: if `nvidia-cudnn-cu11` 404s, try `nvidia-cudnn-cu12` (and
  refuse to claim success unless the next probe shows the libs resolved).
- Add a regression test that asserts the bootstrap doesn't dpkg/pip the wrong
  set when TF version is 2.13.

**How to verify it's broken.** Force `recommended_distro = "Ubuntu-24.04"`
with Python 3.13 (default on 24.04) and run the CUDA install flow. The wheel
solver fails.

---

## 4. `download_models` HEAD request silently swallows TLS/DNS errors

**Risk: HIGH**

**What breaks.** `vibechek/analyzer.py:283-292` — when the HEAD request to
`essentia.upf.edu` times out, errors, or returns no Content-Length, we
return `False` ("can't verify; trust what we have"). That means:

1. If `essentia.upf.edu` is down or blocked (corporate proxies, geographic
   firewalls — the host is in Spain), our installer hits the timeout, logs at
   DEBUG, and *uses whatever file is already on disk*, even if it's a 200 KB
   HTML "Service Unavailable" page from a previous failed run.
2. There is no proxy support anywhere in the codebase. `urllib.request.urlopen`
   honors `HTTP_PROXY` / `HTTPS_PROXY` env vars by default but we never set them
   when running inside WSL, where the env doesn't inherit Windows proxy settings.
3. The 10-second HEAD timeout, then 30-second GET timeout, then 64KB chunk read
   means a slow connection can be killed mid-download and we'll leave a
   `.partial` file behind that the next run treats as missing (so it retries
   forever).

The model_size sanity check (analyzer.py:349-353) only catches "implausibly
small" — a partial that's bigger than `min_size` but still truncated passes
silently if the server didn't send Content-Length.

**Where in code.**
- `vibechek/analyzer.py:276-292` — `_needs_download`
- `vibechek/analyzer.py:295-358` — `_download_with_progress`
- `vibechek/analyzer.py:47` — `MODEL_BASE_URL = "https://essentia.upf.edu/models"`

**Recommended fix.**
- On HEAD failure, surface the network error rather than trusting local file.
- Add a retry-with-backoff for transient errors (timeout, 5xx).
- Mirror the models on our own CDN / GitHub release asset so a single domain
  outage doesn't break the install.
- Validate via SHA-256 (publish hashes in the code), not Content-Length.

**How to verify it's broken.** Block `essentia.upf.edu` in /etc/hosts. Delete a
model file. Run `vibechek download-models`. It reports success because HEAD
failed and there's no local file, then loud-fails much later in `load_models`.

---

## 5. PyInstaller bundle resolution doesn't survive macOS Gatekeeper / quarantine

**Risk: HIGH**

**What breaks.** `ui/src-tauri/src/sidecar.rs:294-316` resolves the sidecar via:
1. `VIBECHEK_SIDECAR` env var
2. `vibechek-sidecar` / `vibechek-sidecar.exe` next to the Tauri exe
3. `vibechek` on PATH

Problems:
1. On macOS, the bundled sidecar must be codesigned + notarized OR the user
   must `xattr -d com.apple.quarantine` it. We have no codesigning step
   visible (no `tauri.conf.json` excerpt with `signingIdentity`). On first
   launch, macOS Gatekeeper kills the unsigned sidecar with `SIGKILL` and the
   user sees only "sidecar died (binary: ...)".
2. On Windows, Defender's Smart Screen can quarantine the .exe sidecar. The
   parent Tauri app gets a non-helpful "spawning sidecar at ... failed" error.
3. The PATH fallback (`Ok("vibechek".to_string())`) trusts the user's PATH —
   but the Tauri app inherits the GUI environment (Finder/Explorer), which on
   macOS doesn't include /usr/local/bin or homebrew paths. Even if the user
   has `vibechek` installed via pipx, the GUI won't find it.

**Where in code.**
- `ui/src-tauri/src/sidecar.rs:294-316` — `resolve_sidecar_binary`
- `ui/src-tauri/src/sidecar.rs:133-140` — `Command::new(&binary).arg("rpc").spawn()`

**Recommended fix.**
- Add a codesigning + notarization step to the macOS build pipeline (the
  RELEASING.md docs need this anyway).
- When the sidecar spawn fails on macOS, check for quarantine attrs and tell
  the user "right-click → Open" the first time.
- Probe the resolved binary via `--version` *before* binding it to the
  sidecar — if it segfaults under Gatekeeper, surface the actual exit code.

**How to verify it's broken.** Download a macOS .dmg from a GitHub Actions
build to a clean Mac, double-click, observe "vibechek-sidecar can't be opened
because Apple cannot check it for malicious software".

---

## 6. `_worker_init` ImportError or model-load failure hangs the analyze for the entire timeout

**Risk: HIGH**

**What breaks.** `vibechek/analyzer.py:914-928` — even with the
`spawn_ctx.Pool` and the preflight check (analyzer.py:816), the pool
initializer can still raise:
- Out-of-memory during model load (each worker is ~800MB; on a 16GB laptop
  with 4 workers we're at the edge)
- Disk-full or "Stale file handle" when loading the .pb files off a network
  drive
- A CUDA OOM on the *first* worker that exits 137; the remaining N-1 workers
  succeed, the pool reports a count of N tasks failed-via-restart, and stalls

When a worker dies during init, `multiprocessing.Pool.imap_unordered` doesn't
raise — it spawns a replacement, which fails the same way, in an infinite
loop. Our cancel check (`cancellation.is_cancelled()`) only fires when there
is *progress*. With zero workers alive, no progress event arrives and the
1-hour RPC_TIMEOUT_SECS in sidecar.rs:28 ticks down.

**Where in code.**
- `vibechek/analyzer.py:750-766` — `_worker_init` / `_worker_analyze`
- `vibechek/analyzer.py:914-928` — pool loop
- `ui/src-tauri/src/sidecar.rs:28` — 1-hour timeout

**Recommended fix.**
- Wrap `_worker_init` with a try/except that writes failure to a sentinel
  queue, and check that queue on every imap result.
- Use `Pool(initializer=_worker_init, ...)` with `maxtasksperchild=1` only
  during debugging; in production add per-worker init timeouts.
- Detect "no progress in 5 minutes" inside `analyze_directory` and tear down
  the pool with a clear error instead of waiting on the RPC timeout.

**How to verify it's broken.** Run analyze with `--workers 8` on a machine
with 8 GB RAM. Some workers OOM during model load. Watch the GUI hang.

---

## 7. `_stage_script_for_wsl` uses NamedTemporaryFile but never cleans up on Ctrl-C

**Risk: MED**

**What breaks.** `vibechek/wsl.py:826-859` writes a `vibechek-wsl-*.sh` file
to `$TEMP` with `delete=False`. The caller `unlink(missing_ok=True)`s it in
a `finally` block — but if the Python sidecar is hard-killed (Tauri exits,
user force-quits, OS reboot), the file stays in `%TEMP%`. Over months these
accumulate in the Windows tempdir, and on Windows tempdir is not auto-pruned.

Worse: the script may contain the user's environment / paths in the bash
heredoc body, including their Windows username if it appears in paths. That's
leaking limited PII to a world-readable temp file.

**Where in code.**
- `vibechek/wsl.py:826-859`
- `vibechek/wsl.py:1528` — token file uses the same pattern with `os.getpid()`

**Recommended fix.**
- On sidecar startup, sweep `%TEMP%/vibechek-wsl-*.sh` and
  `%TEMP%/vibechek-wsl-pid-*.txt` older than 24h.
- chmod the staged files to 0600 on POSIX (no-op on Windows but doesn't hurt).
- Use a per-session subdir (`%TEMP%/vibechek-<pid>/`) so a single cleanup
  removes the whole tree.

**How to verify it's broken.** Run an install, force-kill the sidecar
mid-run, look in `%TEMP%`. The `.sh` file is still there.

---

## 8. `find_audio_files` blows up on non-UTF-8 filenames on Windows

**Risk: MED**

**What breaks.** `vibechek/utils.py:38` uses `Path.rglob` which on Windows
returns `WindowsPath` objects whose `__str__` round-trips via the active
codepage. Filenames containing characters outside cp1252 (Cyrillic, CJK,
emoji) are common in DJ libraries from international stores. When we then:

1. Serialize the path through JSON-RPC to the Tauri shell — JSON requires
   UTF-8, and we cast Paths through `_json_default` which calls `str(o)`. On
   a misconfigured locale this can mojibake.
2. Pass the path to `MutagenFile(str(filepath))` — mutagen opens the file
   with the *byte* path on POSIX, and a UTF-16 OS path on Windows; the
   `str(filepath)` collapses to whatever the locale supports.
3. Translate to a WSL path: `win_to_wsl_path` (wsl.py:794) does a regex
   replace on the string. WSL's `/mnt/c/...` *does* support arbitrary
   UTF-8 bytes, but if the Windows side already mangled the path, the WSL
   side gets the mangled version and `MonoLoader` fails to open the file.
4. The `analysis.json` written back also goes through `json.dumps(...,
   ensure_ascii=False)`, so a Windows-side reading of the file with a
   non-UTF-8 locale reverses the corruption again.

**Where in code.**
- `vibechek/utils.py:22-43` — `find_audio_files`
- `vibechek/wsl.py:794-806` — `win_to_wsl_path`
- `vibechek/analyzer.py:1094` — `_json.loads(local_output.read_text(encoding="utf-8"))`
- `vibechek/rpc.py:79-87` — `_json_default`

**Recommended fix.**
- Set `PYTHONUTF8=1` in the sidecar env from the Rust side
  (`Command::new(...).env("PYTHONUTF8", "1")`).
- In `cli.py:19-24` we already `reconfigure(encoding="utf-8")` stdout/stderr,
  but the env var is the only way to fix `Path` itself.
- Add a test with a Cyrillic-named MP3 file that round-trips through the
  WSL analyze path.

**How to verify it's broken.** Put `тест.mp3` in a folder, run analyze on
Windows with the system locale set to English (cp1252). The path either
disappears from the report or analyzes with `?` placeholders.

---

## 9. `_dispatch` uses ThreadPoolExecutor + cancellation singleton — concurrent long ops corrupt state

**Risk: MED**

**What breaks.** `vibechek/rpc.py:42-44, 746` runs requests through a
`ThreadPoolExecutor(max_workers=8)`. The cancellation module
(`vibechek/cancellation.py`) is a **process-global singleton** — one `_flag`,
one `_current_kind`. If the GUI fires two cancellable ops in parallel (say,
`analyze_directory` and `backup_tags`), the second `begin()` clobbers the
first's `_current_kind`; the first op's cancel check still works
(`is_cancelled()` is a shared flag), but `end()` from op 1 clears the flag
for op 2.

Worse, `_dispatch` (rpc.py:692-694) calls `cancellation.begin(kind)` for
every cancellable method, immediately. If two analyze calls land in the pool
at the same time (the GUI can't fire two, but a misbehaving frontend or a
test harness could), they share the same model-pool spawn and the first
to `end()` wipes the flag for the second.

The cancellation docstring (cancellation.py:8-12) hints at this — "processed
serially" — but the actual code is parallel-dispatched.

**Where in code.**
- `vibechek/rpc.py:42-44` — `_DISPATCH_WORKERS = 8`
- `vibechek/rpc.py:691-710` — `_dispatch` calls `cancellation.begin/end`
- `vibechek/cancellation.py:25-43` — module-global state

**Recommended fix.**
- Enforce "one long op at a time" in `_dispatch`: if a cancellable op is
  already running, return `INVALID_REQUEST` with a "busy" data field.
- Or move `_current_kind` to a per-request token and let
  `cancel_operation(token)` accept a specific id.

**How to verify it's broken.** From a test, fire two `find_duplicates` over
the RPC pipe. Call `cancel_operation`. One operation gets cancelled, the
other ignores the flag (or both end with mixed state).

---

## 10. `_to_toml_dict` silently drops `None` values — round-trip is lossy

**Risk: MED**

**What breaks.** `vibechek/config.py:182-190` — `_stringify_paths` drops dict
entries where the value is None (TOML has no null type). This means:

1. `DuplicateConfig(review_folder=None)` saves `[duplicates]` with no
   `review_folder` key. On reload, `_subset` defaults it back to `None` —
   that's accidentally fine.
2. `OrganizationConfig(target_root=None)` — same pattern, accidentally fine.
3. BUT: when a future config field defaults to `None`, the GUI's "reset to
   default" + save → load round-trip silently drops it. A user who explicitly
   sets `target_root` to "" expecting an empty string will find it doesn't
   round-trip — the field disappears and reverts to default.

Also: `_to_toml_dict` calls `asdict(cfg)` which materializes `Path` objects
as `PosixPath('...')` on POSIX and `WindowsPath('...')` on Windows. The
`_stringify_paths` walk only catches top-level `Path` instances — nested
Paths inside dicts work because `asdict` recursively turns dataclasses into
dicts, but a list of Paths would not be stringified. Currently we don't have
one, but the abstraction is leaky.

**Where in code.**
- `vibechek/config.py:174-190`

**Recommended fix.**
- Encode None as a sentinel string like `"__none__"` and decode on read, OR
- Use a JSON config file (config has no native null limitation).

---

## 11. `analyze_directory` worker memory cap assumes 800 MB but TF on CPU can be smaller / on GPU larger

**Risk: MED**

**What breaks.** `vibechek/analyzer.py:854-867` does
`memory_cap = max(1, usable_mb // 800)`. The 800 MB figure is a CPU estimate.
On GPU mode, TF reserves GPU VRAM not host RAM, so the *real* limit becomes
GPU VRAM, not host RAM — and we silently let 4 workers (GPU cap, line 872-879)
share whatever VRAM is left. On a 4 GB GTX 1050, four workers × 1 GB model
load + activations OOM-kill the pool in 30 seconds.

Conversely, if the user has 64 GB RAM but only 4 GB VRAM and we're in GPU
mode, the host-RAM-based cap (usable_mb // 800) = ~78, then GPU cap pulls
it back to 4. Fine. But in **CPU mode on a Mac with 8 GB RAM**, the cap is
`(8192 - 2048) // 800 = 7` and we spawn 7 workers, each importing TF — total
~5.6 GB, swapping starts, the OS kills the Tauri parent.

The fact that we silently downgrade workers (log.warning, no GUI surface)
means users complain about "slow analyze" without knowing why.

**Where in code.**
- `vibechek/analyzer.py:843-879`

**Recommended fix.**
- Read VRAM via `nvidia-smi --query-gpu=memory.free` for GPU cap; pick
  `floor(vram_mb / 1500)` workers.
- Surface the downgrade reason in a `progress` notification so the GUI shows
  "Capped workers from 8 to 3 due to low VRAM" instead of hiding it in a log.

---

## 12. RPC `analyze_directory` doesn't enforce a sensible per-request timeout

**Risk: MED**

**What breaks.** `ui/src-tauri/src/sidecar.rs:28` — `RPC_TIMEOUT_SECS = 60 * 60`
(1 hour). For most ops this is fine. For a 50,000-track analyze it's not
enough. The frontend's `useSidecar.ts:46-53` propagates the timeout error
as an `RpcError`, which the operation store treats as a failure (not a
cancellation). After 1 hour of successful analyze, the GUI throws
"sidecar call 'analyze_directory' timed out" and the user thinks their
work was lost. (In practice the sidecar keeps running and the analysis.json
still ends up on disk via the partial-write logic, but the GUI has no way to
recover it.)

Conversely, a quick op like `system_info` should be limited to a few seconds,
not an hour — if it hangs, the GUI hangs.

**Where in code.**
- `ui/src-tauri/src/sidecar.rs:28, 98-115`

**Recommended fix.**
- Per-method timeout map: quick reads = 30s, analyses = no timeout (use a
  heartbeat notification from the sidecar instead, mark dead if no heartbeat
  in 5 minutes).
- If timeout fires, check for the partial analysis.json on disk and offer to
  load it.

---

## 13. WSL `bash -lc` invocation in `run_vibechek_in_wsl` re-introduces variable-substitution bug

**Risk: MED**

**What breaks.** After fixing the `bash -c "X=$(...);"` empty-variable bug
by switching install scripts to staged tempfiles, `run_vibechek_in_wsl`
(wsl.py:1530-1536) still uses `bash -lc cmd_str` for the actual analyze
launch. The `cmd_str` includes `echo $$ > $TOKEN_FILE` and a chained
`. cuda-env.sh; vibechek ...`. The token file write *does* work in this
case (we tested it), but the underlying preprocessor quirk is the same —
any future addition of a `VAR=$(...)` pattern in this string will silently
fail with empty `VAR`.

There's a comment about this in `_stage_script_for_wsl` (wsl.py:826-859)
but the analyze path doesn't use staging.

**Where in code.**
- `vibechek/wsl.py:1530-1536` — `cmd_str` + `bash -lc cmd_str`

**Recommended fix.**
- Convert `run_vibechek_in_wsl` to use `_stage_script_for_wsl` for symmetry
  and crash-safety.
- Add a lint rule / test that forbids `bash -lc` with multi-line strings.

---

## 14. `download_models` writes models to user_data_dir — accessible to other users on shared machines

**Risk: MED**

**What breaks.** `vibechek/config.py:27-28` — `DATA_DIR = user_data_dir(APP_NAME)`
on Windows resolves to `%LOCALAPPDATA%/Vibechek` (per-user, OK) but on Linux
to `~/.local/share/Vibechek`, on macOS to `~/Library/Application Support/Vibechek`.

In WSL, that's the *WSL* user's home, not the Windows user's home. Models
downloaded by the Windows-side sidecar live at `%LOCALAPPDATA%/Vibechek/models/`,
which WSL sees at `/mnt/c/Users/<WinUser>/AppData/Local/Vibechek/models/`.

`_analyze_via_wsl` (analyzer.py:1041) translates this with `win_to_wsl_path`
and passes it as `--models-dir`. But:
1. The WSL user is "root" or a different name from the Windows user. If WSL
   has multiple users, only the one running vibechek can access the mounted
   models because Drvfs respects Windows ACLs.
2. If `%LOCALAPPDATA%` contains non-ASCII (Windows username with accented
   chars), the path translation breaks (see finding #8).
3. The download path requires write access. On a corporate workstation
   where `%LOCALAPPDATA%` is locked down, model downloads silently fail —
   we catch the IOError, treat it as model-missing, retry next launch.

**Where in code.**
- `vibechek/config.py:27-28`
- `vibechek/analyzer.py:1037-1041`

**Recommended fix.**
- Allow `--models-dir` override at the GUI level so users on locked-down
  machines can point at `D:\models` etc.
- On Windows, store models under the WSL distro's `~/.vibechek/models` and
  copy via `wsl --import` once at install time. Then `--models-dir` is
  inside WSL and the cross-mount path-translation bug goes away.

---

## 15. Concurrent JSON-RPC writes from threads can interleave when stderr leaks to stdout

**Risk: MED**

**What breaks.** `vibechek/rpc.py:54-62` — `_StdoutWriter` uses a mutex to
serialize JSON-RPC frames. Good. BUT: TF / essentia / native CUDA libs
sometimes write directly to file descriptor 1 (stdout) via printf() in C
code, bypassing Python's `sys.stdout`. We mitigate this by setting
`TF_CPP_MIN_LOG_LEVEL=3` (analyzer.py:45, 355-357) and capture it in the
WSL probe, but for the *native* analyze path on Linux:

1. `essentia` C++ code can `fprintf(stderr, ...)`.
2. `tf.Constant` and friends call back into native ops that occasionally
   print errors to fd 1.

The Tauri sidecar wires the Python child's stdout straight to the JSON-RPC
parser (sidecar.rs:146-148). One stray native printf and the JSON parser
chokes on the line. We catch the parse error in handle_message (sidecar.rs:268)
but lose the response.

**Where in code.**
- `vibechek/rpc.py:53-76`
- `ui/src-tauri/src/sidecar.rs:262-291`

**Recommended fix.**
- Use a framing scheme other than newline-delimited JSON over stdout — e.g.,
  Content-Length prefixed messages (LSP-style) so binary noise is detectable.
- Or open a dedicated pipe via fd 3 for protocol traffic and use stdout
  purely for "the inevitable garbage".

---

## 16. `_subset` config loader doesn't validate types — pickled rubbish becomes config

**Risk: MED**

**What breaks.** `vibechek/config.py:151-163` — `_subset` only checks `if
"Path" in str(ftype)` for coercion. Otherwise it forwards the TOML-decoded
value verbatim. If a user (or a corrupted TOML write) puts a string where an
int is expected (`workers = "lots"`), the dataclass accepts it. Later,
`AnalysisConfig.workers > 0` blows up in `analyze_directory:851` with
`TypeError: '>' not supported between instances of 'str' and 'int'`.

The handler at `_dispatch` catches `(TypeError, KeyError, ValueError)`
(rpc.py:699) and returns `INVALID_PARAMS` — but the user sees "Invalid params:
'>' not supported..." which is incomprehensible.

**Where in code.**
- `vibechek/config.py:151-163`
- `vibechek/rpc.py:699-701`

**Recommended fix.**
- Use `dataclasses-json` or pydantic for parse-time validation, OR
- Add explicit type coercion in `_coerce` for `int`, `float`, `bool`, `str`.

---

## 17. `cleanup_wsl_tree` `pkill -f vibechek` is a footgun on multi-user WSL distros

**Risk: MED**

**What breaks.** `vibechek/wsl.py:1582-1588` runs `pkill -9 -f vibechek` as
a "belt-and-suspenders" worker takedown. `pkill -f` matches the *command line*,
so:
1. Any other process whose command line contains "vibechek" gets SIGKILLed.
2. A user running `vim ~/code/vibechek/foo.py` in a separate WSL terminal —
   killed.
3. The user's own shell history `grep vibechek ~/.bash_history` — killed.
4. A separate vibechek install on the same WSL user (e.g., a dev checkout
   running tests) — killed.

Without `-u $USER` it's even worse on a multi-user WSL distro, but most
single-user WSLs run as user 1000 so this is bounded — until it isn't.

**Where in code.**
- `vibechek/wsl.py:1582-1588`

**Recommended fix.**
- Use `pkill -P <pid>` (parent-pid filter) or `pkill -G <pgid>` instead.
- We already write the bash PID to a token file — use `pkill -s <sid>` or
  walk /proc to find descendants of that PID.

---

## 18. Sidecar binary path with spaces breaks Windows process spawn

**Risk: MED**

**What breaks.** `ui/src-tauri/src/sidecar.rs:303-311` returns the candidate
path via `candidate.to_string_lossy().into_owned()`. Then
`Command::new(&binary).arg("rpc")` is called. On Windows, `Command::new`
passes the binary path through the Win32 `CreateProcessW` API, which
correctly handles spaces in the **executable** path... usually.

But the user's install path is `C:\Users\Jack\My Drive\Vibechek\...` — and
"My Drive" contains a space *and* is a virtual filesystem (Google Drive
File Stream). Two failure modes:
1. Some Tauri builder configurations install per-user under `%LOCALAPPDATA%`
   which has no spaces — fine. Others install under `Program Files\Vibechek\`
   which works on Windows but the sidecar exe inherits the working directory
   of the parent, which may have spaces — and any subsequent
   `subprocess.Popen([wsl, "-d", distro, "--", "bash", wsl_script_path])`
   call inside Python uses `subprocess` quoting rules that mostly handle
   spaces but not always.
2. Google Drive virtual filesystem doesn't support file locking / mmap, so
   when PyInstaller's bootloader tries to dlopen libraries from the install
   path, the process can hang on first launch.

**Where in code.**
- `ui/src-tauri/src/sidecar.rs:303-311`
- Indirectly: any `Popen` call in `wsl.py` (binary search → spawn)

**Recommended fix.**
- Refuse to run if the install path contains characters known-bad to the
  bootloader (warn at startup, surface in a banner).
- Document "don't install under Google Drive" in INSTALL.md.

**How to verify it's broken.** Install Vibechek under `C:\My Drive\Apps\Vibechek\`
and try to launch.

---

## 19. `restore_tags` re-uses backup paths verbatim — won't restore to a moved library

**Risk: MED**

**What breaks.** `vibechek/tagger.py:319-346` — restore iterates the backup's
`files` dict, keyed by the *original* path. If the user backed up
`D:\Music\foo.mp3` then renamed the drive to `E:\`, restore skips every file
(`skipped_missing` count), and the user sees "Restored 0/12000". No retry,
no path-translation prompt, no opportunity to remap.

This is the entire purpose of a backup tool — restoring to a moved library is
a major use case.

**Where in code.**
- `vibechek/tagger.py:319-346`

**Recommended fix.**
- Add an optional `path_remap: dict[str, str]` argument or a heuristic that
  matches by filename + size if the original path is missing.

---

## 20. `pkill -TERM 0` in run_vibechek_in_wsl trap kills the parent shell — but only if it's a process group leader

**Risk: LOW**

**What breaks.** `vibechek/wsl.py:1532` — the bash trap `kill -TERM 0 2>/dev/null`
sends SIGTERM to the entire process group. This works only when our bash is
the *process group leader* — it is, because we spawned it directly via
`wsl.exe -d <distro> -- bash -lc "..."`. But:

1. If WSL2 evolves to wrap commands in another shell, our bash becomes a
   non-leader and `kill 0` kills nothing.
2. The trap line uses `2>/dev/null` to swallow errors — silent failure of
   the trap means the user clicks Cancel, the watchdog goes through
   `_kill_wsl_tree()` which then `pkill -f vibechek`'s everything.

**Where in code.**
- `vibechek/wsl.py:1530-1535`

**Recommended fix.**
- Use `setsid` to force a new session and `kill -- -$$` (kill the negative
  PGID directly) instead of relying on `kill 0`.

---

## 21. `essentia.upf.edu` HTTPS uses a hardcoded base URL — no fallback mirror

**Risk: LOW (but a likely time bomb)**

**What breaks.** `vibechek/analyzer.py:47` — `MODEL_BASE_URL = "https://essentia.upf.edu/models"`.
This URL has been stable, but academic infrastructure is famously fragile.
The UPF (Universitat Pompeu Fabra) maintains this for free as part of their
MTG (Music Technology Group). If the URL changes — and it has in the past —
every installed copy of Vibechek breaks until we ship a new release.

**Where in code.**
- `vibechek/analyzer.py:47-91`

**Recommended fix.**
- Mirror the models in a GitHub Release asset, host on our own bucket, or use
  HuggingFace (where many essentia models are also published).
- Allow `MODEL_BASE_URL` override via env var so power users can self-host.

---

## 22. `_R` class in `_probe_distro` is a code smell that hides a bug

**Risk: LOW**

**What breaks.** `vibechek/wsl.py:252-260` — we manually build a fake result
object after decoding stdout, then immediately check `if result.returncode != 0`
(line 259) — which we just set to 0 unconditionally on line 257. The check is
dead code. If proc.returncode was actually nonzero, we already returned at
line 242. The dead check makes it harder to spot the absence of error
handling for stdout-not-matching-expected-format.

**Where in code.**
- `vibechek/wsl.py:241-269`

**Recommended fix.**
- Delete lines 252-259 entirely; use `result = subprocess.run` and decode
  stdout into a local var, or pull out a small helper that does both.

---

## 23. ID3 tag writer assumes encoding=3 (UTF-8) which Rekordbox 5 doesn't read

**Risk: LOW**

**What breaks.** `vibechek/tagger.py:230, 235, 238, 245-247, 438-454` — every
TXXX/TIT1/TCON write uses `encoding=3` (UTF-8). Rekordbox 5 (and some other
older DJ software) ignores UTF-8 frames, only reading encoding=1 (UTF-16) or
encoding=0 (ISO-8859-1). Users on older Rekordbox who run our tagger see no
genre changes in Rekordbox even though the tag is on disk.

**Where in code.**
- `vibechek/tagger.py:230, 235, 238, 245-247, 416-460`

**Recommended fix.**
- Make encoding configurable in `TaggingConfig` (default UTF-8 for modern
  DJ software).
- Or write both UTF-8 and UTF-16 frames for max compat (some software reads
  the first one only).

---

## 24. `_emit_progress` swallows large messages — RPC pipe writes can block under backpressure

**Risk: LOW**

**What breaks.** `vibechek/rpc.py:90-95` — `_emit_progress` writes a JSON
notification per call. During a fast analyze on a small library (or a fast
download), we emit thousands per second. If the Tauri stdout reader falls
behind (Rust event emitter blocked because the frontend is unfocused), the
pipe buffer fills, and `_StdoutWriter.write` blocks the worker thread that
called it. The whole analyze stalls.

We don't currently see this because `_dispatch_workers=8` and progress comes
from the analyze worker on a slow cadence, but
`_download_with_progress` throttles to 10/sec (analyzer.py:328) — *which
proves we know about the backpressure risk in one place but ignore it
elsewhere*.

**Where in code.**
- `vibechek/rpc.py:90-95`
- `vibechek/analyzer.py:328` (good pattern, not applied everywhere)

**Recommended fix.**
- Add a single global progress throttle (max 20/sec) inside `_emit_progress`.
- Or buffer notifications and coalesce when the writer is slow.

---

## Findings summary

- **HIGH (6):** #1, #2, #3, #4, #5, #6
- **MED (13):** #7, #8, #9, #10, #11, #12, #13, #14, #15, #16, #17, #18, #19
- **LOW (5):** #20, #21, #22, #23, #24

(24 findings total.)

## Resolution status as of v0.3.0-beta.9

| # | Risk | Status | Released in |
|---|---|---|---|
| 1 | HIGH | FALSE ALARM — macOS wheels do exist | (no change needed) |
| 2 | HIGH | FIXED — `cuda-env.sh` only sourced when `--gpu != off` | beta.8 |
| 3 | HIGH | FIXED — `_resolve_cuda_packages` auto-routes cu11/cu12 by .so suffix | beta.8 |
| 4 | HIGH | FIXED — local size sanity check + retry-with-backoff + surface network errors | beta.8 |
| 5 | HIGH | FIXED — codesigning env vars wired in `release.yml`, full setup guide in RELEASING.md | beta.9 |
| 6 | HIGH | FIXED — `_worker_init` wraps + 5-min stall watchdog + `maxtasksperchild=200` | beta.8 |
| 7 | MED | FIXED — startup sweep of `vibechek-wsl-*` tempfiles older than 24h | beta.8 |
| 8 | MED | FIXED — `PYTHONUTF8=1` set by Rust shell when spawning sidecar | beta.8 |
| 9 | MED | FIXED — `_LONG_OP_LOCK` rejects concurrent cancellable ops with structured busy error | beta.8 |
| 10 | MED | FIXED — config storage migrated from TOML to JSON; `None` round-trips | beta.9 |
| 11 | MED | FIXED — `_probe_free_vram_mb` reads nvidia-smi, caps workers at `free_mb // 1500` | beta.9 |
| 12 | MED | FIXED — per-method timeout map in `sidecar.rs::timeout_for`; analyze unbounded | beta.9 |
| 13 | MED | FIXED — `run_vibechek_in_wsl` migrated to staged-tempfile launcher with setsid | beta.8 |
| 14 | MED | NOT A BUG — Settings already exposes models_dir override | (verified, no change) |
| 15 | MED | FIXED — `_silence_native_logs()` + Rust ignores non-JSON lines as noise | beta.9 |
| 16 | MED | FIXED — `_coerce` validates int/float/bool/str; fallback to default + warning log | beta.9 |
| 17 | MED | FIXED — `pkill -f` removed; PID group kill only via token file | beta.8 |
| 18 | MED | FIXED — sidecar emits `notify` frame for risky install paths (My Drive, OneDrive, etc.) | beta.9 |
| 19 | MED | FIXED — `restore_tags_with_remap` with filename+size + filename-alone fallbacks | beta.9 |
| 20 | LOW | FIXED — `setsid` already added to launcher; `kill 0` semantics now reliable | beta.8 |
| 21 | LOW | FIXED — `_download_from_mirrors` walks `MODEL_BASE_URLS`; env var override | beta.9 |
| 22 | LOW | FIXED — dead `_R` wrapper class removed from `_probe_distro` | beta.9 |
| 23 | LOW | FIXED — `TaggingConfig.id3_text_encoding` (default UTF-8); user-tunable | beta.9 |
| 24 | LOW | FIXED — `_emit_progress` throttled to 20/sec; final tick always emitted | beta.9 |

**23 of 24 findings resolved.** The one remaining (#1 macOS install) was a false alarm — Apple Silicon and Intel wheels exist on PyPI.

### Test growth
- Baseline (start of audit): 212 tests passing
- After all fixes: **300 tests passing** (+88 new regression tests)

## Top 3 to fix this week

1. **#1 macOS install path** — README and INSTALL.md actively lie about
   macOS support. Every Mac user who downloads the app today will be
   confused / angry within five minutes. Either land a working brew-based
   path or hide the macOS option in the UI and update docs.

2. **#4 model download silent fallback** — single-domain dependency on a
   foreign academic server, with code that *prefers* stale local files over
   surfacing errors. One CDN outage = every user's install is permanently
   broken-but-not-saying-so. Add SHA-256 validation + a mirror.

3. **#3 CUDA wheel version pinning** — Ubuntu 24.04 (which we recommend!)
   ships Python 3.12+ where cu11 wheels are increasingly unavailable. This
   will turn into a flood of "Enable GPU button doesn't work" reports the
   moment we get more Windows users. Detect TF version + branch cu11/cu12.
