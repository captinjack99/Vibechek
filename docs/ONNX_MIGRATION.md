# ONNX Runtime Migration Plan

Status: **WIRED + VALIDATED TF-FREE (2026-06-01).** The ONNX engine is selectable in the app (Settings → Analysis → Inference engine) and runs end-to-end on **plain Essentia + ONNX Runtime with zero TensorFlow** — confirmed by running the real analyzer in a plain-essentia venv (genre=Rock, vocal=Vocal 0.977 vs the TF baseline Rock/0.972; `tensorflow` never imported). The melspec linchpin is settled: plain `essentia` ships `TensorflowInputMusiCNN` and its output is **bit-identical** to the essentia-tensorflow build (no NumPy reimplementation needed — the migration's dominant risk is gone, not just reduced). The default stays `essentia_tf`. **Remaining to flip the default:** (1) host the converted head bundle on the `models-onnx-v1` release (`scripts/build_onnx_model_bundle.py` produces it + the pinned SHA256s — owner-only upload); (2) cross-platform GPU smoke tests (AMD/Intel/Apple). Parity harness: [`scripts/onnx_parity.py`](../scripts/onnx_parity.py).

This document is the contract a contributor should be able to pick up and execute against. If something here is ambiguous, file an issue before writing code.

> ## ✅ Validation results (2026-06-01) — the migration works
>
> Ran the official ONNX EffNet backbone (`discogs-effnet-bsdynamic-1.onnx`) against
> essentia-tensorflow's ground truth on a real track (Adele — "Chasing Pavements"), in WSL,
> via `scripts/onnx_parity.py`. **Result: PASS.**
>
> | Metric | TF (essentia) | ONNX | Verdict |
> |---|---|---|---|
> | Mean-embedding cosine | — | **0.99942** | ✅ (≥0.995) |
> | Genre top-class (Discogs-400) | 308 | 308 | ✅ **identical** |
> | Voice/instrumental | [0.028, 0.972] | [0.003, 0.997] | ✅ both "Vocal" |
>
> **What this proves + the exact recipe to implement:**
> - The official ONNX backbone is a faithful conversion — no self-conversion of the backbone needed.
> - **Melspec recipe:** essentia's `TensorflowInputMusiCNN` over 512/256 frames → `[N, 96]` log-mel → window into `[k, 128, 96]` patches **at hop 64** (overlapping; matches essentia's internal patching — hop 128 still gives 0.999 but 64 is tighter) → backbone ONNX.
> - **Genre comes free from the backbone:** ONNX `outputs[0]` is the 400-class genre sigmoid, `outputs[1]` is the 1280-d embedding. So only the **mood / voice / danceability** heads need a one-off `tf2onnx` conversion (trivial dense graphs); genre does not.
> - Mean-over-patches pooling matches what `analyzer.py` already does.
>
> **The one remaining de-risking item:** the melspec above used essentia's `TensorflowInputMusiCNN`.
> To actually *retire* TensorFlow, that melspec must come from either (a) the plain `essentia`
> (non-`-tensorflow`) wheel if it ships that algorithm, or (b) a small NumPy reimplementation
> **validated against `TensorflowInputMusiCNN`'s output using this same harness**. Everything
> downstream (backbone, heads, pooling, classification) is now proven. This is mechanical, not
> research.

> **Revised 2026-06-01 — verified against essentia.upf.edu (supersedes the looser earlier note).**
> The migration's *primary driver* is sound: **retire end-of-life TensorFlow 2.5** (unpatched
> CVEs, CUDA-only, pins old Python); cross-vendor GPU + a CPU speedup are bonuses. **But the
> "just download the official ONNX" shortcut is only partly real** — confirmed by reading the
> model index + the EffNet metadata JSON:
>
> 1. **EffNet backbone** — official ONNX exists (`discogs-effnet-bsdynamic-1.onnx`, dynamic
>    batch). **Its input is a mel-spectrogram tensor `[batch, 128, 96]`** (`serving_default_melspectrogram`),
>    NOT raw audio — so dropping essentia means **reimplementing essentia's mel-spectrogram
>    (`TensorflowInputMusiCNN`: 16 kHz, 128 mel bands, 96-frame patches) bit-exactly in NumPy**
>    and validating it against the C++ output. This is the dominant risk/effort and the reason
>    the estimate is ~2 weeks, not ~1.
> 2. **The EffNet classifier heads** (`genre_discogs400`, `mood_*`, `voice_instrumental`,
>    `danceability`) **have NO official ONNX — only `.pb`.** They are tiny graphs
>    (embedding → dense → sigmoid/softmax), so a one-off `tf2onnx` conversion is trivial — but
>    it IS a conversion step (old §4 does *not* go away for the heads). Outputs are read from
>    the session at runtime, so no hardcoded tensor names needed.
> 3. **MAEST** *does* ship full ONNX (`genre_discogs400-discogs-maest-*.onnx`) and is the
>    ONNX-native, current-best replacement for the **genre** path specifically. It does NOT
>    provide mood/voice/danceability (those stay EffNet-conditioned → still need converted
>    heads). So MAEST is an option for genre, not a wholesale replacement.
>
> **Net plan:** EffNet backbone ONNX + a NumPy melspec validated against essentia + `tf2onnx`
> for the four heads (and/or MAEST for genre). Route the tiny heads to CPU even in GPU mode
> (GPU-neutral). Triage the CC BY-NC (non-commercial) model licenses if there's commercial
> intent. **A parity harness run against the real models is a hard prerequisite to flipping
> the default** — shipping an unvalidated melspec would silently misclassify every track.
> Sections below predate this note; read §4 with these corrections.

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

## 4. Models — mixed: official ONNX for the backbone, convert the heads

**Verified against essentia.upf.edu (2026-06-01) — supersedes the earlier "all official ONNX" claim:**

1. **EffNet backbone** — use the official **`discogs-effnet-bsdynamic-1.onnx`** (dynamic batch). Its input is **`serving_default_melspectrogram`, shape `[batch, 128, 96]`** with outputs `PartitionedCall:0` (style probs) + `PartitionedCall:1` (the 1280-d embedding we actually use). The melspectrogram is **NOT** baked into the ONNX graph — essentia computed it in C++ (`TensorflowInputMusiCNN`), so you must reimplement it in NumPy (16 kHz, 128 mel bands, 96-frame patches) and validate bit-exactly against essentia's output. This is the real work (see §3, the top note, and the timeline).
2. **Classification heads** (`genre_discogs400`, `mood_aggressive/happy/relaxed/sad`, `voice_instrumental`, `danceability`) — **NO official ONNX exists; only `.pb`.** They are trivial graphs (1280-d embedding → dense → sigmoid/softmax), so a one-off `python -m tf2onnx.convert` (§4.1) per head is the path. Read input/output names from the resulting ONNX at runtime (`session.get_inputs()/get_outputs()`); don't hardcode. Keep the conversion script in `scripts/convert_models_to_onnx.py`, excluded from the wheel.
3. **MAEST (optional, genre only)** — `genre_discogs400-discogs-maest-*.onnx` ships official ONNX and is the current-best open tagger for the **genre** path. It does NOT cover mood/voice/danceability (those stay EffNet-conditioned → still need the converted heads from #2). Treat MAEST as an opt-in "high accuracy" genre profile; budget time to re-tune the EffNet-calibrated thresholds (`VOCAL_INSTRUMENTAL_MAX`, `VOCAL_FULL_MIN`, `_MOOD_INDEX`, energy-blend weights) since its score distribution differs.

> **License:** several Discogs models are **CC BY-NC** (non-commercial). Hosting the converted head ONNX on our own release is fine for an OSS project, but the **usage** license matters if Vibechek ever has commercial intent — triage separately (true of today's `.pb` models too).

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
| Backbone ONNX download + `tf2onnx`-convert the 6 heads + hashes | 1 day |
| **Mel-spectrogram reimpl in NumPy, validated bit-exact vs essentia** | **3–5 days** ⚠️ dominant |
| `load_models()` / `analyze_track()` ONNX path (EP chain, heads→CPU) | 2 days |
| Feature flag + config plumbing         | 1 day    |
| Parity test harness + fixture (needs a real model runtime) | 2 days |
| (Optional) MAEST genre backbone A/B + threshold re-tune | 3–5 days |
| Cross-platform smoke testing (Win AMD/Intel, Apple Silicon, Linux) | 2 days |
| Docs (USER_GUIDE, INSTALL, ROADMAP)    | 1 day    |
| **Total (core, EffNet via ONNX)**      | **~2 weeks** |

The mel-spectrogram parity is the dominant item and the reason this can't be done "blind": the ONNX backbone takes a melspec tensor, essentia computed that melspec in C++, and the NumPy reimplementation must match it closely enough that genres/moods don't drift — which is only provable by running both engines on real audio.
| **+ MAEST backbone (optional)**        | **+3–5 days** |

Because we consume MTG's official ONNX (pre-processing baked in), the old "port the mel-spectrogram to NumPy and validate bit-for-bit" risk largely disappears — that was the main reason for the original 3-week estimate. Verify the official ONNX includes pre-processing per model before relying on it.

---

## 9. Open questions

- Do we package `onnxruntime` (CPU-only, ~12 MB) by default and let users `pip install onnxruntime-gpu` / `onnxruntime-directml` themselves, or ship platform-specific extras (`vibechek[gpu-cuda]`, `vibechek[gpu-directml]`)? Recommendation: platform extras, mirroring the current `vibechek[ml]` pattern.
- **Route the tiny classifier heads to CPU even in GPU mode.** The audit's research found the small heads are GPU-neutral-to-negative on integrated GPUs (kernel-launch + transfer overhead dominates); only the EffNet/MAEST **backbone** is worth accelerating. So build the EP chain for the backbone session but pin the head sessions to `CPUExecutionProvider`, and do **not** market "DirectML 3–10× faster" as a blanket claim. Benchmark the backbone per EP and report real numbers.
- **License:** several Discogs models are **CC BY-NC** (non-commercial). Hosting is moot (we use MTG's official ONNX), but if Vibechek pursues commercial distribution, the *usage* license must be cleared — track separately; it applies to today's `.pb` models too.
