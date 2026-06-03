# Bundled ONNX classification heads

These are the converted ONNX classification heads for the ONNX inference engine
(`AnalysisConfig.inference_engine = "onnx"`), shipped inside the app so the
engine works without downloading anything that isn't already hosted.

- `genre_discogs400`, `danceability`, `voice_instrumental`,
  `mood_aggressive`, `mood_happy`, `mood_relaxed`, `mood_sad` — each a small
  dense classifier head (`.onnx`) plus its class labels (`.json`).
- The 18 MB EffNet **backbone** (`discogs-effnet-bsdynamic-1.onnx`) is **not**
  bundled — it is the official MTG export and is fetched from essentia.upf.edu
  during ONNX setup (with progress).

## Provenance & licence

These heads are produced by `scripts/convert_heads_to_onnx.py` from the
**MTG / Essentia** TensorFlow models (the Discogs-EffNet head set). Those models
are published by the Music Technology Group (UPF) under
**CC BY-NC-SA 4.0** — see https://essentia.upf.edu/models.html. They are
redistributed here under the same terms (attribution + non-commercial +
share-alike); Vibechek itself is free/open-source (AGPL-3.0). `SHA256SUMS.txt`
pins each file's hash (also pinned in `analyzer.MODEL_SHA256_ONNX`).

Re-generate with `scripts/build_onnx_model_bundle.py` and re-copy here when the
heads are re-converted for a new model release.
