# Vibechek

**Open-source ML for your DJ library.** Auto-tag genre, mood, energy, BPM, and key. Find duplicates the way your ears would. Organize 10,000 tracks in an afternoon. Keep every Rekordbox cue point intact.

> **Status:** `v0.3.0-beta.1` — feature-complete, headed for stable. Battle-tested on a real 12,000-track personal library. Cross-platform (Windows / macOS / Linux). Free forever under AGPL-3.0.

---

## Why Vibechek?

Every paid library tool makes one of three trade-offs:

- **Mixed In Key** is the gold standard for key + energy, but charges $58, only detects two attributes, and runs nothing else.
- **Lexicon DJ** is the deepest library manager, but the good features sit behind a $20/month subscription and require an online account.
- **Rekordbox** is "free" if you accept Pioneer's account ecosystem, has had a "MOOD: HIGH/MID/LOW" column for years, and *still* won't auto-detect genre — the #1 request on their own forum.

Vibechek does all of that, plus the things they don't bother with — ML genre classification across 400 Discogs subgenres, timeslot tagging (Opener / Warm-Up / Peak / Afterhours), Chromaprint-based acoustic de-duplication that catches re-encodes and remixes, and runs entirely on your machine with no accounts, no telemetry, and zero recurring cost.

```
$0 forever  •  no account  •  no upload  •  AGPL-3.0  •  CPU or GPU
```

---

## What Vibechek does that the paid tools don't

| Capability                                          | Vibechek     | Mixed In Key | Lexicon       | Rekordbox    | beaTunes | Tunebat     |
| --------------------------------------------------- |:------------:|:------------:|:-------------:|:------------:|:--------:|:-----------:|
| ML genre + subgenre (Discogs-400 taxonomy)          | ✅            | —            | —             | —            | —        | —           |
| Timeslot tag (Opener / Warm-Up / Peak / Afterhours) | ✅            | —            | —             | —            | —        | —           |
| Energy 0-5 + Dark/Neutral/Bright mood               | ✅            | Energy only  | —             | HIGH/MID/LOW | Loudness | "Happiness" |
| **Acoustic** duplicate detection (Chromaprint)      | ✅            | —            | filename only | —            | filename | —           |
| Bulk auto-organize into Genre/Subgenre folders      | ✅            | —            | partial       | —            | —        | —           |
| Full tag backup / restore (incl. binary frames)     | ✅            | —            | $/mo tier     | $/mo tier    | —        | —           |
| Preserves Rekordbox GEOB/PRIV cue frames            | ✅            | n/a          | sync only     | native       | unknown  | —           |
| Works offline, no account                           | ✅            | ✅            | account req.  | account req. | ✅        | upload req. |
| Open source                                         | **AGPL-3.0** | —            | —             | —            | —        | —           |
| GPU acceleration                                    | ✅            | —            | —             | —            | —        | —           |
| Price                                               | **$0**       | $58 once     | $10-20/mo     | $0-30/mo     | ~$35     | freemium    |

Stacking the two most popular paid tools together (Mixed In Key + Lexicon DJ) still costs ~$58 up front + $10-20/month and *still* doesn't ML-classify genre, *still* doesn't tag timeslot, *still* doesn't do acoustic de-dup. Vibechek does all three, runs locally, and ships it open source.

---

## The headline features

### Machine Learning that actually knows your tracks

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

### 🔍 De-duplication that doesn't lie

MD5 catches the same MP3 saved twice. **Chromaprint** catches the same song saved as FLAC *and* MP3 *and* `(Original Mix)` *and* `(Extended Mix)` — by listening to the audio itself. Auto-keeper rules pick the best version by codec → bitrate → file size → newest → shortest path (or whatever order you configure), and you can override any choice before anything moves.

### 🗂️ One-click organize

Plan and execute a clean `Music/Genre/Subgenre/` tree from your analysis. Rare genres bucket into `Other/` (threshold you control). Dry-run before commit. Hierarchy rules let you tune subgenre handling, target root, naming.

### 🛟 The tag backup nobody else ships

One click snapshots every ID3, Vorbis, and MP4 tag — **including the binary GEOB and PRIV frames Rekordbox stores cue points and beat grids in.** Most tag editors silently strip these when they rewrite a file. Vibechek preserves them by default and offers full restore. Your performance data is never at risk.

### 🤖 Cross-Platrform GPU acceleration

Got a GPU? Vibechek probes the *actual analysis engine* (not just the host) to see if TensorFlow can really use it... AND, if your GPU is hardware-visible but missing CUDA runtime libraries, the UI says so plainly and offers a one-click "Enable GPU" install. No false promises, no silent CPU fallback you don't know about. This helps speed up the analysis engine tremendously.

### 🪟 🍎 🐧 Zero-CLI setup on every platform

Every other tool we benchmarked makes you set up Python or pip or some random runtime by hand on some systems. Vibechek is the only one where the GUI does it for you on **every OS**:

- **Windows.** Essentia has no Windows wheel. Vibechek detects that, auto-installs WSL Ubuntu via UAC, creates a venv inside WSL, installs Essentia, and routes analysis through it — paths get translated `C:\Music` ↔ `/mnt/c/Music` under the hood. You click *Install Essentia*; the right thing happens.
- **macOS & Linux.** Vibechek creates a hermetic Python venv at `~/.vibechek/venv/`, installs `essentia-tensorflow + vibechek` into it, and routes analysis through that venv. Doesn't touch your system Python. Click *Install Essentia* in the Preflight dialog; ~3-5 minutes later it's running.
- **GPU on any of the above.** Full GPU support across all OS platforms.

No terminal required. On any platform.

---

## Built for power users *and* people who hate CLIs

- **Desktop app** (Tauri 2.x + React) for click-and-go users. Five tabs: Library, Duplicates, Organize, Tags, Settings.
- **CLI** (`vibechek`) for scriptability, headless servers, cron jobs, Makefiles. Every GUI button is a subcommand.
- Cancel any long operation at any time. Progress bars are real (byte-level for downloads, track-level for analysis).
- Auto-saved analysis state per library. Re-open the app, your last library is right there.

```
vibechek analyze ~/Music         # full ML pass
vibechek dedupe ~/Music          # MD5 + Chromaprint
vibechek organize analysis.json  # plan + execute genre folders
vibechek tag analysis.json       # apply tags (Rekordbox-safe)
vibechek backup-tags ~/Music     # snapshot before any write
```

---

## Install

**End users.** Grab the installer for your OS from the [Releases page](https://github.com/papapew/Vibechek/releases). The first time you click *Analyze*, the in-app setup walks you through everything missing:

- **Windows** → auto-installs WSL Ubuntu (UAC prompt), Vibechek + Essentia inside it, then optionally CUDA libs for GPU acceleration.
- **macOS / Linux** → creates a managed venv at `~/.vibechek/venv/` and installs Essentia + Vibechek into it. Doesn't touch your system Python.

Either way: ~5-10 minutes total, no terminal, no `pip` to remember. Full walkthrough: [docs/USER_GUIDE.md](docs/USER_GUIDE.md).

**Developers** (for contributing or running from source):

```bash
git clone https://github.com/papapew/Vibechek.git
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
                              │ vibechek package (29 RPC methods)                     │
                              │  analyzer · tagger · duplicates · organizer · genres  │
                              │  config · cancellation · library_state · backup_history│
                              │  preflight · wsl · resources · logging_setup           │
                              └───────────────────────────────────────────────────────┘
```

- Python sidecar handles every long-running operation in a thread pool (8 workers) so the UI never freezes.
- Long ops are cancellable. JSON-RPC progress notifications stream live to the UI.
- On Windows without native Essentia, analyze transparently routes through `vibechek` in WSL Ubuntu.
- Auto-generated TypeScript types mirror Python dataclasses so the wire stays type-safe.

Full deep dive: [ui/README.md](ui/README.md).

---

## What's on the roadmap

| Phase | Goal                                                                                                                 | Status                       |
| ----- | -------------------------------------------------------------------------------------------------------------------- | ---------------------------- |
| 1     | Package the proven Python pipeline into `vibechek`                                                                   | ✅ Done                       |
| 2     | Cross-platform installer + signed CI release pipeline                                                                | ✅ Done                       |
| 3     | Desktop UI, full WSL automation, GPU truth detection                                                                 | ✅ Done                       |
| 4     | Polish, docs, community launch                                                                                       | 🚧 In progress (you're here) |
| 5+    | Smart playlist rules engine • Mashup recommender • Cue-point auto-generation • MusicBrainz lookup • Mobile companion | 💭 Ideas                     |

See [docs/ROADMAP.md](docs/ROADMAP.md) for the full breakdown, plus features competitors have that Vibechek deliberately doesn't (cross-DAW cue sync, cloud library backup, real-time streaming analysis).

---

## Stats

<!-- STATS_LINE_START -->
**487 Python tests** · **44 JSON-RPC methods** · **27 Python modules** · auto-updated by `scripts/update_readme_stats.py`
<!-- STATS_LINE_END -->

- 24 frontend tests across keeperRules, LibraryFilters, ConfirmModal, Sidebar
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

If you're more of an ideas person than a code person — [open an issue](https://github.com/papapew/Vibechek/issues). Especially: what's missing from your DJ workflow that no tool currently does?

---

## Acknowledgements

- [Essentia](https://essentia.upf.edu/) — the open ML audio library from UPF Barcelona that powers everything
- [Discogs-EffNet](https://essentia.upf.edu/models.html) — the genre classification model
- [Chromaprint](https://acoustid.org/chromaprint) — acoustic fingerprinting
- [Mutagen](https://mutagen.readthedocs.io/) — careful audio tag I/O
- [Tauri](https://v2.tauri.app/) — the small, fast desktop shell that makes "ship a Rust+React+Python app" reasonable

## License

AGPL-3.0-or-later. See [LICENSE](LICENSE). TL;DR: use it, fork it, modify it — but if you ship a modified version to others (including as a hosted web service), they must get the source too.

If Vibechek saves you the cost of a Mixed In Key license, consider [sponsoring development](https://github.com/sponsors/papapew) or just starring the repo. It actually helps.
