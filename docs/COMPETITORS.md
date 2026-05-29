# Competitor claims & sources

The README comparison table and the "Why Vibechek?" section make specific claims about
other DJ-library tools. This doc tracks each claim and where to verify it, so the
marketing stays honest and easy to audit before a launch.

> ⚠️ **Pricing and features change.** Treat every figure below as *as-of the README's
> writing* and re-check the official source before any public launch post. Fill in the
> **Last verified** column when you check. Don't cite a price you haven't re-confirmed.

## Sources (official pages)

| Tool | Official site | Pricing/plans page |
|---|---|---|
| Mixed In Key | https://mixedinkey.com/ | https://mixedinkey.com/ (one-time purchase) |
| Lexicon DJ | https://www.lexicondj.com/ | https://www.lexicondj.com/pricing |
| Rekordbox | https://rekordbox.com/ | https://rekordbox.com/en/plan/ |
| beaTunes | https://www.beatunes.com/ | https://www.beatunes.com/en/store.html |
| Tunebat | https://tunebat.com/ | https://tunebat.com/ |

## Claims to verify

| # | Claim in README | Tool | Source to check | Last verified |
|---|---|---|---|---|
| 1 | "$58 for key and energy", two attributes only | Mixed In Key | MIK store page | _TODO_ |
| 2 | Deepest library manager; best features behind a $10-20/mo subscription; pushes a cloud/online account | Lexicon DJ | Lexicon pricing page | _TODO_ |
| 3 | Duplicate detection is filename-based | Lexicon DJ | Lexicon features/docs | _TODO_ |
| 4 | Has had "MOOD: HIGH/MID/LOW" for years; still won't auto-detect genre | Rekordbox | Rekordbox feature docs + their forum's top request | _TODO_ |
| 5 | "Free" only within Pioneer's account ecosystem; $0-30/mo plan range | Rekordbox | Rekordbox plans page | _TODO_ |
| 6 | ~$35; loudness (not energy); filename-based dedup | beaTunes | beaTunes store + feature list | _TODO_ |
| 7 | Freemium; requires upload; reports "Happiness" | Tunebat | Tunebat site / analyzer | _TODO_ |

## Capability matrix (mirror of the README table)

These are the rows asserted in the README comparison table. Re-confirm each ✅/— against
the source above before launch.

| Capability | Vibechek | Mixed In Key | Lexicon | Rekordbox | beaTunes | Tunebat |
|---|:--:|:--:|:--:|:--:|:--:|:--:|
| ML genre + subgenre (Discogs-400) | ✅ | — | — | — | — | — |
| Timeslot tag | ✅ | — | — | — | — | — |
| Energy 0-5 + mood | ✅ | Energy only | — | HIGH/MID/LOW | Loudness | "Happiness" |
| Acoustic (Chromaprint) dedup | ✅ | — | filename only | — | filename | — |
| Bulk auto-organize into folders | ✅ | — | partial | — | — | — |
| Full tag backup/restore (binary frames) | ✅ | — | $/mo tier | $/mo tier | — | — |
| Preserves Rekordbox GEOB/PRIV | ✅ | n/a | sync only | native | unknown | — |
| Works offline, no account | ✅ | ✅ | account req. | account req. | ✅ | upload req. |
| Open source | AGPL-3.0 | — | — | — | — | — |
| GPU acceleration | ✅ | — | — | — | — | — |
| Price | $0 | $58 once | $10-20/mo | $0-30/mo | ~$35 | freemium |

## Tone guidance (for the Reddit launch)

Per [REDDIT_LAUNCH.md](REDDIT_LAUNCH.md): don't trash competitors. Mixed In Key is
genuinely great at key detection — it's *narrow*, not bad. The honest framing is "this
covers the parts those tools don't," not "those tools suck." If a claim here can't be
re-verified, soften or drop it rather than overstate.
