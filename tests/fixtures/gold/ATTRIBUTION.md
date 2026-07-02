# Gold-corpus fixture attribution

These are 45-second excerpts of openly-licensed tracks, committed as CI
fixtures for the gold-corpus accuracy gate (see `manifest.json` for the
asserted values). Each file's ID3 comment carries the same license pointer.
The excerpts were stream-copied (no re-encode) from the middle of each track;
all other metadata was stripped so the gate measures the audio pipeline, not
tag passthrough.

| File | Track | Artist | License | Source |
|---|---|---|---|---|
| `gold_melodic_ede_say_you_will.mp3` | Say You Will | ED.E | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) | <https://archive.org/details/say-you-will> |
| `gold_trance_moonwalk_lights_out.mp3` | Lights Out (EDM Trance Dance) | Moonwalk | [CC BY 3.0](https://creativecommons.org/licenses/by/3.0/) | <https://archive.org/details/jamendo-525562> (mirror of Jamendo album 525562) |
| `gold_house_ninjeh_stay_tuned.mp3` | Stay Tuned | Ninjeh | [CC0 1.0](https://creativecommons.org/publicdomain/zero/1.0/) | <https://archive.org/details/ninjeh-stay-tuned> |

The CC BY tracks are used under their licenses with attribution to the artists
above; no endorsement is implied. The excerpts are test data only and are not
part of the Vibechek application or its installers.
