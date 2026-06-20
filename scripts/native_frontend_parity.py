#!/usr/bin/env python3
"""Validate the pure-NumPy MusiCNN mel frontend against essentia, end-to-end.

Companion to `scripts/onnx_parity.py`. Where that proved the ONNX *backbone*
matches TF given essentia's mel, THIS proves the NumPy *frontend* matches
essentia's `TensorflowInputMusiCNN` — closely enough that, through the same
backbone, embeddings + genre don't drift. It's the validation gate for retiring
the last essentia dependency on the ONNX path (see docs/ONNX_MIGRATION.md and
`vibechek/numpy_frontend.py`).

Two checks per track:
  * Stage A (frontend isolation): essentia mel vs numpy mel on the SAME essentia
    16 kHz decode -> backbone -> cosine + genre top-1.
  * Stage B (fully native): soundfile+soxr decode -> numpy mel -> backbone, vs the
    essentia ground truth (adds decode/resampler drift).

DEV/CI-on-demand: needs essentia, onnxruntime, soundfile, soxr, and the backbone
present. On Windows, run it inside WSL (where essentia lives).

    python scripts/native_frontend_parity.py <audio>... [--models DIR]
    python scripts/native_frontend_parity.py --make-fixture tests/data/musicnn_mel_fixture.npz
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from vibechek.numpy_frontend import musicnn_mel  # noqa: E402
from vibechek.onnx_backend import (  # noqa: E402
    BACKBONE_ONNX_FILENAME,
    MEL_FRAME_SIZE,
    MEL_HOP_SIZE,
    _make_patches,
)

COS_MIN = 0.995


def _essentia_mel(audio: np.ndarray) -> np.ndarray:
    import essentia.standard as es

    return np.array(
        [es.TensorflowInputMusiCNN()(fr)
         for fr in es.FrameGenerator(audio, frameSize=MEL_FRAME_SIZE, hopSize=MEL_HOP_SIZE, startFromZero=True)],
        dtype=np.float32,
    )


def _backbone(sess, iname, mel):
    activations, embeddings = sess.run(None, {iname: _make_patches(mel.astype(np.float32))})
    return activations.mean(0), embeddings.mean(0)


def _cos(a, b):
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-9))


def make_fixture(out: Path) -> int:
    """Regenerate the committed parity fixture (deterministic synth + essentia mel)."""
    import essentia

    essentia.log.infoActive = essentia.log.warningActive = False
    sr = 16000
    rng = np.random.default_rng(0)
    t = np.arange(sr * 2) / sr
    x = sum(np.sin(2 * np.pi * f * t) / 6.0 for f in (110, 220, 440, 880, 1760, 3520))
    x = x + 0.5 * np.sin(2 * np.pi * (50 * t + 0.5 * (7000 - 50) / t[-1] * t**2))
    x = (x + 0.02 * rng.standard_normal(len(t))).astype(np.float32)
    x /= np.max(np.abs(x)) / 0.9
    mel = _essentia_mel(x)
    out.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(out, audio=x, mel=mel)
    print(f"wrote {out} audio={x.shape} mel={mel.shape} ({out.stat().st_size/1024:.0f} KB)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("audio", nargs="*")
    ap.add_argument("--models", default=str(Path.home() / "onnx_val" / "models"))
    ap.add_argument("--make-fixture", metavar="PATH", default=None)
    args = ap.parse_args()

    if args.make_fixture:
        return make_fixture(Path(args.make_fixture))
    if not args.audio:
        ap.error("provide audio file(s) or --make-fixture PATH")

    import essentia
    import essentia.standard as es
    import onnxruntime as ort
    import soundfile as sf
    import soxr

    essentia.log.infoActive = essentia.log.warningActive = False
    backbone = Path(args.models) / BACKBONE_ONNX_FILENAME
    sess = ort.InferenceSession(str(backbone), providers=["CPUExecutionProvider"])
    iname = sess.get_inputs()[0].name

    a_cos, a_gen, b_cos, b_gen = [], [], [], []
    for path in args.audio:
        audio = es.MonoLoader(filename=path, sampleRate=16000, resampleQuality=4)()
        mel_es, mel_np = _essentia_mel(audio), musicnn_mel(audio)
        ge, ee = _backbone(sess, iname, mel_es)
        gn, en = _backbone(sess, iname, mel_np)
        ca = _cos(ee, en); ga = int(ge.argmax()) == int(gn.argmax())
        a_cos.append(ca); a_gen.append(ga)

        wav, srate = sf.read(path, dtype="float64", always_2d=True)
        a16 = soxr.resample(wav.mean(1), srate, 16000, quality="VHQ").astype(np.float32)
        gnat, enat = _backbone(sess, iname, musicnn_mel(a16))
        cb = _cos(ee, enat); gb = int(ge.argmax()) == int(gnat.argmax())
        b_cos.append(cb); b_gen.append(gb)
        print(f"{Path(path).name[:40]:40s} A:cos={ca:.5f} genre={'OK' if ga else 'XX'}  "
              f"B(native):cos={cb:.5f} genre={'OK' if gb else 'XX'}")

    ok = min(a_cos) >= COS_MIN and all(a_gen) and min(b_cos) >= COS_MIN and all(b_gen)
    print(f"\nStage A frontend: cos min {min(a_cos):.5f} genre {sum(a_gen)}/{len(a_gen)}")
    print(f"Stage B native:   cos min {min(b_cos):.5f} genre {sum(b_gen)}/{len(b_gen)}")
    print("PARITY:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
