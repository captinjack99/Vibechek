# Vibechek — Project Summary

**Vibechek** is an open-source, ML-powered DJ library tool. It analyzes audio with
Essentia ML models and auto-classifies genre/subgenre, BPM, key, energy, mood,
timeslot, direction, and vocal type; finds true (acoustic) duplicates while keeping
genuine variants (Extended/Radio/Remix, alt formats); organizes files into
genre/subgenre folders; and backs up / restores every tag — all while preserving
Rekordbox cue points and beat grids. Genre specifically has three opt-in
classifiers (Discogs-EffNet, a pure-audio **CLAP** model ~2× more accurate, and a
fully-local **online LLM lookup**) plus smart existing-tag reconciliation; when
those sources disagree it flags the track for one-click **review** instead of
silently overwriting a hand-curated tag. It runs entirely on your machine: no
account, no telemetry, no upload. Free forever under **AGPL-3.0**.

- **Repo:** https://github.com/captinjack99/Vibechek
- **Current version:** `v0.6.0-beta` (public beta)
- **Platforms:** Windows, macOS, Linux (desktop app + CLI)

> This is the living project summary. For release history see [CHANGELOG.md](../CHANGELOG.md).

## Origins

Vibechek grew out of a pile of personal Python scripts used to clean up a real
~12,000-track DJ library: dedupe (MD5 + Chromaprint), full Essentia ML analysis,
confidence-filtered tag application that preserved Rekordbox GEOB/PRIV frames, and
genre/subgenre folder organization. The scripts worked, friends wanted them, and
"here's a folder of .py files, install Python first" was too embarrassing to keep
sending. So the pipeline got packaged into a real cross-platform app. That original
12k-track library is still the primary real-world test bed.

## Architecture

```
React UI ──[Tauri invoke]──► Rust shell ──[JSON-RPC stdin/stdout]──► Python sidecar
```

- **Frontend:** Tauri 2.x + React (Vite, TypeScript, Tailwind, Zustand, react-virtuoso,
  framer-motion, WaveSurfer.js). Five views — Library, Duplicates, Organize, Tags,
  Settings — plus a persistent global audio player and a "Recent operations" undo panel.
- **Rust shell:** spawns the Python sidecar, multiplexes JSON-RPC by id, re-broadcasts
  progress + per-track records as Tauri events, detects sidecar death on EOF.
- **Python sidecar (`vibechek rpc`):** the same package the CLI uses. 48 JSON-RPC
  methods, threadpool dispatch (8 workers) so fast reads interleave with long ops,
  cooperative cancellation, and all the real work (analyzer, tagger, duplicates,
  organizer, journal, profiles, config, wsl, resources, …). Notable add-ons:
  `cdj_export` (FLAC → AIFF transcode + Rekordbox-XML rewrite for older CDJs, CLI-only);
  `onnx_backend` (an opt-in, GPU-accelerated ONNX Runtime inference engine mirroring
  `analyzer.load_models`, via the `inference_engine` config field); and two opt-in genre
  sources — `clap_genre` (a pure-audio CLAP-embedding + kNN classifier over a bundled
  reference, via `genre_classifier="clap"`) and `genre_web` (a fully-local LLM that reads
  web results for artist+title, via `genre_web_lookup`) — both layered into the existing
  tag-vs-ML reconciliation in `genres.py`.
- **Auto-generated types:** `scripts/generate_ts_types.py` mirrors Python dataclasses
  into `ui/src/types/generated.ts` so the wire stays type-safe.

## What it does

| Area | Summary |
|---|---|
| **ML analysis** | Essentia + Discogs-EffNet: genre/subgenre (~400 classes), BPM, key (Camelot), energy 0-5, mood (Dark/Neutral/Bright), timeslot (Opener/Warm-Up/Peak/Afterhours), direction (Up/Steady/Down), vocal (Instrumental/Light Vocal/Vocal), danceability. Per-field confidence + a two-stage genre fallback (write the parent genre when the subgenre is unsure). |
| **Trust UX — conflict surfacing** | Genre reconciliation records where the effective value came from (your tag / audio model / web lookup) and whether the sources disagreed. When they do, the track is flagged for one-click **review** — a toolbar filter + a per-row marker — and the Track Details panel shows all three reads, which one won, and a plain-English reason. Augments, never silently overwrites, hand-curated tags. |
| **Hybrid CPU+GPU** | Analysis runs CPU-only, GPU-only, or **hybrid** — GPU workers and CPU workers share one work-stealing queue, so a VRAM-limited GPU no longer caps throughput while cores idle. Per-device throughput is measured + reported. |
| **De-duplication** | MD5 (byte-identical) + Chromaprint (acoustic — catches re-encodes/remixes). Rules-based keeper picker (codec → bitrate → size → newest → shortest path), manual override, move-to-review or trash. |
| **Organize** | Plan + execute a `Genre/Subgenre/` tree; small genres bucket into `Other/`; dry-run preview; overwrite-safe moves. |
| **Tag write** | Per-field write toggles (genre/BPM/key/energy/mood/timeslot/direction/vocal; BPM+Key default off). Tunable vocal sensitivity re-labels from the stored raw score without re-analyzing. **Always preserves Rekordbox GEOB/PRIV binary frames.** |
| **Backup / restore** | One-click snapshot of every tag (incl. binary frames) + full restore, with backup history and stale-backup warnings. |
| **Undo journal** | Organize + dedupe-move write an append-only JSONL journal (one flushed line per move). Partial runs are recoverable; finished runs revert with one click. |
| **DJ profiles** | One-click setting presets for different workflows. |
| **FLAC → CDJ export** | `vibechek cdj-export <rekordbox.xml> --out <dir>` transcodes a FLAC library to sample-identical 16-bit **AIFF** and rewrites the Rekordbox XML so beat grids + cues copy across with zero offset math — lets older Pioneer CDJs play a FLAC collection. Strictly additive (sources never touched); never MP3 (its encoder delay shifts the grid). CLI-only; optional `[cdj]` extra (soundfile) with an ffmpeg fallback. |
| **Inference engine (native / essentia_tf / opt-in ONNX)** | Default is `native` on Windows (WSL-free, in-process) and `essentia_tf` on macOS/Linux. An opt-in, GPU-accelerated **ONNX Runtime** backend (`"onnx"`) runs every neural forward pass off the EOL TF 2.5 runtime with a cross-vendor GPU EP chain (CUDA → ROCm → CoreML → CPU); validated to match the TF path end-to-end. **NVIDIA CUDA is hardware-validated** (RTX 4070, TF-free); ROCm + CoreML are wired but hardware-unverified. Provisioned via Settings → "Set up ONNX engine" into a separate `~/.vibechek/venv-onnx` (extras `[onnx]` / `[onnx-gpu]`); on Windows the `essentia_tf` and `onnx` engines run inside WSL (the native default does not). Essentia stays for DSP either way. |
| **In-app auto-update** | Opt-in `tauri-plugin-updater`: Settings → "Software updates" → check / download / install / relaunch. CI signs artifacts + publishes `latest.json` when a signing key is configured; ships inert until one is enrolled. |
| **Zero-CLI setup** | Windows desktop installs analyze in-process via the bundled native engine (no setup); the WSL fallback (CLI zip / essentia_tf / onnx) auto-installs WSL Ubuntu (UAC) + Essentia and routes through it with transparent path translation. macOS/Linux create a hermetic `~/.vibechek/venv/`. Optional one-click GPU (CUDA pip wheels). |

## Platform model

- **Windows:** a fresh install uses the **bundled native engine**
  (`inference_engine="native"`, the default since v0.6.3-beta) — WSL-free and fully
  in-process: ONNX inference + a bit-exact NumPy mel frontend + a DSP-only native
  essentia wheel (decode/BPM/key) built from the wo80 CMake fork, folded into the
  PyInstaller sidecar. It runs the *same* model weights as Mac/Linux — validated to
  identical genre top-1 and key, ±2% BPM on a 40-track parity set — so Windows users
  take no accuracy penalty; it ships CPU-only ONNX Runtime.
  `preflight.essentia_serves_engine()` routes this engine in-process. When the native
  bundle is absent (the lean CLI zip, a pip install) or the user picks
  `essentia_tf`/`onnx`, preflight **falls back to WSL**: `vibechek` in a WSL venv,
  paths translated `C:\…` ↔ `/mnt/c/…` at the boundary (the frontend never sees WSL).
  On that WSL path, when the WSL install is strictly older than the sidecar the
  analyzer **auto-upgrades it in place** on the next analyze (engine-aware, one-time,
  with a progress step) instead of refusing to dispatch; a newer WSL venv is left
  alone, a same-version "no such option" code drift triggers one in-place update +
  retry, and it hard-errors only if the automatic update itself fails. The pip source
  is pinned to the sidecar's own release tag (`git+…@v<version>`, not `main`), so the
  update converges WSL onto exactly the sidecar's version.
- **macOS / Linux:** a managed venv at `~/.vibechek/venv/` runs Essentia directly.
- **GPU:** NVIDIA CUDA runtime via PyPI wheels installed into the venv (works on any
  Linux/WSL distro, no apt/keyring/root). The Settings GPU row probes the *actual*
  analysis engine (TF inside WSL on Windows), never a host-only `nvidia-smi`.

## Distribution

- **Desktop installers** built in CI on tag push: Windows `.exe` (NSIS), Linux
  `.deb`/`.AppImage`, macOS `.dmg` (Apple Silicon). CLI archives too.
- **Code signing is opt-in.** With no cert secrets configured, builds ship **unsigned**
  — current state for the beta. macOS users clear Gatekeeper once (right-click → Open,
  or `xattr -dr com.apple.quarantine`). Signed + notarized builds are planned for
  stable. See [docs/RELEASING.md](RELEASING.md).

## Key technical decisions

1. **Hybrid CPU+GPU over single-device.** A shared work-stealing queue self-balances
   fast/slow devices instead of predictive scheduling.
2. **Store the raw vocal score.** Labels (Instrumental/Light Vocal/Vocal) are derived
   from a stored 0-1 score at tag time, so cutoffs are tunable without re-analyzing.
   Calibrated so instrumental-dance melodic leads (~0.64-0.71) stay Instrumental.
3. **Per-field write toggles** replaced the old single skip-BPM/key flag; genre is
   additionally gated by a confidence threshold; BPM/Key default off.
4. **Subgenre as main genre.** Rekordbox sorts by the main genre field, so subgenres
   (e.g. "Deep House") are written to TCON.
5. **GEOB/PRIV preservation** on every tag write — guarded by a regression test.
6. **JSON config** (not TOML): native null type, graceful load, drops unknown keys so
   adding fields is backwards-safe.
7. **Surface provenance, don't overwrite silently.** Genre reconciliation stamps each
   record with its source (`ml_genre_source`) + a conflict flag (`ml_genre_conflict`)
   and keeps the pre-reconcile audio/web reads. The library UI flags conflicts for
   review instead of presenting a black-box answer — the augment-not-overwrite stance
   a skeptical pro needs. (These fields live on the `MLResult` dataclass so the
   generated TS types carry them; defaulting `None` keeps them off the wire until
   reconciliation, so the raw record shape is unchanged.)
8. **Opt-in heavy deps stay out of the core install.** The inference engine is config-gated
   (`inference_engine`: `native` default on Windows | `essentia_tf` default on macOS/Linux |
   `onnx`) and imports `onnxruntime`/`essentia` lazily. `native` is the WSL-free Windows path —
   ONNX inference + a pure-NumPy mel frontend + an in-process native essentia wheel for
   decode/BPM/key (built via `scripts/build_native_essentia_wheel.ps1`); default unchanged.
   CDJ export's `soundfile` lives in the optional `[cdj]` extra (ffmpeg fallback otherwise).
   `numpy` is declared in the `[dev]` extra (not core) because the analysis code imports it
   lazily while the pure-logic tests import it directly — so a clean `[dev]` install can run
   them without erroring at collection.

## Project stats

<!-- These mirror the README stats line. NOTE: scripts/update_readme_stats.py
     only rewrites README.md — refresh this copy by hand when it drifts. -->
- **929 Python tests**, **48 JSON-RPC methods**, **32 Python modules**
- **104 frontend tests** (vitest + RTL + jsdom + Tauri mocks)
- Production-tested against a ~12,000-track personal DJ library

## Roadmap

See [docs/ROADMAP.md](ROADMAP.md). Near-term: polish + community launch toward
`v1.0`. Later ideas: smart-playlist export, per-genre confidence thresholds,
multi-library support, MixedInKey/Lexicon/Beatport tag import, signed macOS builds.
