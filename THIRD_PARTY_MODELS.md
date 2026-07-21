# Third-party models and tools bundled with Vibechek

Vibechek's application code is AGPL-3.0-or-later (see [LICENSE](LICENSE)), but the
ML model weights it downloads and redistributes are third-party works under their
own licenses. This file is the license notice CC BY-NC-SA 4.0 requires us to ship
alongside those weights — and an honest inventory of everything else the app
fetches on your behalf.

## Bundled / auto-downloaded by the default analysis engines

All of the following were created by the **Music Technology Group, Universitat
Pompeu Fabra (MTG-UPF)** and are licensed **[CC BY-NC-SA 4.0](https://creativecommons.org/licenses/by-nc-sa/4.0/)**
("also available under proprietary license upon request" — see
[essentia.upf.edu/models.html](https://essentia.upf.edu/models.html)):

| Model | Files | Changes made |
|---|---|---|
| Discogs-EffNet backbone | `discogs-effnet-bs64-1.pb` / `discogs-effnet-bsdynamic-1.onnx` | none (verbatim redistribution) |
| Genre head (Discogs-400) | `genre_discogs400-discogs-effnet-1.pb` + `.json` | ONNX conversion of the `.pb` head (numerically validated against the original); the converted file is likewise CC BY-NC-SA 4.0 |
| Danceability head | `danceability-discogs-effnet-1.pb` + `.json` | same ONNX conversion note |
| Voice/instrumental head | `voice_instrumental-discogs-effnet-1.pb` + `.json` | same ONNX conversion note |
| Mood heads (aggressive, happy, relaxed, sad) | `mood_*-discogs-effnet-1.pb` + `.json` | same ONNX conversion note |

Attribution: Essentia and the Essentia models are developed by MTG-UPF
(Bogdanov et al.); see [essentia.upf.edu](https://essentia.upf.edu/) and the
per-model pages for the papers behind each model.

**Non-commercial note:** Vibechek is a free, no-account, donation-supported
application; it bundles these weights under the NC terms above. If you fork
Vibechek into anything commercial, the MTG weights are the first thing you must
replace or re-license (MTG offers proprietary licensing on request).

## Opt-in downloads (fetched only when you enable the feature)

| Component | License | Source |
|---|---|---|
| LAION-CLAP checkpoint `music_audioset_epoch_15_esc_90.14.pt` (CLAP genre classifier) | **CC0-1.0** | [lukewys/laion_clap](https://huggingface.co/lukewys/laion_clap) (LAION-AI/CLAP) |
| Qwen2.5 7B (online genre lookup, via Ollama) | **Apache-2.0** | pulled by Ollama at setup; not redistributed by Vibechek |
| Chromaprint `fpcalc` (audio fingerprinting for dedupe) | **LGPL-2.1+** | official [AcoustID Chromaprint release](https://acoustid.org/chromaprint), downloaded verbatim, checksum-pinned |

## The essentia library itself

The essentia audio-analysis library (and the essentia-tensorflow build) is
**AGPL-3.0** — license-aligned with Vibechek's own code.
