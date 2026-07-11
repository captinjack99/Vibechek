"""ONNX Runtime inference backend for the Discogs-EffNet analysis pipeline.

This module replaces the *inference* half of `vibechek.analyzer`'s
essentia-tensorflow path with `onnxruntime`, while STILL using essentia for the
pure-DSP work (MonoLoader audio decode/resample, the MusiCNN mel-spectrogram,
RhythmExtractor2013 BPM, KeyExtractor). The motivation and the validated recipe
are documented in `docs/ONNX_MIGRATION.md`:

  * the official ONNX EffNet backbone (`discogs-effnet-bsdynamic-1.onnx`) takes a
    mel-spectrogram tensor `[batch, 128, 96]` (NOT raw audio) and emits
    `outputs[0]` = 400-class genre sigmoid and `outputs[1]` = the 1280-d
    embedding the heads consume;
  * the classification heads (`genre_discogs400`, `danceability`,
    `voice_instrumental`, `mood_aggressive/happy/relaxed/sad`) have no official
    ONNX, so they are converted one-off with `tf2onnx` (see
    `scripts/convert_heads_to_onnx.py`) — they are tiny dense graphs run on CPU.

Why this is the *inference* swap, not the *DSP* swap: essentia's
`TensorflowInputMusiCNN` still produces the mel-spectrogram here. Retiring TF
entirely (a NumPy melspec) is a separate, later step — this backend already lets
ONNX Runtime do every neural-net forward pass, which is what unlocks
cross-vendor GPU (CUDA / ROCm / DirectML / CoreML) and drops the EOL TF 2.5
runtime from the inference path.

Public surface (mirrors `analyzer.load_models` exactly so `analyze_audio_features`
and ALL downstream logic stay byte-unchanged):

    build_providers(use_gpu)  -> list[str]
    load_onnx_models(model_dir, use_gpu="auto") -> dict[str, Any]

`load_onnx_models` returns the SAME dict keys as `analyzer.load_models`:
`effnet`, `genre`, `genre_classes`, `danceability`, `voice_instrumental`,
`aggressive`, `happy`, `relaxed`, `sad`, plus a `<name>_classes` list per head
whose metadata carries one. Each callable has the identical signature its
essentia counterpart had:

    effnet(audio: np.ndarray)      -> np.ndarray[k, 1280]
    <head>(embedding: np.ndarray)  -> np.ndarray[k, n_classes]

so the only place that has to know an engine is in play is `load_models`.

Nothing here imports `onnxruntime` or `essentia` at module load — both are
imported lazily inside the functions that need them, with a clear error if
missing, so the package (and the unit suite) stays importable on a machine that
has neither installed.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import numpy as np

log = logging.getLogger(__name__)

# Backbone ONNX: the official MTG export. Its single input is the
# mel-spectrogram tensor [batch, 128, 96]; outputs[0] is the 400-class genre
# sigmoid and outputs[1] is the 1280-d embedding. We read the input name from
# the session at runtime (see _OnnxEffnet) rather than hardcoding it.
BACKBONE_ONNX_FILENAME = "discogs-effnet-bsdynamic-1.onnx"
BACKBONE_ONNX_URL = (
    "https://essentia.upf.edu/models/feature-extractors/discogs-effnet/"
    "discogs-effnet-bsdynamic-1.onnx"
)


def bundled_onnx_assets_dir() -> Path | None:
    """Directory holding the bundled converted ONNX heads, or None if absent.

    The classification heads are tiny (~5 MB total) and NOT hosted upstream
    (essentia ships only the `.pb` originals + the 18 MB EffNet backbone), so we
    ship them inside the app: PyInstaller bundles `vibechek/onnx_assets/` into
    the frozen sidecar (-> `<_MEIPASS>/onnx_assets`); a source/dev install reads
    them straight from `vibechek/onnx_assets/`. The ONNX setup flow copies these
    into `<models>/onnx/` so the engine works with NO download of the heads (only
    the official backbone is fetched, from essentia). Returns the first dir that
    exists and actually contains `.onnx` files.
    """
    import sys  # noqa: PLC0415

    candidates: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / "onnx_assets")
    candidates.append(Path(__file__).resolve().parent / "onnx_assets")
    for c in candidates:
        try:
            if c.is_dir() and any(c.glob("*.onnx")):
                return c
        except OSError:
            continue
    return None


# Must match analyzer._ONNX_SUBDIR (kept literal here to avoid importing the
# heavy analyzer module just for a constant).
_ONNX_SUBDIR = "onnx"


def stage_bundled_onnx_heads(model_dir: Path, on_progress=None) -> dict:
    """Copy the bundled converted heads into ``<model_dir>/onnx`` for offline use.

    Self-healing: creates the subdir, removes any leftover ``.partial`` files
    from an interrupted download, and overwrites stale/wrong head files with the
    bundled (hash-pinned) ones. The EffNet backbone is NOT bundled — it is
    fetched separately from essentia. Returns ``{"staged": [...], "source":
    str|None}`` (source is None when no bundle is present, e.g. a lean dev run).
    """
    import shutil  # noqa: PLC0415

    onnx_dir = Path(model_dir) / _ONNX_SUBDIR
    onnx_dir.mkdir(parents=True, exist_ok=True)
    # Clean interrupted-download leftovers so a retry starts clean.
    for stale in onnx_dir.glob("*.partial"):
        try:
            stale.unlink()
        except OSError:
            pass

    src = bundled_onnx_assets_dir()
    if src is None:
        log.warning("No bundled ONNX heads found to stage (lean build?).")
        return {"staged": [], "source": None}

    files = sorted([*src.glob("*.onnx"), *src.glob("*.json")])
    staged: list[str] = []
    for i, f in enumerate(files):
        shutil.copy2(f, onnx_dir / f.name)
        staged.append(f.name)
        if on_progress:
            on_progress(i + 1, len(files), f"Staging {f.name}")
    log.info("Staged %d bundled ONNX head files into %s", len(staged), onnx_dir)
    return {"staged": staged, "source": str(src)}

# Mel-spectrogram patching geometry (validated in scripts/onnx_parity.py:
# cosine 0.99942 vs essentia-tensorflow on a real track).
#   frameSize=512 / hopSize=256 → [N, 96] log-mel frames (per essentia MusiCNN)
#   window into [k, 128, 96] patches at hop PATCH_HOP (overlapping; matches
#   essentia's internal patching) → backbone → per-patch embeddings → mean.
PATCH_FRAMES = 128  # frames per patch (the backbone's fixed time dimension)
PATCH_HOP = 64  # frame stride between patches (tighter than 128; both pass)
MEL_FRAME_SIZE = 512
MEL_HOP_SIZE = 256

# The heads ALWAYS run on CPU. The audit found the tiny dense heads are
# GPU-neutral-to-negative (kernel-launch + host↔device transfer overhead
# dominates); only the backbone is worth a GPU EP. See docs/ONNX_MIGRATION.md §9.
_HEAD_PROVIDERS = ["CPUExecutionProvider"]

# Head model names whose .onnx must exist in the model dir, paired with the
# essentia descriptor key analyzer.load_models uses. genre_discogs400 maps to
# the dict key "genre" (matching the essentia path).
_HEAD_SPECS: tuple[tuple[str, str], ...] = (
    ("genre_discogs400", "genre"),
    ("danceability", "danceability"),
    ("voice_instrumental", "voice_instrumental"),
    ("mood_aggressive", "aggressive"),
    ("mood_happy", "happy"),
    ("mood_relaxed", "relaxed"),
    ("mood_sad", "sad"),
)


def build_providers(use_gpu: str) -> list[str]:
    """Return the ONNX Runtime execution-provider chain for the BACKBONE session.

    The chain is the cross-vendor priority order from docs/ONNX_MIGRATION.md §5,
    filtered to whatever `onnxruntime.get_available_providers()` reports on this
    build. ORT picks the first provider that supports each op and falls through
    gracefully, so listing an unavailable provider is harmless — but we filter
    anyway so the logged chain reflects reality in bug reports.

    `use_gpu="off"` forces `["CPUExecutionProvider"]` only (the explicit
    user-disabled-GPU case). "auto"/"on" use the full filtered chain. CPU is
    always appended as the last-resort fallback.

    Only the backbone uses this chain; the heads always run on CPU.
    """
    import onnxruntime as ort  # noqa: PLC0415 (lazy — keeps the module import-safe)

    available = set(ort.get_available_providers())

    if use_gpu == "off":
        # CPU-only when present (it always is on a real ORT build); empty list
        # lets ORT default, but we prefer to be explicit for the log line.
        return ["CPUExecutionProvider"] if "CPUExecutionProvider" in available else []

    preferred = [
        "CUDAExecutionProvider",      # NVIDIA Linux/Windows
        "ROCmExecutionProvider",      # AMD Linux
        "DirectMLExecutionProvider",  # Windows DX12 (NVIDIA/AMD/Intel)
        "CoreMLExecutionProvider",    # macOS Apple Silicon
        "CPUExecutionProvider",       # always-present last resort
    ]
    chain = [p for p in preferred if p in available]
    # Guarantee a CPU fallback even on an exotic build that didn't report it in
    # `preferred` order (defensive — every real build ships CPU).
    if "CPUExecutionProvider" in available and "CPUExecutionProvider" not in chain:
        chain.append("CPUExecutionProvider")
    return chain


def _patch_indices(n_frames: int, patch_frames: int = PATCH_FRAMES, hop: int = PATCH_HOP) -> list[int]:
    """Return the start-frame index of each [patch_frames, 96] window.

    Pure function (no numpy/essentia) so it can be unit-tested in isolation.
    Mirrors `scripts/onnx_parity.py`:

        idx = list(range(0, n_frames - patch_frames + 1, hop)) or [0]

    When the clip is shorter than one patch (`n_frames < patch_frames`) there is
    no full window, so we return `[0]` and the caller pads/zero-fills that single
    short patch (essentia does the same — every track yields at least one patch).
    """
    idx = list(range(0, n_frames - patch_frames + 1, hop))
    return idx or [0]


def _make_patches(mel: np.ndarray, patch_frames: int = PATCH_FRAMES, hop: int = PATCH_HOP) -> np.ndarray:
    """Window a `[N, 96]` log-mel array into `[k, patch_frames, 96]` patches.

    Uses `_patch_indices` for the start positions. A final clip shorter than one
    patch is zero-padded up to `patch_frames` so the backbone's fixed time
    dimension is satisfied (yields a single padded patch).
    """
    import numpy as np  # noqa: PLC0415

    # An empty/effectively-empty decode (audio shorter than one 512-sample frame
    # at 16 kHz) yields zero mel frames — a 1-D `(0,)` array with no `shape[1]`.
    # Honour the "every track yields at least one patch" contract by returning a
    # single zero patch instead of crashing on `mel.shape[1]` (matches essentia).
    if mel.ndim < 2 or mel.shape[0] == 0:
        return np.zeros((1, patch_frames, 96), dtype=np.float32)

    n_frames = int(mel.shape[0])
    idx = _patch_indices(n_frames, patch_frames, hop)
    if n_frames < patch_frames:
        pad = np.zeros((patch_frames, mel.shape[1]), dtype=np.float32)
        pad[:n_frames] = mel
        return pad[np.newaxis, :, :].astype(np.float32)
    return np.stack([mel[i:i + patch_frames] for i in idx]).astype(np.float32)


class _OnnxEffnet:
    """Backbone callable: raw 16 kHz audio → `[k, 1280]` per-patch embeddings.

    Mirrors essentia's `TensorflowPredictEffnetDiscogs(output="PartitionedCall:1")`
    call form so `analyze_audio_features` is unchanged: it returns the per-patch
    (un-pooled) embedding matrix and the analyzer means over axis 0 downstream.

    Pipeline (the validated recipe):
        audio --MusiCNN melspec (essentia OR numpy)--> [N, 96]
              --window hop 64-----------------------> [k, 128, 96]
              --backbone ONNX-----------------------> outputs[1] = [k, 1280]

    The mel-spectrogram is produced by essentia's `TensorflowInputMusiCNN` by
    default, OR by the pure-NumPy `vibechek.numpy_frontend` when `numpy_frontend`
    is set — the latter is the last step to a fully essentia-free (WSL-free)
    native-Windows analyze path. The NumPy frontend is a bit-close reproduction
    (validated: embedding cosine 1.00000, genre top-1 5/5 vs essentia); see
    `numpy_frontend` and `scripts/native_frontend_parity.py`. Default stays
    essentia until the full gold-corpus parity gate is run.

    The 400-class genre activations (`outputs[0]`) are cached per call into
    `self.last_genre_activations` so the genre head can reuse them instead of
    re-running the backbone — see `_OnnxBackboneGenre`.
    """

    def __init__(self, session: Any, numpy_frontend: bool = False) -> None:
        self._sess = session
        self._input_name = session.get_inputs()[0].name
        self._numpy_frontend = numpy_frontend
        # Per-call cache so the "genre" callable can pull the backbone's own
        # 400-class output for the SAME audio without a second forward pass.
        self.last_genre_activations: np.ndarray | None = None

    def _melspec(self, audio: np.ndarray) -> np.ndarray:
        import numpy as np  # noqa: PLC0415

        if self._numpy_frontend:
            # Pure-NumPy path — no essentia. This is what removes the last
            # essentia dependency from the ONNX inference pipeline on Windows.
            from vibechek.numpy_frontend import musicnn_mel  # noqa: PLC0415

            return musicnn_mel(audio)

        import essentia.standard as es  # noqa: PLC0415

        musicnn = es.TensorflowInputMusiCNN()
        frames = es.FrameGenerator(
            audio, frameSize=MEL_FRAME_SIZE, hopSize=MEL_HOP_SIZE, startFromZero=True
        )
        return np.array([musicnn(fr) for fr in frames], dtype=np.float32)

    def __call__(self, audio: np.ndarray) -> np.ndarray:
        mel = self._melspec(audio)  # [N, 96]
        patches = _make_patches(mel)  # [k, 128, 96]
        activations, embeddings = self._sess.run(None, {self._input_name: patches})
        self.last_genre_activations = activations  # [k, 400]
        return embeddings  # [k, 1280]


class _OnnxBackboneGenre:
    """Genre callable backed by the backbone's own 400-class output.

    The official backbone already emits the Discogs-400 genre sigmoid
    (`outputs[0]`) — there is no separate genre forward pass needed. This thin
    callable returns the activations the backbone cached on its last `__call__`,
    matching the essentia `genre(embedding) -> [k, 400]` signature so
    `analyze_audio_features` (which does `genre(embeddings)` then means) is
    unchanged.

    Defensive: if for some reason the backbone hasn't run yet (it always has,
    since the analyzer calls `effnet(audio)` first), fall back to the converted
    `genre_discogs400.onnx` head if present, else raise.
    """

    def __init__(self, effnet: _OnnxEffnet, fallback_head: _OnnxHead | None) -> None:
        self._effnet = effnet
        self._fallback = fallback_head

    def __call__(self, embedding: np.ndarray) -> np.ndarray:
        cached = self._effnet.last_genre_activations
        if cached is not None:
            return cached
        if self._fallback is not None:
            return self._fallback(embedding)
        raise RuntimeError(
            "Genre activations unavailable: backbone produced no cached output "
            "and no genre_discogs400.onnx head was loaded."
        )


class _OnnxHead:
    """Classification-head callable: `[k, 1280]` embedding → `[k, n_classes]`.

    Mirrors essentia's `TensorflowPredict2D(input=..., output=...)` call form.
    Input/output node names are read from the converted ONNX session metadata
    at construction time (NOT hardcoded), and the session is pinned to CPU
    (heads are GPU-neutral).
    """

    def __init__(self, session: Any) -> None:
        self._sess = session
        self._input_name = session.get_inputs()[0].name

    def __call__(self, embedding: np.ndarray) -> np.ndarray:
        import numpy as np  # noqa: PLC0415

        emb = np.atleast_2d(np.asarray(embedding, dtype=np.float32))
        out = self._sess.run(None, {self._input_name: emb})
        return out[0]


def _load_head_session(onnx_path: Path) -> Any:
    """Build a CPU InferenceSession for a head .onnx, or raise a clear error."""
    import onnxruntime as ort  # noqa: PLC0415

    if not onnx_path.exists():
        raise RuntimeError(
            f"Head model not found: {onnx_path.name}. The ONNX backend needs the "
            f"classification heads converted from their .pb files. Run:\n"
            f"    python scripts/convert_heads_to_onnx.py --models {onnx_path.parent}\n"
            f"(one-time dev/setup step — essentia does not host ONNX heads). See "
            f"docs/ONNX_MIGRATION.md §4."
        )
    return ort.InferenceSession(str(onnx_path), providers=_HEAD_PROVIDERS)


def _read_classes(metadata_path: Path) -> list[str]:
    """Return the `classes` list from a head's .json metadata, or [] if absent."""
    if not metadata_path.exists():
        return []
    try:
        meta = json.loads(metadata_path.read_text(encoding="utf-8"))
        classes = meta.get("classes")
        return classes if isinstance(classes, list) else []
    except Exception as e:  # noqa: BLE001
        log.warning("Bad head metadata %s: %s", metadata_path.name, e)
        return []


def load_onnx_models(
    model_dir: Path, use_gpu: str = "auto", numpy_frontend: bool | None = None
) -> dict[str, Any]:
    """Load the ONNX-backed model set, mirroring `analyzer.load_models`'s dict.

    Returns callables with IDENTICAL signatures to the essentia path so the
    analyzer's downstream logic is byte-unchanged. Keys:
        effnet, genre, genre_classes, danceability, voice_instrumental,
        aggressive, happy, relaxed, sad, and <name>_classes per head with
        metadata.

    The backbone uses the GPU EP chain (`build_providers(use_gpu)`); the heads
    always run on CPU. `onnxruntime` and `essentia` are imported lazily with a
    clear error if missing. A missing head .onnx raises pointing at
    `scripts/convert_heads_to_onnx.py`.
    """
    try:
        import onnxruntime as ort  # noqa: PLC0415
    except ImportError as e:
        raise RuntimeError(
            "onnxruntime is not installed but inference_engine='onnx' was "
            "selected. Install it (CPU: `pip install onnxruntime`; GPU: "
            "`pip install onnxruntime-gpu` / `onnxruntime-directml`), or switch "
            "back to inference_engine='essentia_tf'."
        ) from e

    model_dir = Path(model_dir)
    backbone_path = model_dir / BACKBONE_ONNX_FILENAME
    if not backbone_path.exists():
        raise RuntimeError(
            f"Backbone ONNX not found: {backbone_path}. Download it from "
            f"{BACKBONE_ONNX_URL} (or run the model download with the onnx "
            f"engine selected). See docs/ONNX_MIGRATION.md §4."
        )

    # Make the GPU execution providers actually usable. onnxruntime-gpu's
    # provider library dlopens the CUDA/cuDNN runtime (libcudnn.so.9, etc.),
    # which lives in the installed `nvidia-*-cu12` wheels, NOT on the default
    # loader path. `preload_dlls()` (onnxruntime >= 1.19) loads them from those
    # wheels — without it CUDAExecutionProvider silently fails to initialize and
    # the backbone falls back to CPU. Validated: this is what flips the RTX 4070
    # backbone run from CPU → CUDA. No-op on CPU-only onnxruntime / GPU off.
    if use_gpu != "off" and hasattr(ort, "preload_dlls"):
        try:
            ort.preload_dlls()
        except Exception as e:  # noqa: BLE001
            log.warning(
                "onnxruntime.preload_dlls() failed; GPU EP may fall back to CPU: %s", e
            )

    providers = build_providers(use_gpu)
    log.info("ONNX backbone EPs (in order): %s", providers)
    backbone_sess = ort.InferenceSession(str(backbone_path), providers=providers or None)

    # Per-run GPU honesty: onnxruntime SILENTLY drops a requested EP that can't
    # initialize (wrong CUDA/cuDNN version, missing libs) and falls back to the
    # next in the chain — printing only a C++-side stderr warning we don't
    # capture. The old code never called get_providers() after construction, so
    # a session that requested CUDA but got CPU looked identical to a real GPU
    # run: analyze that should take minutes silently took hours on CPU. Compare
    # the ACTIVE provider to what we asked for and record an honest flag so the
    # analyzer's "N GPU workers" line and the report reflect what really ran.
    gpu_active = False
    if use_gpu != "off" and providers:
        active = backbone_sess.get_providers()
        requested_gpu_ep = providers[0] if providers[0] != "CPUExecutionProvider" else None
        active_first = active[0] if active else None
        if requested_gpu_ep is not None and active_first == requested_gpu_ep:
            gpu_active = True
        elif requested_gpu_ep is not None:
            log.warning(
                "ONNX backbone requested %s but is running on %s — GPU "
                "acceleration is NOT active (likely a CUDA/cuDNN version "
                "mismatch or missing libs). Analysis will run on CPU.",
                requested_gpu_ep, active_first or "CPU",
            )

    # Pure-NumPy mel frontend (essentia-free). Explicit arg wins; otherwise the
    # VIBECHEK_NUMPY_FRONTEND env var opts in. Default OFF — essentia's
    # TensorflowInputMusiCNN stays the frontend until the full gold-corpus parity
    # gate is run; this flag exists so the validated native path can be exercised
    # (and promoted to an AnalysisConfig field) without yet flipping the default.
    if numpy_frontend is None:
        import os  # noqa: PLC0415

        numpy_frontend = os.environ.get("VIBECHEK_NUMPY_FRONTEND", "").strip().lower() not in (
            "", "0", "false", "no", "off",
        )
    if numpy_frontend:
        log.info("ONNX backbone using the pure-NumPy mel frontend (essentia-free)")

    loaded: dict[str, Any] = {}
    # Record whether GPU acceleration actually took (see the get_providers()
    # check above) so an in-process consumer / the analyze summary can report the
    # truth instead of assuming the requested EP won.
    loaded["_gpu_active"] = gpu_active
    effnet = _OnnxEffnet(backbone_sess, numpy_frontend=numpy_frontend)
    loaded["effnet"] = effnet

    # Heads. genre_discogs400 is loaded as an OPTIONAL fallback head; the
    # primary "genre" callable reuses the backbone's own 400-class output.
    genre_fallback: _OnnxHead | None = None
    for file_stem, key in _HEAD_SPECS:
        onnx_path = model_dir / f"{file_stem}.onnx"
        metadata_path = model_dir / f"{file_stem}.json"
        classes = _read_classes(metadata_path)

        if file_stem == "genre_discogs400":
            # Genre comes free from the backbone, but load the converted head if
            # present so we have a fallback AND so a missing genre head is not a
            # hard error (the backbone covers it). Classes still come from JSON.
            if onnx_path.exists():
                genre_fallback = _OnnxHead(_load_head_session(onnx_path))
            if classes:
                loaded["genre_classes"] = classes
            continue

        # All other heads are required for the onnx engine.
        loaded[key] = _OnnxHead(_load_head_session(onnx_path))
        if classes:
            loaded[f"{key}_classes"] = classes

    loaded["genre"] = _OnnxBackboneGenre(effnet, genre_fallback)
    return loaded
