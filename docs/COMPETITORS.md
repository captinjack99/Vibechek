# Competitor claims & sources

Sourcing for the comparison claims in the README. Every row below was checked against
the tools' official pages (plus reputable secondary sources where the official page was
unclear).

**Last verified: 2026-05-29.** Pricing and features change — re-check before any launch
post. Where the research contradicted an earlier README claim, the README has been
corrected and the change is noted under "Corrections applied" at the bottom.

## Capability matrix (verified)

| Capability | Vibechek | Mixed In Key | Lexicon | Rekordbox | beaTunes | Tunebat |
|---|:--:|:--:|:--:|:--:|:--:|:--:|
| ML genre + subgenre auto-detection | ✅ | — | — | — | — | — |
| Surfaces tag ↔ detection genre conflicts for review (augment, not overwrite) | ✅ | — | — | — | — | — |
| Timeslot tag (Opener/Warm-Up/Peak/Afterhours) | ✅ | — | — | — | — | — |
| Energy rating | ✅ (0-5) | ✅ (1-10) | ✅ | — | ~ (loudness) | ✅ (0-100, Pro) |
| Mood labels | ✅ | — | ~ (Spotify) | ~ (HIGH/MID/LOW)¹ | ~ (inferred) | ~ ("Happiness") |
| Acoustic (fingerprint) dedup | ✅ | — | ✅ | — (metadata only) | ✅ (+ metadata) | — |
| Bulk auto-organize into genre/subgenre folders | ✅ | — | ✅ | — | — | — |
| Full tag backup/restore (incl. binary frames) | ✅ | — | ~ (DB backup, paid) | — (library DB only) | — | — |
| Preserves Rekordbox GEOB/PRIV cues | ✅ | n/a² | sync only | native | — | n/a |
| Works offline, no account | ✅ | — (online + acct)³ | ~ (acct for paid/cloud) | — (acct to activate) | ✅ | — (web app) |
| Open source | AGPL-3.0 | — | — | — | — | — |
| GPU acceleration | ✅ | — | — | — | — | — |
| Price | **$0** | $58 once⁴ | $10-20/mo or $199-399 once | $0-36/mo | €34.95 once | freemium ($7.99/mo Pro) |

✅ = yes · ~ = partial/qualified · — = no/none

¹ Rekordbox's HIGH/MID/LOW is a track-**structure** label (drives phrase analysis), not an
energy rating or set-position tag. ² MIK generates its *own* cue points; it doesn't
preserve pre-existing Rekordbox cue/grid data. ³ Since v2.5 MIK's analysis runs on its
servers, so analyzing new tracks needs an internet connection + license activation.
⁴ MIK standard list $58 (on sale $39 as of writing); free tier analyzes up to 200 tracks.

## Per-tool detail

### Mixed In Key — `mixedinkey.com`
Proprietary desktop app focused on harmonic mixing: musical **key** (Camelot) + a patented
**Energy 1-10** rating, plus auto cue points, BPM, and ID3 cleanup. One-time purchase
(standard $58; sale $39; free tier ≤200 tracks; Pro ~$49). **Now requires internet + an
account** to analyze (server-side algorithm since v2.5). No mood labels, no genre/subgenre
auto-detection, no timeslot tagging, no duplicate detection, no folder reorganization, no
tag backup/restore, no GPU. Not open source.
Sources: <https://shop.mixedinkey.com/mixed-in-key>, <https://mixedinkey.com/learn-more/>,
<https://en.wikipedia.org/wiki/Mixed_In_Key>.

### Lexicon DJ — `lexicondj.com`
Proprietary desktop DJ library manager (Win/macOS). **Freemium**: free library conversion,
then Essential ($9.99/mo or $199 lifetime) and Ultimate ($19.99/mo or $399 lifetime) — so
**not subscription-only**, and one-time lifetime options exist. Built-in key + energy
analysis, pattern-based **auto-organization into genre/subgenre folders**, and **acoustic
fingerprint duplicate detection** (not filename-based). No ML genre/subgenre auto-detection
(it standardizes existing tags), no native set-position tagging, no GPU. Tag/database
backup exists (cloud, Ultimate) but not a documented arbitrary-binary-frame file backup.
Sources: <https://www.lexicondj.com/pricing>, <https://www.lexicondj.com/manual/find-duplicates>,
<https://www.lexicondj.com/manual/moving-and-renaming-f-iles>, <https://www.lexicondj.com/lexicon-vs-djoid>.

### Rekordbox — `rekordbox.com` (Pioneer DJ / AlphaTheta)
Proprietary, closed-source. **Freemium subscription**: Free $0; Core $12/mo ($120/yr);
Creative $18/mo ($180/yr); Professional $36/mo ($360/yr). No perpetual license. Requires a
Pioneer DJ account to activate (runs offline afterward). Strong native key detection and a
long-standing **HIGH/MID/LOW "Mood"** (a structure-type label, not energy or set-position).
**No native genre auto-detection** (top, long-unfulfilled forum request), no acoustic dedup
(only Title+Artist metadata merge), no on-disk genre-folder reorganization, no documented
full tag backup/restore, no GPU-accelerated analysis.
Sources: <https://rekordbox.com/en/plan/>, <https://rekordbox.com/en/support/faq/rekordbox7/>,
<https://forums.pioneerdj.com/hc/en-us/community/posts/900002146446-Genre-Detection-In-Rekordbox>.

### beaTunes — `beatunes.com` (tagtraum industries)
Proprietary Win/macOS analyzer. **One-time perpetual license, €34.95 per platform** (note:
EUR, ~$38 — not USD). Audio key (musical + Open Key Notation), BPM, loudness/ReplayGain +
a "color" similarity measure, valence/arousal **mood** labels (inferred from Last.fm /
AcousticBrainz, not pure audio), and **duplicate detection that is metadata-based by default
and acoustic-fingerprint-capable** (not filename-based). No ML genre auto-classification, no
timeslot tagging, no genre-folder reorganization, no tag backup/restore (explicitly "no
undo"), no DJ cue/grid preservation feature, no GPU. Not open source.
Sources: <https://www.beatunes.com/en/beatunes-buy.html>, <https://www.beatunes.com/en/beatunes-faq.html>,
<https://help.beatunes.com/discussions/problems/45455-detecting-duplicates>.

### Tunebat — `tunebat.com`
Proprietary **web-based** key/BPM + sentiment analyzer over a 70M+ track database.
**Freemium**: free tier = key + Camelot + BPM; Pro ($7.99/mo or $35.88/yr) unlocks sentiment
(Energy, Danceability, **"Happiness"**) for uploaded files. Analysis runs client-side in the
browser (no server upload), but it's a website, not an offline/desktop app. No library
management at all: no duplicate detection, no file organization, no tag writing/backup, no
Rekordbox cue/grid interaction, no genre/subgenre auto-classification, no GPU. Not open source.
Sources: <https://tunebat.com/Analyzer>, <https://producerhive.com/buyer-guides/dj-gear/tunebat-review/>.

## Corrections applied to the README (2026-05-29 verification)

The research disproved several earlier comparison claims; the README table + prose were
updated to match reality:

- **Lexicon dedup is acoustic, not filename-based** — Lexicon now shows ✅ for acoustic dedup.
- **beaTunes dedup is metadata + acoustic, not filename-based** — beaTunes now shows ✅.
- **Lexicon does auto-organize into genre/subgenre folders** — was "partial", now ✅.
- **Lexicon pricing** — was "$10-20/mo"; it's freemium with $9.99-19.99/mo *or* $199-399
  one-time lifetime.
- **Rekordbox pricing** — was "$0-30/mo"; monthly Professional is $36/mo ($30/mo only on
  annual billing). Now "$0-36/mo".
- **Mixed In Key is no longer offline/no-account** — analysis is server-side since v2.5 and
  needs activation; the "works offline, no account ✅" claim was removed.
- **beaTunes price is in EUR** — €34.95 one-time, not "~$35" USD.
- The "stacking MIK + Lexicon still doesn't do acoustic de-dup" line was removed (Lexicon
  does). Vibechek's honest, still-unique differentiators are **ML genre/subgenre
  auto-detection, timeslot tagging, local-first + open-source + $0, and GPU acceleration** —
  none of the five offers ML genre auto-detection or is open source.

## Analysis accuracy & methodology vs the field (researched 2026-06-18)

This matrix is about *capabilities*. On *accuracy*, the honest finding is that **no competitor publishes
gold-corpus accuracy numbers**, so cross-tool comparison rests on academic proxies + reputation, not
measurement. Deep-research synthesis (cited, adversarially verified):

- **Key — Vibechek's ~71% exact-Camelot is AT the state of the art for electronic music, not behind it.**
  The best published systems on GiantSteps (604 Beatport EDM excerpts, the directly comparable benchmark)
  cluster in the high-60s–mid-70s exact-correct band: supervised CNN (Korzeniowski-Widmer, 67.9% exact),
  genre-agnostic AllConv (67.9%), self-supervised S-KEY 2025 (~72% MIREX). A third-party reimplementation
  even put **Mixed In Key 8.3 at ~69% exact — beaten by a CNN in some runs, beating it in others.** Caveat:
  academic figures are MIREX-weighted/-correct, not Camelot-exact, so this is same-ballpark, not point-for-point.
  Sources: arxiv 1706.02921, 1808.05340, 2501.12907.
- **The major→parallel-minor flip is INHERENT to single-key detection, not a Vibechek bug.** It persists even
  in CNNs under train/test tonality shift, and MIREX scores it as the *worst* near-miss (0.2 credit, vs 0.5 for a
  fifth, 0.3 for relative). Confirms the standing "don't blind-correct the flip" rule. Source: MIREX 2025 Audio
  Key Detection wiki + arxiv 1706.02921.
- **Swapping essentia's pitch-class template for a CNN key model is the only credible per-track key lever — but
  the realistic gain over 71% is low single digits** (CNN beat the best EDM template by only ~4 pts; Vibechek's
  71% already exceeds it), gated by deployment cost (madmom is non-viable on the py3.13 toolchain), and a generic
  non-EDM-trained CNN *collapses* on electronic material. Not worth it without a production-path A/B on our own
  gold corpus first.
- **BPM — Vibechek's ~97% is NOT comparable to the ~74% audio-only literature figures.** Our 97% reflects
  filename reconciliation + octave-folding on an easy electronic-only corpus; pure-audio cross-genre *exact*
  tempo (Acc1) plateaus ~74% because **octave (half/double-time) errors are the dominant, persistent failure
  mode** — which our octave-folding already targets. Single-value BPM has little headroom. Sources: TISMIR
  10.5334/tismir.43, arxiv 2401.00209.
- **The one structural gap is VARIABLE beatgrids** (local tempo / drift), which Rekordbox's "dynamic" analysis
  provides and Vibechek's single static BPM lacks. A capability gap, not an accuracy gap (see ROADMAP "Variable /
  dynamic beatgrid"). NB the research could NOT directly source how Rekordbox/Serato/Traktor implement variable
  beatgrids, and confirmed **Mixed In Key does NOT do variable beatgrids** (fixed 0/30/70% chunks + ±3 BPM
  consensus → one static BPM — same class as Vibechek). Source: mixidaw.com/BPM.
- **Mixed In Key's 1-10 Energy: nothing to copy.** Its patented algorithm's technical basis is undocumented and
  community-disputed (volume-bias, narrow 5-8 effective range); it's a set-building UX feature, not a published
  method. The only realistic target is matching the UX (a monotonic, set-orderable scale) — consistent with
  Vibechek's own finding that energy/mood has no measurable accuracy lever. Source: mixedinkey.com energy guide.

**Bottom line for README/launch claims:** do NOT claim "more accurate than $TOOL" — we can't measure them, and on
the comparable academic proxy our key is at parity with the SOTA, not ahead. The honest, defensible framing stays
*capability* coverage + local-first + open-source + $0 (the existing matrix above), plus "key/BPM accuracy on par
with the best published methods for electronic music."

## Tone guidance

Be fair, not dismissive. Mixed In Key is genuinely great at key detection — it's *narrow*,
not bad. Lexicon is a strong, deep library manager. The honest framing is "Vibechek covers
the parts those tools don't (and is free + open source)," not "those tools are bad." If a
claim can't be re-verified against a primary source, soften or drop it.

A distinctive, honest angle to lead with: Vibechek **augments** a DJ's existing tags and
**surfaces conflicts for one-click review instead of silently overwriting** hand-curation —
where the other tools either don't touch genre or just impose their own values. Lead with
*trust* + capability coverage + local-first/OSS/$0, not accuracy bragging: Vibechek's
analysis numbers are **at parity with the field, not ahead** (see "Analysis accuracy &
methodology" above — key ≈71% is SOTA-tier for electronic, not a record).
