# ONNX Runtime Migration Plan

Status: **Proposal / spec.** No code yet. Owner: TBD. Estimated effort: **~1 week** for one engineer with Python + ML experience (revised down — see below).

This document is the contract a contributor should be able to pick up and execute against. If something here is ambiguous, file an issue before writing code.

> **Revised 2026-06-01 (audit).** The original plan assumed we must *self-convert* the
> `.pb` models with `tf2onnx` and re-host them. That premise was wrong: **MTG already
> publishes official, dynamic-batch `.onnx` for the EffNet backbone + heads**, so the
> conversion step (old §4) and the re-hosting license question (old §9) largely go away —
> we just point `download_models()` at MTG's ONNX URLs. The migration is therefore reframed
> from *"convert models"* to **"retire end-of-life TensorFlow 2.5"**, whose real driver is
> **security/maintainability** (TF 2.5 is EOL with unpatched CVEs, CUDA-only, and pins old
> Python) — cross-vendor GPU + a CPU speedup are bonuses. Also: the EffNet **genre head**
> should be *replaced* by the ONNX-native **MAEST** head (current best open tagger), not
> converted; route the tiny classifier heads to CPU even in GPU mode (they're GPU-neutral);
> and triage the model licenses (several are CC BY-NC — non-commercial) if there's commercial
> intent — that's true today regardless of ONNX. Sections below are kept for reference;
> where they say "convert", read "download the official ONNX" instead.

---

## 1. Why ONNX

**Primary driver — retire end-of-life TensorFlow.** `essentia-tensorflow` bundles **TensorFlow 2.5**, which is past end-of-life: no security patches, several known CVEs in the bundled runtime, CUDA-only, and it pins an old Python that increasingly blocks dependency upgrades across the project. That alone justifies the move — a deprecated, unpatched ML runtime shipping in a desktop app is a liability independent of any feature win.

**Secondary driver — cross-vendor GPU + CPU speed.** Vibechek's ML pipeline (`vibechek/analyzer.py`) wraps **`essentia-tensorflow`**, whose **CUDA-only** TF wheels give GPU acceleration exclusively on NVIDIA cards with a matching CUDA toolkit. ONNX Runtime's CPU build also generally beats TF 2.5's CPU path.

That excludes:
- **AMD GPUs** on Windows and Linux (no ROCm support in the bundled TF build)
- **Intel GPUs** (Arc, Iris Xe) on Windows
- **Apple Silicon** (M1/M2/M3/M4) — no Metal support
- **DirectML** users on Windows who don't have CUDA installed

Today, every non-NVIDIA user falls back to CPU inference, which on a typical 1000-track library is the difference between a coffee break and a multi-hour wait. Users have flagged this more than once.

**ONNX Runtime** solves this directly. Its **Execution Provider (EP)** architecture lets a single runtime dispatch to:

- `CUDAExecutionProvider`     — NVIDIA
- `DirectMLExecutionProvider` — Windows (any DX12-capable GPU: NVIDIA, AMD, Intel)
- `CoreMLExecutionProvider`   — Apple Silicon
- `ROCmExecutionProvider`     — AMD on Linux
- `CPUExecutionProvider`      — always-on fallback

One runtime, one wheel, one code path — every GPU vendor covered.

---

## 2. What stays the same

**Everything that isn't TF inference.** Essentia's signal-processing primitives are pure C++ DSP, no TF dependency. We keep using them as-is:

- `essentia.standard.MonoLoader`         — audio decode + resample
- `essentia.standard.RhythmExtractor2013` — BPM
- `essentia.standard.KeyExtractor`        — musical key (combined with `vibechek/keys.py` Camelot mapping)

These live in the plain `essentia` package (no `-tensorflow` suffix), so once we drop the TF dependency these still install on every platform.

The model download / mirror / SHA256 pipeline in `download_models()` also stays. The only changes are file extensions (`.pb` → `.onnx`) and which hashes we check.

---

## 3. What changes

The TF-backed inference calls in `load_models()` and `analyze_track()` get replaced. Concretely, in `vibechek/analyzer.py`:

| Today                                             | After                                              |
| ------------------------------------------------- | -------------------------------------------------- |
| `from essentia.standard import TensorflowPredict2D, TensorflowPredictEffnetDiscogs` | `import onnxruntime as ort` |
| `loaded["effnet"] = TensorflowPredictEffnetDiscogs(graphFilename=..., output="PartitionedCall:1")` | `loaded["effnet"] = ort.InferenceSession(path, providers=PROVIDERS)` |
| `loaded["genre"] = TensorflowPredict2D(graphFilename=..., input=..., output=...)` | same — `InferenceSession`, input/output names live in the ONNX metadata |
| `predictions = model(audio)` (essentia call form) | `predictions = session.run([out_name], {in_name: audio_tensor})[0]` |

The effnet feature-extractor previously took raw 16 kHz audio and ran the spectrogram + the CNN inside essentia. After the conversion, **the ONNX graph must include the same pre-processing** (mel spectrogram, framing). `tf2onnx` will export whatever's in the TF graph; if essentia was doing pre-processing externally, we need to either:

1. Port that pre-processing to a small Python function, or
2. Re-export the model with the pre-processing layers included.

Investigate before assuming. Check `essentia/src/algorithms/machinelearning/tensorflowpredicteffnetdiscogs.cpp` in the essentia source to confirm what's TF-internal vs. C++-external.

---

## 4. Models — use MTG's official ONNX (do NOT self-convert)

**Revised:** the original "convert every `.pb` with `tf2onnx`" plan is unnecessary. The MTG / Essentia model index publishes **official `.onnx` exports** (dynamic batch) for the Discogs-EffNet backbone and the classifier heads, with the pre-processing already baked into the graph. So:

1. For each model in `analyzer.MODELS`, find its **official `.onnx`** on <https://essentia.upf.edu/models.html> (each model page lists a `.onnx` alongside the `.pb`). Point `download_models()` at those URLs (and the `.json` metadata, which is unchanged).
2. **Replace, don't convert, the genre head.** The EffNet `genre_discogs400` head is the weakest/oldest piece; swap the backbone+head to **MAEST** (`discogs-maest-*`, ONNX-native, current best open music tagger — HF `mtg-upf/discogs-maest-*`, ISMIR 2023). Gate it behind the feature flag (§6), A/B on the parity fixture (§7), and **budget time to re-tune the EffNet-calibrated thresholds** (`VOCAL_INSTRUMENTAL_MAX`, `VOCAL_FULL_MIN`, `_MOOD_INDEX`, energy-blend weights) since MAEST's score distribution differs. Ship MAEST as an opt-in "high accuracy" profile if its CPU latency is unacceptable.
3. The handful of heads MTG does **not** publish as ONNX (if any) are the only candidates for a one-off `tf2onnx` conversion — check first; in practice the heads take EffNet embeddings and have no pre-processing, so they convert trivially if needed. Keep any such script in `scripts/convert_models_to_onnx.py`, excluded from the wheel.

> Because we're consuming MTG's *official* ONNX, the old "re-host our own converted weights + verify the license allows it" question is moot for hosting — but the **usage** license still matters: several Discogs models are **CC BY-NC** (non-commercial). If Vibechek ever has commercial intent, triage this separately (it's true of the current `.pb` models too).

### 4.2 Hash + mirror update

The current `_DEFAULT_MODEL_BASE_URLS` chain is:

```
https://essentia.upf.edu/models                                  (upstream, .pb only)
https://github.com/papapew/Vibechek/releases/download/models-v1  (our mirror)
```

After conversion:

1. Upload the new `.onnx` and `.json` (metadata is unchanged) files to a new GitHub Release tag, e.g. `models-onnx-v1`.
2. Compute SHA256 for each `.onnx` and add a `MODEL_HASHES` constant to `vibechek/analyzer.py` (the field doesn't exist today — `download_models()` only validates Content-Length). Add per-file verification.
3. Bump `MODEL_BASE_URLS` to point at the new release tag. Drop the UPF URL from the fallback chain (they don't host `.onnx` files).
4. Update `MODELS` entries to reference `.onnx` filenames.

### 4.3 Conversion repo

The conversion script doesn't need to ship in Vibechek — it's a one-time job. Keep it in a separate folder (`scripts/convert_models_to_onnx.py`) so we can re-run it if essentia publishes a new model version, but exclude it from the wheel.

---

## 5. Execution provider chain

In `load_models()`, build the provider list once and pass it to every `InferenceSession`:

```python
def _build_providers() -> list[str]:
    """Return providers in priority order; ORT silently skips any not available."""
    available = ort.get_available_providers()
    preferred = [
        "CUDAExecutionProvider",     # NVIDIA Linux/Windows
        "ROCmExecutionProvider",     # AMD Linux
        "DirectMLExecutionProvider", # Windows DX12 (NVIDIA/AMD/Intel)
        "CoreMLExecutionProvider",   # macOS Apple Silicon
        "CPUExecutionProvider",      # always present, last resort
    ]
    chain = [p for p in preferred if p in available]
    log.info("ONNX EPs (in order): %s", chain)
    return chain
```

ORT picks the first provider that supports each op, falling through gracefully — no manual probing needed. Log the chosen chain at startup so user bug reports include it.

Respect the existing `apply_gpu_preference()` hook from `vibechek/resources.py`: if the user has explicitly disabled GPU, return `["CPUExecutionProvider"]` only.

---

## 6. Backward compatibility / feature flag

We **do not** rip out essentia-tensorflow on day one. The two engines coexist behind a config flag during the transition.

In `vibechek/config.py`'s `AnalysisConfig`, add:

```python
inference_engine: Literal["onnx", "essentia_tf"] = "onnx"
```

Default to `"onnx"` once it's tested. `load_models()` and `analyze_track()` branch on `cfg.inference_engine`. The TF code path stays untouched; we just stop installing `essentia-tensorflow` by default. Users who already have it set keep working.

Removal timeline:
- **v0.4.x** — ship both engines, default = `onnx`, document the flag in `docs/USER_GUIDE.md`
- **v0.5.0** — remove the essentia-tensorflow branch and the flag

---

## 7. Test plan — confidence parity

The risk is silent numerical drift: ONNX rounds slightly differently than TF, and we don't want every user's library to suddenly re-classify.

### Parity harness

1. **Fixture**: 100 tracks spanning the genre spread (techno, jazz, ambient, rock, vocal, instrumental, high-BPM, low-BPM). Pull these from the existing `tests/` audio fixtures plus a curated set committed under `tests/fixtures/parity/` (or referenced via a manifest of public-domain URLs).

2. **Run both engines** on the same library, writing two `analysis.json` files.

3. **Compare** with a new test module `tests/test_onnx_parity.py`:
   - `ml_genre`, `ml_subgenre`, `ml_mood`, `ml_vocal`: exact string match required for ≥95% of tracks. Log mismatches but don't fail unless the rate drops below threshold.
   - `ml_genre_confidence`, `ml_danceability`: absolute difference < **0.05**.
   - `ml_bpm`, `ml_key`: must match exactly — these come from essentia's signal-processing path, which doesn't change, so any drift is a bug in the test harness.
   - `ml_energy` (integer 1–5): off-by-one allowed for ≤10% of tracks.

4. **Performance check**: log wall-clock per-track on the same hardware (CPU baseline + each GPU EP). Document in the PR description. Expected: CUDA ≈ parity with TF/CUDA today, DirectML ≈ 3–10× CPU on consumer AMD/Intel GPUs.

### Pre-merge gates

- Parity test green
- Existing `tests/` suite green (316 tests today, mainly non-ML — they should be unaffected)
- Manual smoke: install on a fresh AMD Windows box and a fresh M-series Mac, run a 50-track library end-to-end

---

## 8. Timeline (best estimate)

| Phase                                  | Effort   |
| -------------------------------------- | -------- |
| Point `download_models()` at MTG's official ONNX URLs + hashes | 0.5 day |
| `load_models()` / `analyze_track()` ONNX path (EP chain, heads→CPU) | 2–3 days |
| Feature flag + config plumbing         | 1 day    |
| Parity test harness + fixture          | 2 days   |
| (Optional) MAEST backbone A/B + threshold re-tune | 3–5 days |
| Cross-platform smoke testing (Win AMD/Intel, Apple Silicon, Linux) | 2 days |
| Docs (USER_GUIDE, INSTALL, ROADMAP)    | 1 day    |
| **Total (core, official ONNX)**        | **~1 week** |
| **+ MAEST backbone (optional)**        | **+3–5 days** |

Because we consume MTG's official ONNX (pre-processing baked in), the old "port the mel-spectrogram to NumPy and validate bit-for-bit" risk largely disappears — that was the main reason for the original 3-week estimate. Verify the official ONNX includes pre-processing per model before relying on it.

---

## 9. Open questions

- Do we package `onnxruntime` (CPU-only, ~12 MB) by default and let users `pip install onnxruntime-gpu` / `onnxruntime-directml` themselves, or ship platform-specific extras (`vibechek[gpu-cuda]`, `vibechek[gpu-directml]`)? Recommendation: platform extras, mirroring the current `vibechek[ml]` pattern.
- **Route the tiny classifier heads to CPU even in GPU mode.** The audit's research found the small heads are GPU-neutral-to-negative on integrated GPUs (kernel-launch + transfer overhead dominates); only the EffNet/MAEST **backbone** is worth accelerating. So build the EP chain for the backbone session but pin the head sessions to `CPUExecutionProvider`, and do **not** market "DirectML 3–10× faster" as a blanket claim. Benchmark the backbone per EP and report real numbers.
- **License:** several Discogs models are **CC BY-NC** (non-commercial). Hosting is moot (we use MTG's official ONNX), but if Vibechek pursues commercial distribution, the *usage* license must be cleared — track separately; it applies to today's `.pb` models too.
