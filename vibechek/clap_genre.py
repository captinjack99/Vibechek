"""Pure-audio genre classifier: CLAP embedding → kNN over a bundled reference.

This is the offline "audio student" from the genre-accuracy R&D (see
docs / memory): a CLAP (HTSAT-base) audio embedding is matched by cosine kNN
against a small curated reference library of web-labeled embeddings shipped in
`vibechek/clap_assets/genre_reference.npz`. It roughly doubles the pure-audio
genre accuracy of the Discogs-EffNet head (~28% → ~54%) and, unlike the file
tag, works on untagged / white-label tracks.

Split of concerns so the package stays import-clean on `[dev]` (numpy only):
  * `load_reference()` + `knn_predict()` are PURE NUMPY → fully unit-testable.
  * `load_clap_model()` + `embed_audio()` LAZILY import `laion_clap` + `torch`
    (the heavy, opt-in deps installed by the CLAP setup flow), with a clear
    error if absent.

Nothing here imports laion_clap/torch at module load.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# CLAP geometry — must match how the bundled reference was embedded
# (internal/bughunt/build_reference.py / clap_embed_cache.py).
_SR = 48000
_SEG_SECONDS = 20
_SEG_FRACS = (0.25, 0.5, 0.75)
_AMODEL = "HTSAT-base"
_CHECKPOINT_NAME = "music_clap.pt"
_CHECKPOINT_URL = (
    "https://huggingface.co/lukewys/laion_clap/resolve/main/"
    "music_audioset_epoch_15_esc_90.14.pt"
)
_DEFAULT_K = 15


@dataclass
class ClapReference:
    """The bundled kNN reference library (normalized embeddings + labels)."""

    emb: Any            # np.ndarray (n, dim) float32, L2-normalized
    labels: Any         # np.ndarray (n,) object/str
    meta: dict


def bundled_clap_assets_dir() -> Path | None:
    """Directory holding the bundled kNN reference, or None if absent.

    Mirrors `onnx_backend.bundled_onnx_assets_dir`: PyInstaller bundles
    `vibechek/clap_assets/` into the frozen sidecar (-> `<_MEIPASS>/clap_assets`);
    a source/pip install reads it from `vibechek/clap_assets/`. Returns the first
    dir that exists and contains the reference npz.
    """
    import sys  # noqa: PLC0415

    candidates: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / "clap_assets")
    candidates.append(Path(__file__).resolve().parent / "clap_assets")
    for c in candidates:
        try:
            if c.is_dir() and (c / "genre_reference.npz").is_file():
                return c
        except OSError:
            continue
    return None


def load_reference(assets_dir: Path | None = None) -> ClapReference:
    """Load + L2-normalize the bundled kNN reference library (pure numpy)."""
    import numpy as np  # noqa: PLC0415

    if assets_dir is None:
        assets_dir = bundled_clap_assets_dir()
    if assets_dir is None:
        raise RuntimeError(
            "No bundled CLAP genre reference found (vibechek/clap_assets/"
            "genre_reference.npz missing — lean/dev build?)."
        )
    z = np.load(Path(assets_dir) / "genre_reference.npz", allow_pickle=True)
    emb = z["emb"].astype(np.float32)
    norms = np.linalg.norm(emb, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    emb = emb / norms
    labels = z["labels"]
    meta_path = Path(assets_dir) / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.is_file() else {}
    return ClapReference(emb=emb, labels=labels, meta=meta)


def knn_vote_shares(
    query_emb: Any, ref: ClapReference, k: int = _DEFAULT_K,
) -> dict[str, float]:
    """Normalized, distance-weighted vote share per label among the top-k
    cosine neighbours (pure numpy). Empty dict on a degenerate query/reference.

    Distance weighting = 1 / cosine-distance (sklearn's weights="distance"):
    it sharply favours the closest neighbours and is worth ~+15 pts exact over
    flat/cosine weighting on the genre corpus — a near-duplicate at cosine 0.97
    should dominate a loose 0.5 neighbour.
    """
    import numpy as np  # noqa: PLC0415

    q = np.asarray(query_emb, dtype=np.float32).ravel()
    nq = np.linalg.norm(q)
    if nq == 0 or ref.emb.shape[0] == 0:
        return {}
    q = q / nq
    sims = ref.emb @ q                      # cosine (ref is normalized)
    k = int(min(max(1, k), sims.shape[0]))
    top = np.argpartition(-sims, k - 1)[:k]
    top = top[np.argsort(-sims[top])]
    votes: dict[str, float] = {}
    total = 0.0
    for idx in top:
        w = 1.0 / max(1.0 - float(sims[idx]), 1e-6)
        lab = str(ref.labels[idx])
        votes[lab] = votes.get(lab, 0.0) + w
        total += w
    if total <= 0:
        return {}
    return {lab: w / total for lab, w in votes.items()}


def knn_predict(
    query_emb: Any, ref: ClapReference, k: int = _DEFAULT_K,
) -> tuple[str, float]:
    """Cosine kNN, distance-weighted vote → (genre, confidence). Pure numpy.

    `query_emb` is a (dim,) vector (need not be normalized). Confidence is the
    winning label's share of the weighted vote (0..1); use `knn_vote_shares`
    when you need the full distribution (e.g. family-sum confidence).
    """
    shares = knn_vote_shares(query_emb, ref, k)
    if not shares:
        return "Unknown", 0.0
    winner = max(shares, key=shares.__getitem__)
    return winner, round(shares[winner], 3)


# ---------------------------------------------------------------------------
# Heavy (opt-in) half: CLAP model load + audio embedding. Lazy imports.
# ---------------------------------------------------------------------------


def clap_checkpoint_path() -> Path:
    """Where the 2.2 GB CLAP checkpoint lives in the analysis environment.

    Kept in the local home (`~/.vibechek/clap/`), NOT the models dir — on Windows
    the models dir is read over the slow WSL↔Windows mount, and it may be on a
    synced drive. The setup flow downloads it here; `load_clap_model` reads it.
    """
    return Path.home() / ".vibechek" / "clap" / _CHECKPOINT_NAME


def load_clap_model(checkpoint: Path | None = None, use_gpu: str = "auto") -> Any:
    """Load the CLAP model (LAZY laion_clap + torch). Opt-in heavy dependency.

    Pinned to CPU regardless of `use_gpu` (param kept for a future opt-in):
    CLAP_Module otherwise self-selects CUDA when torch sees a GPU, and a
    ~2 GB fp32 model landing on the card OUTSIDE the analyzer's VRAM worker
    budget (sized for the EffNet engine only) OOMs consumer GPUs in hybrid
    mode. Embedding 3×20 s segments on CPU costs a few seconds per track —
    small next to the rest of the analysis.
    """
    del use_gpu
    ckpt = Path(checkpoint) if checkpoint else clap_checkpoint_path()
    if not ckpt.is_file():
        raise RuntimeError(
            f"CLAP checkpoint not found at {ckpt}. Run the CLAP genre setup first."
        )
    try:
        import laion_clap  # noqa: PLC0415
    except ImportError as e:
        raise RuntimeError(
            "CLAP genre classifier needs laion-clap + torch. Install with: "
            "pip install 'vibechek[clap]' (or use the in-app CLAP setup)."
        ) from e
    model = laion_clap.CLAP_Module(enable_fusion=False, amodel=_AMODEL, device="cpu")
    model.load_ckpt(str(ckpt))
    return model


def _segments(audio: Any) -> Any:
    """Extract the 3 fixed segments (25/50/75%, 20 s) used to build the reference."""
    import numpy as np  # noqa: PLC0415

    n = len(audio)
    seg = int(_SEG_SECONDS * _SR)
    out = []
    for fr in _SEG_FRACS:
        start = max(0, int(n * fr) - seg // 2)
        if start >= n:
            continue
        out.append(audio[start:start + seg])
    if not out:
        out = [audio]
    maxlen = max(len(s) for s in out)
    arr = np.zeros((len(out), maxlen), dtype=np.float32)
    for i, s in enumerate(out):
        arr[i, : len(s)] = s
    return arr


def embed_audio(model: Any, audio: Any) -> Any:
    """Embed mono 48 kHz audio → a (dim,) CLAP embedding (3-segment mean)."""
    import numpy as np  # noqa: PLC0415

    arr = _segments(np.asarray(audio, dtype=np.float32))
    emb = model.get_audio_embedding_from_data(x=arr, use_tensor=False)
    emb = np.asarray(emb, dtype=np.float32)
    norms = np.linalg.norm(emb, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    emb = emb / norms
    return emb.mean(0)


__all__ = [
    "ClapReference",
    "bundled_clap_assets_dir",
    "load_reference",
    "knn_predict",
    "knn_vote_shares",
    "clap_checkpoint_path",
    "load_clap_model",
    "embed_audio",
    "_CHECKPOINT_URL",
    "_CHECKPOINT_NAME",
]
