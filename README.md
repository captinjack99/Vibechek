# Vibechek

**Open-source ML for your DJ library.** Auto-tag genre, mood, energy, BPM, and key. Find duplicates the way your ears would. Organize 10,000 tracks in an afternoon. Keep every Rekordbox cue point intact.

> **Status:** `v0.8.2-beta` — public beta, feature-complete, headed for stable. Battle-tested on a real 12,000-track personal library. Cross-platform (Windows / macOS / Linux); on Windows the WSL-free native analysis engine bundled into the installer is now the default. Free forever under AGPL-3.0.

---

## Why Vibechek?

Every paid library tool makes one of three trade-offs:

- **Mixed In Key** is the gold standard for key + energy, but charges $58, only detects those two attributes, and (since v2.5) needs an internet connection + account to analyze.
- **Lexicon DJ** is the deepest library manager, but its good features sit behind a paid tier (subscription *or* a one-time lifetime license) and it nudges you toward an online account.
- **Rekordbox** is "free" if you accept Pioneer's account ecosystem, has had a "MOOD: HIGH/MID/LOW" column for years, and *still* won't auto-detect genre — the #1 request on their own forum.

Vibechek does all of that, plus the things none of them offer — ML genre classification across ~400 Discogs subgenres and timeslot tagging (Opener / Warm-Up / Peak / Afterhours) — alongside acoustic de-duplication, genre-folder organization, and GPU-accelerated analysis, running entirely on your machine with no accounts, no telemetry, and zero recurring cost. It's also the only one that's open source.

```
$0 forever  •  no account  •  no upload  •  AGPL-3.0  •  CPU or GPU
```

---

## What Vibechek does that the paid tools don't

_Verified 2026-05-29 — sources + caveats in [docs/COMPETITORS.md](docs/COMPETITORS.md)._

| Capability | Vibechek | Mixed In Key | Lexicon | Rekordbox | beaTunes | Tunebat |
| --- |:--:|:--:|:--:|:--:|:--:|:--:|
| ML genre + subgenre (Discogs-400 taxonomy) | ✅ | — | — | — | — | — |
| Timeslot tag (Opener / Warm-Up / Peak / Afterhours) | ✅ | — | — | — | — | — |
| Energy rating | ✅ 0-5 | ✅ 1-10 | ✅ | — | loudness | ✅ (Pro) |
| Mood labels | ✅ | — | Spotify | HIGH/MID/LOW¹ | inferred | "Happiness" |
| **Acoustic** duplicate detection | ✅ | — | ✅ | metadata | ✅ | — |
| Bulk auto-organize into Genre/Subgenre folders | ✅ | — | ✅ | — | — | — |
| Full tag backup / restore (incl. binary frames) | ✅ | — | DB only | library DB | — | — |
| Preserves Rekordbox GEOB/PRIV cue frames | ✅ | n/a | sync only | native | — | n/a |
| Works offline, no account | ✅ | online+acct | paid acct | acct | ✅ | web app |
| Open source | **AGPL-3.0** | — | — | — | — | — |
| GPU acceleration | ✅² | — | — | — | — | — |
| Price | **$0** | $58 once | $10-20/mo or $199-399 once | $0-36/mo | €34.95 once | freemium |

¹ Rekordbox's HIGH/MID/LOW is a track-structure label, not an energy rating.

² NVIDIA CUDA is validated (WSL/Linux, both the essentia-tensorflow and ONNX engines). AMD (ROCm) and Apple (CoreML) are wired via the opt-in ONNX engine but hardware-unverified. The Windows-default native engine bundles CPU-only ONNX Runtime — switch to the ONNX engine for GPU there.

The honest gaps none of them fill: **ML genre/subgenre auto-detection** and **timeslot tagging** — no tool here does either — plus being **local-first, open source, and $0**. Several do more than they're sometimes credited for (Lexicon and beaTunes both have acoustic de-dup; Lexicon organizes into genre folders too), so Vibechek's pitch is the *combination*, run locally and for free, not a checklist nobody else can touch.

---

## The headline features

### What the ML reads from each track

Vibechek uses the [Discogs-EffNet model](https://essentia.upf.edu/models.html), trained on the largest electronic-music taxonomy in existence, to classify every track on:

- **Genre + subgenre** across ~400 categories
- **BPM and key** (Camelot wheel notation)
- **Energy** on a 0-5 scale
- **Mood** (Dark / Neutral / Bright)
- **Timeslot** (Opener / Warm-Up / Peak / Afterhours)
- **Direction** (Up / Steady / Down — does the track build or wind down?)
- **Vocal type** (Vocal / Light Vocal / Instrumental)
- **Danceability**

You get a tunable confidence threshold per attribute. Tracks below the bar don't get rewritten.

### Genre: three classifiers + tag reconciliation

The Discogs-EffNet head above is the default. But genre is the hardest field to get right, so Vibechek lets you pick how it's decided — and it's smart about libraries that already carry tags:

- **Trust your existing tags (default).** Beatport/curated tags are usually better than any audio guess, so Vibechek *keeps a specific existing genre* and uses the model only to fill gaps — while ignoring generic junk ("Dance/Pop", "Electronic") and letting a confident model read override a tag that's clearly wrong. Fully configurable (`prefer_tag` / `prefer_ml` / `tag_only` / `ml_only`).
- **CLAP audio classifier (opt-in).** A modern audio-embedding model matched against a curated reference library — **roughly 2× the genre accuracy** of the Discogs head on pure audio, and unlike a tag it works on **untagged / white-label** tracks. One-click *Set up CLAP genre engine* in Settings (a ~2.2 GB model, downloaded once). BPM/key/mood are unchanged.
- **Online genre lookup (opt-in).** Searches for the track's artist + title, opens the store pages the search returns, and reads the genre straight off the page — keeping it only when that page names this exact track, and refusing shop categories like "Dance/Pop" that aren't really genres. Layers in as **tag › verified web › audio**: the most accurate option on tagged libraries, **73% exact / 87% family** on our adjudicated test corpus against 52%/71% for tags and audio alone. No AI model, no account, no API key. One-click *Set up online lookup*.
- **Conflicts surfaced, never silently resolved.** When your tag, the audio model, and the web lookup disagree, Vibechek flags the track for **one-click review** instead of quietly picking a winner — an "N to review" toolbar filter and a per-row marker, plus a **Genre sources** panel in Track Details showing all three reads, which one won, and why ("Changed your tag *Tech House* → *Trance* — the audio model disagreed"). It augments your curation; it never overwrites it behind your back.
- **Import your Rekordbox tags.** Rekordbox keeps your genre edits in its own database — they never reach the files. Import its collection XML and the genre you curated there joins the reconciliation at the trusted "your tag" tier (labelled *Your tag (Rekordbox)*), remembered across re-analyses. Embedded key tags and Mixed In Key *Energy N* comments surface too — as context, not overrides: measured on real libraries, tag keys from other tools' analyzers are simply less accurate than Vibechek's audio read (49% vs 63% exact), so a disagreeing tag key flags the track instead of silently replacing the better value.

All of it feeds the same reconciliation, so the **default behavior is unchanged unless you opt in** — and you can mix them (e.g. trust tags, fall back to the CLAP model, escalate to web only when unsure).

### De-duplication that doesn't lie — *and knows a remix isn't a dupe*

MD5 catches the same MP3 saved twice. **Chromaprint** catches the same song saved as FLAC *and* MP3 — by listening to the audio itself. But it's **variant-aware**: by default it *keeps* the versions a DJ actually wants side by side — an **Extended** vs **Radio** vs **Remix** edit, or a FLAC *Original Mix* next to an MP3 *Extended* — and only collapses true duplicates *within* the same version. Every part is configurable (collapse across versions, keep one file per format, duration tolerance for mislabeled lengths). Auto-keeper rules then pick the best file by codec → bitrate → file size → newest → shortest path (or whatever order you set), and you can override any choice before anything moves.

### One-click organize

Plan and execute a clean `Music/Genre/Subgenre/` tree from your analysis. Rare genres bucket into `Other/` (threshold you control). Dry-run before commit. Hierarchy rules let you tune subgenre handling, target root, naming.

### Tag backup that survives a rewrite

One click snapshots every ID3, Vorbis, and MP4 tag — **including the binary GEOB and PRIV frames Rekordbox stores cue points and beat grids in.** Most tag editors silently strip these when they rewrite a file. Vibechek preserves them by default and offers full restore. Your performance data is never at risk.

### Cross-platform GPU acceleration — *and* hybrid CPU+GPU

Got a GPU? Vibechek probes the *actual analysis engine* (not just the host) to see if TensorFlow can really use it... AND, if your GPU is hardware-visible but missing CUDA runtime libraries, the UI says so plainly and offers a one-click "Enable GPU" install. No false promises, no silent CPU fallback you don't know about.

Better yet: **hybrid analysis runs your GPU and your spare CPU cores at the same time.** A modest laptop GPU might only fit ~3 analysis workers in VRAM, which used to leave 16 CPU cores idle. Now Vibechek runs GPU workers *and* CPU workers against one shared work queue that self-balances — whichever device finishes a track grabs the next. On an RTX 4070 Laptop + i9, a 50-track run split GPU 9 / CPU 41 and used every resource at once. Toggle it in Settings.

The worker slider tells the truth about what it can actually deliver: its max is computed live from your engine, your chosen genre classifier, and measured resources — the WSL VM's RAM (not the host's) for WSL-routed engines, since CLAP costs ~4.5 GB/worker against ~0.8 GB for the default classifier. A mid-run clamp streams its reason to the GUI ("Workers capped 16→2: CLAP needs ~4.4 GB each; the WSL VM has 15.5 GB") instead of silently running fewer workers than you asked for. And before a WSL-routed analyze starts, Vibechek verifies the engine venv still imports its ML stack and repairs it in place — including restoring CUDA libraries a WSL reinstall wiped out — so a stale environment heals itself instead of failing partway through a run.

There's also a selectable **ONNX Runtime inference engine** (Settings → Analysis → **Inference engine**, opt-in) that runs the same models with **cross-vendor GPU acceleration** and **drops the end-of-life TensorFlow runtime entirely** (it runs on plain Essentia + ONNX Runtime + converted heads, in a separate `~/.vibechek/venv-onnx`). **NVIDIA CUDA is validated** (the backbone runs GPU-accelerated on an RTX 4070, TF-free); **AMD (ROCm, native Linux) and Apple (CoreML) are wired** via ONNX Runtime's execution-provider chain. On Windows the ONNX path runs inside WSL, because Essentia has no Windows wheel — the WSL-free native default is a separate, CPU-only path. Validated to match the default engine on real tracks. The default engine is the bundled native engine on Windows and essentia-tensorflow on macOS/Linux; flip to ONNX in Settings and click **Set up ONNX engine** to provision it (the installer auto-picks the GPU runtime for your hardware). Extras: `vibechek[onnx]` (CPU) and `vibechek[onnx-gpu]`.

### Undo that survives a crash mid-run

Organize and dedupe-move write an append-only journal as they go — one flushed line per file moved, *before* the next move. So a run that dies halfway (disk full, power loss) is recoverable, and a finished run can be reverted with one click from the **Recent operations** panel. Files go back to exactly where they came from, newest-first, never clobbering anything that's since moved into the origin.

### Tag exactly the fields you want

Every ML field has an independent write toggle — genre, BPM, key, energy, mood, timeslot, direction, vocal. BPM and key default *off* (Rekordbox's own detection is usually better), genre is additionally gated by a confidence threshold, and the rest write whenever you want them to. Vocal detection has a tunable sensitivity (Instrumental ≤ / Vocal ≥) that re-labels tracks **without re-analyzing**, because the raw model score is stored alongside the label.

### One global player, always in reach

A single persistent player bar lives at the app root. Preview any track from anywhere, and it follows you across tabs. Two previews can never overlap, every track starts cleanly at 0:00, and there's always a visible stop control.

### FLAC → CDJ export (play FLAC on old Pioneer decks)

Older CDJs (CDJ-2000nexus and earlier) can't read FLAC. `vibechek cdj-export <rekordbox.xml>` transcodes your FLACs to **AIFF** — a *sample-identical* decode, so your cue points and beat grids copy across with zero drift — and rewrites a Rekordbox XML you re-import and export to USB. It never uses MP3 (encoder delay shifts the grid ~26 ms), never touches your source files, and preserves the `TEMPO`/`POSITION_MARK` data byte-for-byte. Your FLAC library plays on the club's first-gen decks with every cue intact.

### Zero-CLI setup on every platform

Every other tool we benchmarked makes you set up Python or pip or some random runtime by hand on some systems. Vibechek is the only one where the GUI does it for you on **every OS**:

- **Windows.** The desktop installer bundles a WSL-free native engine (essentia + ONNX Runtime folded into the sidecar) and defaults to it, so a fresh install analyzes fully in-process — no WSL, nothing to set up. Click *Analyze* and it runs. WSL is only the fallback: a lean CLI zip / pip install, or the essentia-tensorflow and ONNX engines, still auto-install WSL Ubuntu via UAC and route through it with transparent path translation (`C:\Music` ↔ `/mnt/c/Music`).
- **macOS & Linux.** Vibechek creates a hermetic Python venv at `~/.vibechek/venv/`, installs `essentia-tensorflow + vibechek` into it, and routes analysis through that venv. Doesn't touch your system Python. Click *Install Essentia* in the Preflight dialog; ~3-5 minutes later it's running.
- **GPU where it's available.** NVIDIA CUDA is validated on Linux and (via WSL) Windows, on both engines. AMD (ROCm) and Apple (CoreML) run through the opt-in ONNX engine but are hardware-unverified. The Windows-default native engine is CPU-only — switch to the ONNX engine in Settings for GPU there.

No terminal required. On any platform.

---

## Built for power users *and* people who hate CLIs

- **Desktop app** (Tauri 2.x + React) for click-and-go users. Five tabs: Library, Duplicates, Organize, Tags, Settings — plus a persistent audio player bar and a **Recent operations** undo panel.
- **CLI** (`vibechek`) for scriptability, headless servers, cron jobs, Makefiles. Every GUI button is a subcommand.
- Cancel any long operation at any time. Progress bars are real (byte-level for downloads, track-level for analysis).
- Auto-saved analysis state per library. Re-open the app, your last library is right there. One-click **DJ profiles** preset your settings for different workflows.

```
vibechek analyze ~/Music         # full ML pass (CPU, GPU, or hybrid)
vibechek dedupe ~/Music          # MD5 + Chromaprint
vibechek organize analysis.json  # plan + execute genre folders
vibechek tag analysis.json       # apply tags (Rekordbox-safe)
vibechek backup-tags ~/Music     # snapshot before any write
vibechek journals                # list past organize/dedupe operations
vibechek revert <journal>        # undo an organize/dedupe move
```

---

## Install

**End users.** Grab the installer for your OS from the [Releases page](https://github.com/captinjack99/Vibechek/releases). The first time you click *Analyze*, the in-app setup walks you through everything missing:

- **Windows** → nothing to install. The desktop installer bundles the WSL-free native engine and defaults to it, so *Analyze* runs in-process on a fresh install. (Fallback for the CLI zip / pip installs, or the essentia-tensorflow / ONNX engines: the in-app setup auto-installs WSL Ubuntu (UAC prompt), Vibechek + Essentia inside it, then optionally CUDA libs for GPU.)
- **macOS / Linux** → creates a managed venv at `~/.vibechek/venv/` and installs Essentia + Vibechek into it. Doesn't touch your system Python.

macOS/Linux setup is ~5-10 minutes total, no terminal, no `pip` to remember; Windows is instant. Full walkthrough: [docs/USER_GUIDE.md](docs/USER_GUIDE.md).

> **macOS (beta builds are unsigned):** the `.dmg` isn't notarized yet, so on first launch macOS says *"Vibechek is damaged / can't be verified."* It isn't — it's just unsigned. Right-click **Vibechek.app → Open → Open**, or run `xattr -dr com.apple.quarantine /Applications/Vibechek.app` once. Signed + notarized builds land with the stable release.

**Developers** (for contributing or running from source):

```bash
git clone https://github.com/captinjack99/Vibechek.git
cd Vibechek

# Python core
python -m venv .venv
. .venv/Scripts/activate          # Windows
# source .venv/bin/activate       # macOS / Linux
pip install -e ".[dev]"

# Frontend
cd ui && npm install

# Run desktop app in dev mode
# First: ../packaging/build-windows.bat (Windows) or ./packaging/build-{linux,macos}.sh
$env:VIBECHEK_SIDECAR = "$pwd\..\.venv\Scripts\vibechek.exe"   # Windows
# export VIBECHEK_SIDECAR=$PWD/../.venv/bin/vibechek            # macOS/Linux
npm run tauri:dev
```

Full developer setup + the platform-specific bits: [docs/INSTALL.md](docs/INSTALL.md).

---

## Architecture

```
React UI ──[Tauri invoke]──► Rust shell ──[JSON-RPC stdin/stdout]──► Python sidecar
                                                                          │
                              ┌───────────────────────────────────────────┴───────────┐
                              │ vibechek package (51 RPC methods)                     │
                              │  analyzer · tagger · duplicates · organizer · genres  │
                              │  clap_genre · genre_web · journal · profiles · config  │
                              │  cancellation · onnx_backend                           │
                              │  library_state · backup_history · preflight · wsl      │
                              │  resources · logging_setup                             │
                              └───────────────────────────────────────────────────────┘
```

- Python sidecar handles every long-running operation in a thread pool (8 workers) so the UI never freezes.
- Long ops are cancellable. JSON-RPC progress notifications stream live to the UI; analyzed tracks also stream in one-by-one so the library populates live.
- Analysis runs CPU-only, GPU-only, or **hybrid CPU+GPU** (work-stealing across one shared queue).
- On Windows without native Essentia, analyze transparently routes through `vibechek` in WSL Ubuntu.
- Auto-generated TypeScript types mirror Python dataclasses so the wire stays type-safe.

Full deep dive: [ui/README.md](ui/README.md).

---

## What's on the roadmap

| Phase | Goal                                                                                                                 | Status                       |
| ----- | -------------------------------------------------------------------------------------------------------------------- | ---------------------------- |
| 1     | Package the proven Python pipeline into `vibechek`                                                                   | ✅ Done                       |
| 2     | Cross-platform installer + CI release pipeline (Win/macOS/Linux; code signing opt-in)                                | ✅ Done                       |
| 3     | Desktop UI, full WSL automation, GPU truth detection                                                                 | ✅ Done                       |
| 4     | Polish, docs, community launch                                                                                       | 🚧 In progress (you're here) |
| 5+    | Smart playlist rules engine • Mashup recommender • Cue-point auto-generation • MusicBrainz lookup • Mobile companion | 💭 Ideas                     |

See [docs/ROADMAP.md](docs/ROADMAP.md) for the full breakdown, plus features competitors have that Vibechek deliberately doesn't (cross-DAW cue sync, cloud library backup, real-time streaming analysis).

---

## Stats

<!-- STATS_LINE_START -->
**1202 Python tests** · **51 JSON-RPC methods** · **38 Python modules** · auto-updated by `scripts/update_readme_stats.py`
<!-- STATS_LINE_END -->

- 197 frontend tests across 26 files (App, rpc, AnalysisProgress, ConfirmModal, DuplicatesView, ErrorToast, GlobalAudioPlayer, LibraryBrowser, LibraryFilters, MemoryRefusalActions, OnnxSetupDialog, OperationsHistory, OrganizeView, PreflightDialog, Settings, SettingsSystem, Sidebar, TagsView, useApplyTags, useConfigPersistence, useSidecar, keeperRules, review, library + notification + operation stores)
- ~4,500 LOC of core logic, 5 main views, threadpool dispatch with cancellation singleton
- Used in production by the author against a 12,000-track personal DJ library

---

## Contributing

The whole point of Vibechek being OSS is that DJs-who-code can shape it. Contributions welcome — small, medium, or "redesign the timeslot algorithm" large.

1. Fork + branch.
2. Make your change. Add tests where you can.
3. `./.venv/Scripts/python.exe -m pytest -q` and `cd ui && npm test`.
4. If you touched a Python dataclass, regenerate TS: `./.venv/Scripts/python.exe scripts/generate_ts_types.py`.
5. Open a PR.

If you're more of an ideas person than a code person — [open an issue](https://github.com/captinjack99/Vibechek/issues). Especially: what's missing from your DJ workflow that no tool currently does?

---

## Acknowledgements

- [Essentia](https://essentia.upf.edu/) — the open ML audio library from UPF Barcelona that powers everything
- [Discogs-EffNet](https://essentia.upf.edu/models.html) — the default genre classification model. The bundled MTG model weights are **CC BY-NC-SA 4.0**, not AGPL — full inventory, attribution, and license notices in [THIRD_PARTY_MODELS.md](THIRD_PARTY_MODELS.md)
- [LAION-CLAP](https://github.com/LAION-AI/CLAP) — the audio-embedding model behind the opt-in CLAP genre classifier
- [ddgs](https://github.com/deedy5/ddgs) — the keyless web search behind the opt-in online genre lookup
- [Chromaprint](https://acoustid.org/chromaprint) — acoustic fingerprinting
- [Mutagen](https://mutagen.readthedocs.io/) — careful audio tag I/O
- [Tauri](https://v2.tauri.app/) — the small, fast desktop shell that makes "ship a Rust+React+Python app" reasonable

## License

AGPL-3.0-or-later. See [LICENSE](LICENSE). TL;DR: use it, fork it, modify it — but if you ship a modified version to others (including as a hosted web service), they must get the source too.

If Vibechek saves you the cost of a Mixed In Key license, consider [sponsoring development](https://github.com/sponsors/captinjack99) or just starring the repo. It actually helps.
