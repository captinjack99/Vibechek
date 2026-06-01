#!/usr/bin/env python3
"""ONNX-vs-TensorFlow parity harness for the Discogs-EffNet pipeline.

This is the validation gate for the ONNX migration (see docs/ONNX_MIGRATION.md).
It proves that the official ONNX EffNet backbone, fed essentia's mel-spectrogram,
reproduces essentia-tensorflow's embedding + genre + (head) predictions closely
enough that classifications don't drift — the one thing that "can't be done blind."

It is a DEV/CI-on-demand tool, NOT part of the unit suite: it needs
essentia-tensorflow, onnxruntime, and the real models present. Run it inside the
environment that actually does analysis (on Windows that's WSL).

Validated 2026-06-01 on a real track (Adele — "Chasing Pavements"):
    hop=64  embedding cosine = 0.99942   genre top-class TF==ONNX (308)
            voice: TF [0.028, 0.972] vs ONNX [0.003, 0.997]  (both "Vocal")

Recipe the migration must implement:
  1. MonoLoader(sampleRate=16000)             — 16 kHz mono
  2. mel = TensorflowInputMusiCNN per 512/256 frame  -> [N, 96] log-mel
     (or a NumPy reimplementation validated against this output, to drop TF)
  3. patch into [k, 128, 96] windows at hop 64 (overlapping; matches essentia)
  4. backbone ONNX -> outputs[0] = 400-class genre (sigmoid), outputs[1] = 1280-d
     embedding. Genre comes free from the backbone; mood/voice/danceability run
     their (tf2onnx-converted) heads on the embedding.
  5. mean over patches — the same pooling analyzer.py already does.

Usage:
    python scripts/onnx_parity.py <audio_file> [--models DIR] [--hop 64]

Exit code 0 if every checked attribute is within tolerance, 1 otherwise.
Download the models first (backbone .pb + .onnx + head .pb/.json) from
https://essentia.upf.edu/models/ — see docs/ONNX_MIGRATION.md §4.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

# Tolerances: embedding cosine must be very high; the user-visible categorical
# outputs (genre top-class, vocal label) must MATCH. These are what guard
# against a silent re-classification of the user's whole library.
COS_MIN = 0.995
GENRE_TOPK = 1  # top-1 genre class must agree


def _quiet_tf() -> None:
    import os

    os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "3")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("audio")
    ap.add_argument("--models", default=str(Path.home() / "onnx_val" / "models"))
    ap.add_argument("--hop", type=int, default=64, help="patch hop in mel frames (essentia uses ~64)")
    args = ap.parse_args()

    _quiet_tf()
    import essentia.standard as es
    import onnxruntime as ort

    m = Path(args.models)
    backbone_pb = m / "discogs-effnet-bs64-1.pb"
    backbone_onnx = m / "discogs-effnet-bsdynamic-1.onnx"
    voice_pb = m / "voice_instrumental-discogs-effnet-1.pb"
    voice_json = m / "voice_instrumental-discogs-effnet-1.json"

    audio = es.MonoLoader(filename=args.audio, sampleRate=16000, resampleQuality=4)()

    # ---- essentia-tensorflow ground truth ----
    emb_tf = es.TensorflowPredictEffnetDiscogs(graphFilename=str(backbone_pb), output="PartitionedCall:1")(audio)
    gen_tf = es.TensorflowPredictEffnetDiscogs(graphFilename=str(backbone_pb), output="PartitionedCall:0")(audio).mean(0)
    vj = json.loads(voice_json.read_text())
    vin, vout = vj["schema"]["inputs"][0]["name"], vj["schema"]["outputs"][0]["name"]
    voice = es.TensorflowPredict2D(graphFilename=str(voice_pb), input=vin, output=vout)
    vp_tf = voice(emb_tf).mean(0)

    # ---- ONNX path: essentia melspec -> patches -> ONNX backbone ----
    mel = np.array(
        [es.TensorflowInputMusiCNN()(fr)
         for fr in es.FrameGenerator(audio, frameSize=512, hopSize=256, startFromZero=True)],
        dtype=np.float32,
    )  # [N, 96]
    P = 128
    idx = list(range(0, mel.shape[0] - P + 1, args.hop)) or [0]
    patches = np.stack([mel[i:i + P] for i in idx]).astype(np.float32)
    sess = ort.InferenceSession(str(backbone_onnx), providers=["CPUExecutionProvider"])
    iname = sess.get_inputs()[0].name
    activations, embeddings = sess.run(None, {iname: patches})
    gen_on, emb_on = activations.mean(0), embeddings.mean(0)
    vp_on = voice(np.atleast_2d(emb_on).astype(np.float32)).mean(0)

    # ---- compare ----
    me = emb_tf.mean(0)
    cos = float(np.dot(me, emb_on) / (np.linalg.norm(me) * np.linalg.norm(emb_on) + 1e-9))
    genre_match = int(gen_tf.argmax()) == int(gen_on.argmax())
    voice_match = int(vp_tf.argmax()) == int(vp_on.argmax())

    print(f"patches: tf~{emb_tf.shape[0]} onnx={len(idx)} (hop={args.hop})")
    print(f"embedding cosine = {cos:.5f}   (min {COS_MIN})")
    print(f"genre top-class  TF={int(gen_tf.argmax())} ONNX={int(gen_on.argmax())}  -> {'MATCH' if genre_match else 'MISMATCH'}")
    print(f"voice            TF={np.round(vp_tf,3)} ONNX={np.round(vp_on,3)}  -> {'MATCH' if voice_match else 'MISMATCH'}")

    ok = cos >= COS_MIN and genre_match and voice_match
    print("\nPARITY:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
