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


def _fail(msg: str) -> None:
    print(f"FAIL: {msg}", file=sys.stderr)
    sys.exit(1)


def _write_wav(path: Path, seconds: float = 1.0, freq: float = 440.0, rate: int = 22050) -> None:
    """A real PCM WAV via the stdlib only — no numpy/soundfile on purpose."""
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        n = int(seconds * rate)
        w.writeframes(
            b"".join(
                struct.pack("<h", int(20000 * math.sin(2 * math.pi * freq * i / rate)))
                for i in range(n)
            )
        )


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

    def call(self, method: str, params: dict | None = None) -> dict:
        """Send one request and block until ITS response frame arrives.

        Notifications (progress / ready / notify) and non-JSON noise are
        skipped; an EOF before the response is a hard failure.
        """
        self._next_id += 1
        req_id = self._next_id
        frame = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}}
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(frame) + "\n")
        self.proc.stdin.flush()

        while True:
            try:
                line = self._lines.get(timeout=CALL_TIMEOUT_SEC)
            except queue.Empty:
                _fail(f"{method}: no response within {CALL_TIMEOUT_SEC}s")
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
            # progress / ready / notify / stale frames — fine, keep reading

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
    _write_wav(lib / "tone_a.wav", freq=440.0)
    _write_wav(lib / "tone_b.wav", freq=523.25)
    print(f"fixtures: 2 WAVs under {lib}")

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

        # 6. the server must still be answering after everything above
        resp = sc.call("ping")
        if (resp.get("result") or {}).get("pong") is not True:
            _fail(f"final ping: sidecar wedged? {resp}")
        print("ok: sidecar still responsive (final ping)")
    finally:
        sc.close()
        shutil.rmtree(tmp, ignore_errors=True)

    print("\nRPC smoke: ALL OK")


if __name__ == "__main__":
    main()
