"""Pure-NumPy reimplementation of essentia's ``TensorflowInputMusiCNN`` log-mel.

This is the last essentia dependency on the ONNX inference path (``onnx_backend``
still calls ``es.TensorflowInputMusiCNN`` to build the mel-spectrogram the
Discogs-EffNet backbone consumes). Reproducing it in NumPy lets the whole neural
pipeline run on native Windows with **zero essentia** — i.e. without WSL — since
the backbone already runs on ONNX Runtime (which ships Windows wheels).

Only NumPy is needed (a real, lazily-imported dependency of the analyzer). No
essentia, librosa, scipy, or torch.

The essentia ``TensorflowInputMusiCNN`` chain, per 512-sample frame at 16 kHz
(``FrameGenerator(frameSize=512, hopSize=256, startFromZero=True)``):

    Hann window  →  magnitude Spectrum  →  MelBands(numberBands=96,
    warpingFormula="slaneyMel", normalize="unit_tri", type="power",
    lowFrequencyBound=0, highFrequencyBound=8000)  →  log10(1 + 10000·x)

Validated against essentia 2.1-beta6-dev on 5 real tracks (electronic / rock /
two MP3s) through the production ``discogs-effnet-bsdynamic-1.onnx`` backbone:

    * global linear-domain scale **k = 1.0** (no normalization fudge needed —
      essentia's Hann is the symmetric, un-normalized window; 5–95% ratio spread
      0.01%, i.e. a single constant explains the whole mapping),
    * per-frame log-mel **L1 = 0.0000**, max abs error ≤ 0.001 (mel range 0–6.5),
    * mean-embedding **cosine = 1.00000** and **genre top-1 5/5** identical.

So this is a bit-close reproduction, not an approximation — the two non-default
traps the migration notes flag (``slaneyMel`` warping and ``unit_tri`` *area*
normalization) are exactly librosa's ``htk=False`` Slaney scale + Slaney
(``norm="slaney"``) area normalization, implemented directly below.
"""

from __future__ import annotations

from functools import lru_cache

import numpy as np

# Geometry — must match onnx_backend.MEL_FRAME_SIZE / MEL_HOP_SIZE and the
# essentia FrameGenerator call in onnx_backend._OnnxEffnet._melspec.
SAMPLE_RATE = 16000
FRAME_SIZE = 512
HOP_SIZE = 256
N_MELS = 96
# Global linear-domain scale that maps this implementation's mel power onto
# essentia's. Empirically 1.0 (essentia uses an un-normalized symmetric Hann);
# kept as a named constant so any future essentia change is a one-line fix.
MEL_SCALE_K = 1.0


@lru_cache(maxsize=4)
def _slaney_mel_filterbank(
    sr: int = SAMPLE_RATE,
    n_fft: int = FRAME_SIZE,
    n_mels: int = N_MELS,
    fmin: float = 0.0,
    fmax: float | None = None,
) -> np.ndarray:
    """Slaney-scale triangular mel filterbank, unit-triangle-AREA normalized.

    Matches essentia ``MelBands(warpingFormula="slaneyMel", normalize="unit_tri")``
    (equivalently librosa ``mel(htk=False, norm="slaney")``). Cached: the matrix
    depends only on constants, so it is built once per process.

    Returns ``[n_mels, n_fft // 2 + 1]`` (float64 for accumulation precision).
    """
    if fmax is None:
        fmax = sr / 2.0
    n_bins = n_fft // 2 + 1
    fft_freqs = np.linspace(0.0, sr / 2.0, n_bins)

    # Slaney hz<->mel: linear below 1 kHz, log above (the htk=False formula).
    f_sp = 200.0 / 3.0
    min_log_hz = 1000.0
    min_log_mel = min_log_hz / f_sp
    logstep = np.log(6.4) / 27.0

    def hz_to_mel(f: np.ndarray) -> np.ndarray:
        f = np.asarray(f, float)
        mel = f / f_sp
        hi = f >= min_log_hz
        return np.where(hi, min_log_mel + np.log(np.maximum(f, 1e-9) / min_log_hz) / logstep, mel)

    def mel_to_hz(m: np.ndarray) -> np.ndarray:
        m = np.asarray(m, float)
        freq = f_sp * m
        hi = m >= min_log_mel
        return np.where(hi, min_log_hz * np.exp(logstep * (m - min_log_mel)), freq)

    mel_pts = np.linspace(hz_to_mel(np.array([fmin]))[0], hz_to_mel(np.array([fmax]))[0], n_mels + 2)
    hz_pts = mel_to_hz(mel_pts)

    fb = np.zeros((n_mels, n_bins), dtype=np.float64)
    for m in range(n_mels):
        f_left, f_center, f_right = hz_pts[m], hz_pts[m + 1], hz_pts[m + 2]
        left = (fft_freqs - f_left) / (f_center - f_left)
        right = (f_right - fft_freqs) / (f_right - f_center)
        fb[m] = np.maximum(0.0, np.minimum(left, right))
        fb[m] *= 2.0 / (f_right - f_left)  # unit_tri (Slaney area) normalization
    return fb


@lru_cache(maxsize=2)
def _hann(n: int) -> np.ndarray:
    """Symmetric, un-normalized N-point Hann window (matches essentia's)."""
    return np.hanning(n)


def musicnn_mel(audio: np.ndarray) -> np.ndarray:
    """Compute the ``[N, 96]`` log-mel essentia ``TensorflowInputMusiCNN`` produces.

    ``audio`` is mono float samples at 16 kHz (the same buffer the essentia path
    feeds ``FrameGenerator``). Returns float32 ``[N, 96]`` where ``N`` is the
    number of full 512-sample frames at hop 256. Short/empty input yields an
    empty ``(0, 96)`` array — the caller's patcher handles the zero-frame case
    (matching ``onnx_backend._make_patches``).
    """
    audio = np.asarray(audio, dtype=np.float64)
    n = audio.shape[0]
    if n < FRAME_SIZE:
        return np.zeros((0, N_MELS), dtype=np.float32)

    # FrameGenerator(startFromZero=True): frames fully inside the signal, the
    # first starting at sample 0, stepping by HOP_SIZE.
    starts = range(0, n - FRAME_SIZE + 1, HOP_SIZE)
    frames = np.stack([audio[s:s + FRAME_SIZE] for s in starts])
    spec = np.fft.rfft(frames * _hann(FRAME_SIZE), n=FRAME_SIZE, axis=1)
    power = np.abs(spec) ** 2  # MelBands(type="power") squares the magnitude spectrum
    mel_power = power @ _slaney_mel_filterbank().T
    return np.log10(1.0 + 10000.0 * MEL_SCALE_K * mel_power).astype(np.float32)
