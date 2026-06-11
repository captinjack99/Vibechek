#!/usr/bin/env python3
"""Live smoke-test for the JSON-RPC sidecar — used by native-smoke.yml.

Spawns ``vibechek rpc`` as a real subprocess (the same entry point the Tauri
desktop shell uses), drives it over stdin/stdout, and asserts on the
responses:

  1. ping               -> pong + version
  2. native_venv_status -> well-formed status (supported=True on Linux/macOS)
  3. scan_directory     -> finds the generated WAV fixtures
  4. scan_only          -> per-track records from the cheap pass (no ML)
  5. analyze_directory  -> reaches a TERMINAL response without an ML engine
                           installed (clean error or a report carrying
                           per-track errors) — never a hang or a crash
  6. ping (again)       -> the server survived all of the above

FULL TIER (``SMOKE_FULL_TIER=1``, Linux/macOS only) continues past the
no-engine error path and proves the REAL install + analyze pipeline on this
machine, all through the same live sidecar:

  7. install_essentia_native / setup_onnx_engine (``SMOKE_ENGINE`` picks
     essentia_tf|onnx) — the actual pip install into ~/.vibechek/venv[-onnx],
     sandboxed under a temp HOME. ``SMOKE_VIBECHEK_SOURCE=<checkout dir>``
     makes the venv install the commit under test instead of GitHub main.
  8. download_models    -> the .pb set (essentia_tf; the ONNX setup already
                           fetched its backbone + staged the bundled heads)
  9. native_venv_status -> MUST now report essentia+vibechek installed — the
                           exact probe invariant the unix site-packages glob
                           bug silently broke (fixed alongside this smoke)
 10. analyze_directory  -> a real ML analyze of the fixtures via the managed
                           venv: 2/2 track records, ml_bpm/ml_key/ml_energy
                           populated, zero per-track or summary errors

Exits non-zero on the first failed expectation, printing the offending frame.

Run locally with the dev venv:  python scripts/rpc_smoke.py
Override the binary:            VIBECHEK_BIN=/path/to/vibechek python scripts/rpc_smoke.py

CI runs this on ubuntu + macos against a BASE install (``pip install -e .``,
no [dev] extras), so it also proves the runtime dependency closure for every
import on the scan/RPC path.
"""

from __future__ import annotations

import json
import math
import os
import queue
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import wave
from pathlib import Path

CALL_TIMEOUT_SEC = 120  # generous: a cold analyze error path may probe engines
NOISE_PREVIEW = 400     # how much of an unparseable line to echo

# Full-tier ceilings. These bound the silent GAP between sidecar output lines,
# not the whole call: progress notifications keep the clock fresh, so they only
# fire when the pipeline truly wedges. pip is mute in a non-tty while it pulls
# one big wheel (essentia-tensorflow is ~0.5 GB), and a cold TF import inside
# the venv child prints nothing for tens of seconds — hence minutes, not 120 s.
INSTALL_GAP_TIMEOUT_SEC = 1800
DOWNLOAD_GAP_TIMEOUT_SEC = 900
ANALYZE_GAP_TIMEOUT_SEC = 900

FULL_TIER = os.environ.get("SMOKE_FULL_TIER") == "1"
SMOKE_ENGINE = os.environ.get("SMOKE_ENGINE", "essentia_tf")


def _fail(msg: str) -> None:
    print(f"FAIL: {msg}", file=sys.stderr)
    sys.exit(1)


def _write_wav(path: Path, seconds: float = 1.0, freq: float = 440.0, rate: int = 22050) -> None:
    """A real PCM WAV via the stdlib only — no numpy/soundfile on purpose.

    The tone is amplitude-gated at 2 Hz (25% duty) — a 120 BPM pulse train —
    so the full tier's rhythm/energy extractors get onsets to chew on instead
    of a wall of steady sine. The base tier doesn't care either way.
    """
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        n = int(seconds * rate)
        frames = bytearray()
        for i in range(n):
            t = i / rate
            gate = 1.0 if (t * 2.0) % 1.0 < 0.25 else 0.15
            frames += struct.pack(
                "<h", int(20000 * gate * math.sin(2 * math.pi * freq * t))
            )
        w.writeframes(bytes(frames))


def _find_binary() -> list[str]:
    override = os.environ.get("VIBECHEK_BIN")
    if override:
        return [override]
    found = shutil.which("vibechek")
    if found:
        return [found]
    _fail("no `vibechek` on PATH and VIBECHEK_BIN not set")
    raise AssertionError  # unreachable


def _sandboxed_env(state_dir: Path) -> dict[str, str]:
    """Point every home/appdata convention at a throwaway dir.

    The sidecar persists config + library state under platformdirs/HOME; the
    smoke must not write into the real user profile (CI is disposable, but
    developers run this locally too).
    """
    env = os.environ.copy()
    state_dir.mkdir(parents=True, exist_ok=True)
    for var in (
        "HOME",
        "USERPROFILE",
        "APPDATA",
        "LOCALAPPDATA",
        "XDG_DATA_HOME",
        "XDG_CONFIG_HOME",
        "XDG_CACHE_HOME",
    ):
        env[var] = str(state_dir)
    return env


class Sidecar:
    """Minimal line-delimited JSON-RPC client around the sidecar subprocess."""

    def __init__(self, state_dir: Path) -> None:
        cmd = [*_find_binary(), "rpc"]
        print(f"spawning: {cmd}")
        self.proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            env=_sandboxed_env(state_dir),
        )
        self._lines: queue.Queue[str | None] = queue.Queue()
        self._next_id = 0
        # Reader thread: select() doesn't work on Windows pipes, and CI devs
        # run this locally on Windows too.
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            self._lines.put(line)
        self._lines.put(None)  # EOF sentinel

    def call(
        self,
        method: str,
        params: dict | None = None,
        timeout: float = CALL_TIMEOUT_SEC,
        echo_progress: bool = False,
    ) -> dict:
        """Send one request and block until ITS response frame arrives.

        Notifications (progress / ready / notify) and non-JSON noise are
        skipped; an EOF before the response is a hard failure. `timeout`
        bounds each WAIT between lines, not the whole call — long operations
        stay alive by streaming progress. `echo_progress` prints progress
        messages (digit-collapsed + deduped, so a byte-counter doesn't flood
        the CI log) — life signs during multi-minute installs.
        """
        self._next_id += 1
        req_id = self._next_id
        frame = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}}
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(frame) + "\n")
        self.proc.stdin.flush()

        last_echo = ""
        while True:
            try:
                line = self._lines.get(timeout=timeout)
            except queue.Empty:
                _fail(f"{method}: no output for {timeout}s")
            if line is None:
                _fail(f"{method}: sidecar stdout EOF before the response (crashed?)")
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                print(f"  (skipping non-JSON noise: {line[:NOISE_PREVIEW]!r})")
                continue
            if msg.get("id") == req_id:
                return msg
            if echo_progress and msg.get("method") == "progress":
                text = str((msg.get("params") or {}).get("message", ""))
                key = re.sub(r"[\d.]+", "#", text)
                if key and key != last_echo:
                    last_echo = key
                    print(f"  · {text[:110]}")
            # other notifications / stale frames — fine, keep reading

    def close(self) -> None:
        try:
            if self.proc.stdin is not None:
                self.proc.stdin.close()
            self.proc.wait(timeout=10)
        except Exception:  # noqa: BLE001 — teardown must never mask the verdict
            self.proc.kill()


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="vibechek-smoke-"))
    lib = tmp / "library"
    lib.mkdir()
    # Base tier: 1 s is plenty for scan + the no-engine error path. Full tier
    # actually runs the models — 8 s keeps every extractor comfortably above
    # its minimum-audio floor while the analyze stays seconds-cheap.
    seconds = 8.0 if FULL_TIER else 1.0
    _write_wav(lib / "tone_a.wav", seconds=seconds, freq=440.0)
    _write_wav(lib / "tone_b.wav", seconds=seconds, freq=523.25)
    print(f"fixtures: 2 WAVs ({seconds:g}s) under {lib}")

    sc = Sidecar(state_dir=tmp / "appstate")
    try:
        # 1. ping
        resp = sc.call("ping")
        result = resp.get("result") or {}
        if result.get("pong") is not True or not result.get("version"):
            _fail(f"ping: unexpected response {resp}")
        print(f"ok: ping (sidecar version {result['version']})")

        # 2. native_venv_status — shape check; True only ever on Linux/macOS
        resp = sc.call("native_venv_status")
        result = resp.get("result")
        if not isinstance(result, dict) or "supported" not in result:
            _fail(f"native_venv_status: unexpected response {resp}")
        if sys.platform != "win32" and result.get("supported") is not True:
            _fail(f"native_venv_status: expected supported=True on this OS, got {result}")
        print(f"ok: native_venv_status (supported={result.get('supported')})")

        # 3. scan_directory
        resp = sc.call("scan_directory", {"path": str(lib)})
        result = resp.get("result") or {}
        if result.get("count") != 2:
            _fail(f"scan_directory: expected count=2, got {resp}")
        print("ok: scan_directory (2 files)")

        # 4. scan_only — the cheap no-ML library load
        resp = sc.call("scan_only", {"path": str(lib)})
        result = resp.get("result") or {}
        tracks = result.get("tracks")
        if not isinstance(tracks, list) or len(tracks) != 2:
            _fail(f"scan_only: expected 2 track records, got {resp}")
        bad = [t for t in tracks if t.get("error")]
        if bad:
            _fail(f"scan_only: per-track errors on clean WAVs: {bad}")
        print("ok: scan_only (2 records, no per-track errors)")

        # 5. analyze_directory without any ML engine: must terminate CLEANLY.
        #    Accept either a JSON-RPC error (no engine) or a report whose
        #    tracks carry errors — both are sane; a hang or crash is not.
        #    SMOKE_SKIP_ANALYZE=1 skips this step for quick local runs (on a
        #    Windows dev box the analyze legitimately routes into WSL, which
        #    is slow and beside the point of a local driver check).
        if os.environ.get("SMOKE_SKIP_ANALYZE") == "1":
            print("skip: analyze_directory (SMOKE_SKIP_ANALYZE=1)")
        else:
            resp = sc.call("analyze_directory", {"path": str(lib), "workers": 1})
            if "error" in resp:
                msg = (resp["error"] or {}).get("message", "")
                print(f"ok: analyze_directory failed cleanly without an engine: {msg[:120]!r}")
            elif "result" in resp:
                print("ok: analyze_directory returned a report (engine present on this machine)")
            else:
                _fail(f"analyze_directory: neither result nor error: {resp}")

        # 7-10. FULL TIER — the real engine install + a real ML analyze.
        if FULL_TIER:
            _run_full_tier(sc, lib)

        # final: the server must still be answering after everything above
        resp = sc.call("ping")
        if (resp.get("result") or {}).get("pong") is not True:
            _fail(f"final ping: sidecar wedged? {resp}")
        print("ok: sidecar still responsive (final ping)")
    finally:
        sc.close()
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\nRPC smoke{' (full tier)' if FULL_TIER else ''}: ALL OK")


def _run_full_tier(sc: Sidecar, lib: Path) -> None:
    """Steps 7-10: engine install → model download → probe → real ML analyze.

    Everything runs through the live sidecar, inside the sandboxed HOME — the
    managed venv, the models dir, and the analysis output all land in the
    throwaway state dir, never in a real profile.
    """
    if sys.platform == "win32":
        _fail("SMOKE_FULL_TIER is Linux/macOS-only (it exercises the managed native venv)")
    engine = SMOKE_ENGINE
    if engine not in ("essentia_tf", "onnx"):
        _fail(f"SMOKE_ENGINE must be essentia_tf or onnx, got {engine!r}")
    source = os.environ.get("SMOKE_VIBECHEK_SOURCE")
    src_params = {"vibechek_source": source} if source else {}
    print(f"\nfull tier: engine={engine}"
          + (f", vibechek from {source}" if source else ", vibechek from GitHub main"))

    # 7. Install the engine into the managed venv (the slow, real step).
    if engine == "onnx":
        resp = sc.call(
            "setup_onnx_engine", {**src_params},
            timeout=INSTALL_GAP_TIMEOUT_SEC, echo_progress=True,
        )
        result = resp.get("result") or {}
        if "error" in resp or not (result.get("ok") and result.get("ready")):
            _fail(
                "setup_onnx_engine: not ready: "
                f"error={resp.get('error') or result.get('error')!r} "
                f"reasons={result.get('reasons_not_ready')}"
            )
        print(f"ok: setup_onnx_engine (ready; staged {len(result.get('staged') or [])} bundled heads)")
    else:
        resp = sc.call(
            "install_essentia_native", {"engine": engine, **src_params},
            timeout=INSTALL_GAP_TIMEOUT_SEC, echo_progress=True,
        )
        result = resp.get("result") or {}
        if "error" in resp or not result.get("ok"):
            _fail(f"install_essentia_native: {resp.get('error') or result.get('error')}")
        print(f"ok: install_essentia_native (essentia {result.get('essentia_version', '?')})")

        # 8. The .pb model set (the ONNX setup handles its own models).
        resp = sc.call(
            "download_models", {"engine": engine},
            timeout=DOWNLOAD_GAP_TIMEOUT_SEC, echo_progress=True,
        )
        if "error" in resp:
            _fail(f"download_models: {resp['error']}")
        models = (resp.get("result") or {}).get("models") or []
        print(f"ok: download_models ({len(models)} models)")

    # 9. The probe MUST see what was just installed. This is the exact
    #    invariant the unix `lib/python3.*` site-packages glob bug broke —
    #    install succeeded, probe said "essentia not installed", forever.
    resp = sc.call("native_venv_status", {"engine": engine})
    st = resp.get("result") or {}
    if not (st.get("essentia_installed") and st.get("vibechek_installed")):
        _fail(
            "native_venv_status after install: the probe is blind to the "
            f"install it just made (the glob-bug class): {st}"
        )
    print(
        f"ok: native_venv_status sees the install "
        f"(essentia {st.get('essentia_version')}, vibechek {st.get('vibechek_version')})"
    )

    # 10. A REAL analyze through the managed venv. Strict now: a report with
    #     both tracks carrying populated ML fields, zero errors anywhere.
    resp = sc.call(
        "analyze_directory",
        {"path": str(lib), "workers": 1, "inference_engine": engine},
        timeout=ANALYZE_GAP_TIMEOUT_SEC, echo_progress=True,
    )
    if "result" not in resp:
        _fail(f"analyze_directory (full): {resp.get('error')}")
    report = resp["result"]
    tracks = report.get("tracks") or []
    if len(tracks) != 2:
        _fail(f"analyze (full): expected 2 track records, got {len(tracks)}")
    for t in tracks:
        name = t.get("filename")
        ml = t.get("ml_analysis") or {}
        if t.get("error"):
            _fail(f"analyze (full): {name}: per-track error: {t['error']}")
        if ml.get("ml_error"):
            _fail(f"analyze (full): {name}: ml_error: {ml['ml_error']}")
        if not isinstance(ml.get("ml_bpm"), (int, float)):
            _fail(f"analyze (full): {name}: ml_bpm missing/not numeric: {ml.get('ml_bpm')!r}")
        if not (isinstance(ml.get("ml_key"), str) and ml["ml_key"].strip()):
            _fail(f"analyze (full): {name}: ml_key missing/empty: {ml.get('ml_key')!r}")
        if not isinstance(ml.get("ml_energy"), int):
            _fail(f"analyze (full): {name}: ml_energy missing/not int: {ml.get('ml_energy')!r}")
    summary = report.get("summary") or {}
    if summary.get("errors"):
        _fail(f"analyze (full): summary reports {summary['errors']} errors")
    print(
        f"ok: full ML analyze via the managed venv ({engine}): "
        "2/2 tracks, bpm/key/energy populated, 0 errors"
    )


if __name__ == "__main__":
    main()
