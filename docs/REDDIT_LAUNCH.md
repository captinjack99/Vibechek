# Reddit launch plan

Drafts and subreddit list for the v0.3.0 launch. The post is written first-person, no AI giveaways (no em-dashes, no "this isn't just X, it's Y", no "delve", no "in today's fast-paced world"). Tone is a DJ talking to other DJs.

Adjust details (track count, years, story specifics) to match what you actually want to share. The arc is what matters: frustration with paid tools, hacky python scripts, friends asking for them, "fine, I'll make this real."

---

## Main post draft (DJ-focused subs)

**Title options:**

1. `I got tired of Mixed In Key, Lexicon, and Rekordbox not doing what I needed, so I built my own library tool and open-sourced it`
2. `Built an open-source library tool because I was tired of paying $20/mo for things that should be free`
3. `12,000 tracks, no patience left for Rekordbox: I built and open-sourced what I wish existed`

**Body:**

```
Hey all. Long-time DJ, not a long-time poster. Putting this out there in case anyone else has had the same headaches I have.

I've been buying and DJing electronic music for years and my library is somewhere around 12k tracks. About a year ago I sat down to actually clean it up and I hit every wall you'd expect.

Mixed In Key wants $58 for key and energy. Fine, it's good at what it does, but that's two attributes. I needed to know mood, vocal type, whether a track builds or fades, what slot in a set it belongs in. None of that.

Rekordbox has had a "MOOD: HIGH / MID / LOW" tag for years and still won't auto-detect genre, which is the request that's been at the top of their own forum for ages. So you end up with 12k tracks where half of them have the wrong genre tag and a quarter have no tag at all.

Lexicon DJ is actually really nice. But the version with the features I wanted was a monthly subscription, plus it pushes you into their cloud account stuff, and the duplicate detection is filename-based, so the same song I owned three times in different formats just sat there.

So I did the dumb thing and started writing Python scripts. Used Essentia (the open-source library out of UPF Barcelona, same one a lot of research papers use) plus the Discogs-EffNet model, which knows about 400 electronic subgenres. Wrote a Chromaprint-based dedupe because I wanted to catch re-encodings, not just byte-identical files. Wrote an organizer because I wanted the output in actual Genre/Subgenre/ folders, not a database.

The scripts worked. My library got sorted in an afternoon. A couple friends asked if they could use them. I sent them a folder of .py files and a "good luck, install Python first" message and felt like an idiot.

That was the kick. Took the scripts, turned them into a real app with a real GUI (Tauri shell, Python sidecar), packaged it for Windows / macOS / Linux. Spent way too long making the Windows install actually work without asking the user to touch a terminal (Essentia has no Windows wheel, so the app auto-installs WSL Ubuntu in the background and runs analysis inside it, translates paths transparently, you never see it).

It's called Vibechek. It's AGPL. There is no account, no telemetry, no upload, no anything. Your library stays on your machine.

What it does:
- ML-tags genre, subgenre, BPM, key, energy (0-5), mood, timeslot, direction, vocal type
- Finds true duplicates by audio fingerprint, not just filenames
- Organizes into Genre/Subgenre/ folders with rules you control
- Writes tags back to your files WITHOUT touching Rekordbox cue points, beat grids, or memory cues (this is the thing I was most afraid of breaking and spent the most time getting right)
- One-click backup and restore of every tag on every file, in case you do something dumb
- Runs on CPU just fine, uses your NVIDIA GPU if you have one and want it to

It's a public beta right now (v0.3.0-beta.3), so there are rough edges. I'd rather ship and get hit with real feedback than polish for six more months. If you try it and something doesn't work, please tell me. Bug reports are gold.

Repo: https://github.com/papapew/Vibechek
Releases (Win/Mac/Linux installers): https://github.com/papapew/Vibechek/releases

Happy to answer questions about how it works, why I picked AGPL, what's on the roadmap, whatever.
```

**Adjustments per subreddit:** see the table below. Some subs want a softer pitch (r/Music), some want technical (r/Python), some want the war story (r/DJs).

---

## Subreddit-by-subreddit plan

Cross-post once, wait at least 6 hours between posts so it doesn't look spammy. Read each sub's rules first. Some require a "release" or "showcase" flair.

| Subreddit | Size | Why post here | Adjust the angle |
|---|---|---|---|
| **r/DJs** | ~500k | The biggest one. Lean DJ war story. | Use the main draft above mostly as-is. |
| **r/Beatmatch** | ~100k | Beginners + intermediates who need cue/key tools. Friendly. | Add a line about "if you're new to DJing and overwhelmed by tag chaos, this is what I wish I'd had." |
| **r/Rekordbox** | ~25k | Pioneer users specifically. They KNOW the genre-detection gap. | Lead harder with "Rekordbox still won't auto-detect genre" and the cue-preservation guarantee. |
| **r/Serato** | ~30k | Same idea as Rekordbox sub but for Serato users. | Mention that Vibechek doesn't touch crates — it just rewrites file tags Serato re-reads. |
| **r/Traktor** | ~15k | Smaller, but Traktor users are devout about library management. | Mention the open-source angle harder; this community over-indexes on it. |
| **r/edmproduction** | ~600k | Producers, not just DJs. Many of them have unwieldy sample/track libraries. | Frame as "useful even if you only listen, not just DJ — tag your downloads." |
| **r/electronicmusic** | ~1m | Broad audience. Less DJ-tool-specific. | Soften the DJ-tool framing; lead with "open-source ML music classifier" angle. |
| **r/SideProject** | ~200k | Builders sharing work. Loves the personal story angle. | Use the "frustration → scripts → app" arc as the whole post. Skip the feature list, link to repo. |
| **r/opensource** | ~250k | Open-source enthusiasts. | Lead with AGPL, local-first, no telemetry. They eat that up. |
| **r/Python** | ~1m | Tough crowd. Don't lead with marketing — lead with the architecture. | Reframe as "I built a JSON-RPC sidecar pattern between a Tauri Rust shell and a Python ML pipeline, here's how" with the DJ use case as the example. |
| **r/learnpython** | ~900k | Don't pitch here. Maybe one mention if a question comes up about audio analysis. | Skip unless someone asks. |
| **r/wearethemusicmakers** | ~2m | Mostly producers; many also have library chaos. | Same as edmproduction, soften the DJ-only framing. |

**Don't post to** r/Music (rules forbid self-promo), r/coding (too broad and noisy), r/programming (will get downvoted for not being deep CS).

---

## A few things to remember when posting

- **Answer every comment.** Even the salty ones. Especially the salty ones. People are more impressed by "here's why I disagree" than by silence.
- **Don't argue Mixed In Key sucks.** It doesn't. It's narrow. There's a difference. If someone defends MIK, the right answer is "agreed, MIK is great at key detection — this is for the parts MIK doesn't cover."
- **The beta status is the credibility move.** Don't oversell. Say "v0.3.0-beta.3, expect some rough edges, please report bugs."
- **Screenshots help massively.** Take five clean screenshots (Library view full of analyzed tracks, the Duplicates view showing a chromaprint catch, the Settings panel with the GPU row green, the PreflightDialog mid-install, the OrganizeView dry-run plan) and attach them.
- **Have a demo video ready.** A 90-second screen recording of "open folder → analyze 200 tracks → see results → apply tags → done" is worth more than the whole post.
- **Don't post all twelve subs in one day.** Spread it over a week. Adjust based on what lands.

---

## Anti-AI-tell checklist (proof you wrote it)

- No em-dashes anywhere (use commas, parens, periods, or "and" / "or" / "but").
- No "in today's world" / "in the modern landscape" / "in an era where".
- No "isn't just X, it's Y" construction.
- No "delve", "navigate", "leverage", "tapestry", "intricate", "robust".
- No bullet lists in the body of an emotional paragraph.
- Use contractions ("I've", "it's", "don't").
- One typo or one awkward phrase is fine and reads more human than perfection.
- First-person, conversational, occasional sentence fragments.
- Specific numbers ("12k tracks", "$58") not vague claims ("lots of tracks", "expensive").
