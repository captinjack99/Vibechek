# Vibechek User Guide

A 10-minute walkthrough of what Vibechek does and how to use it. Written for DJs who don't want to read code.

---

## What is Vibechek?

Vibechek looks at your music folder and helps you do four things:

1. **Tag your tracks automatically.** Detects genre, mood, energy, BPM, key, vocal type — all via machine learning. Writes the results to your files so Rekordbox / Serato / Traktor can see them.
2. **Find duplicates.** Catches both byte-identical copies (the same MP3 saved twice) and re-encodings (same song saved as FLAC and MP3).
3. **Organize into folders.** `Music/House/Deep House/`, `Music/Techno/Minimal Techno/`, etc.
4. **Back up your tags.** Before any of the above, take a one-click snapshot of every tag on every file. If anything goes wrong, restore.

**Crucially: Vibechek never touches your cue points, beat grids, or memory cues.** Those are stored as binary frames in your MP3 files; Vibechek reads them, holds them safely, and writes them back exactly as it found them. Whether you use Rekordbox, Serato, or Traktor, your performance data is preserved.

---

## First launch

1. Download the installer from [Releases](https://github.com/captinjack99/Vibechek/releases).
2. Run it. On Windows the installer offers "Add to PATH" — you can leave it unchecked unless you want to use the CLI.
3. Launch Vibechek.

A short tour overlay walks you through the basics. You can skip it.

You'll see five tabs in the sidebar:

| Tab | Use it for |
|---|---|
| **Library** | Open a folder, browse tracks, run ML analysis |
| **Duplicates** | Find and remove duplicates |
| **Organize** | Move files into genre/subgenre folders |
| **Tags** | Back up / restore tags |
| **Settings** | Tune defaults, see system resources, view logs |

Two things live outside the tabs: a **persistent audio player bar** (preview any track
from anywhere — it follows you across tabs and never plays two previews at once) and a
**Recent operations** entry in the sidebar that lists past organize/dedupe runs with a
one-click Undo.

---

## Workflow 1: First scan of a new library

### Step 1 — Open a folder

Library tab → **Open folder** → pick your music folder.

You get two big buttons:

- **Just show me my library** — instant. Reads filenames and any existing tags. No ML, no waiting. Use this if you mostly want to browse, dedupe, or organize.
- **Analyze with ML** — the full ML pass. Reads every file, runs all the models, gives you genre / mood / energy / BPM / key / direction / vocal for every track. On a 12k-track library this takes roughly 1-3 hours on CPU, much less with a GPU — and Vibechek can run **hybrid CPU+GPU** to use both at once. Tracks stream into the list as they finish, and it's cancellable any time.

### Step 2 — (Windows) Setup, only if needed

On Windows the desktop app ships a **bundled native analysis engine** and uses it by
default, so a fresh install just starts analyzing — nothing to install. Skip to Step 3.

Setup only appears on a fallback path: the CLI zip, or after you switch to the
essentia-tensorflow / ONNX engine, or turn on the opt-in CLAP / online-lookup genre
engines (which still run through WSL on Windows). Then the first time you click
**Analyze with ML** a dialog shows what's missing:

- **WSL not installed?** → click "Install WSL + Ubuntu". You'll get a UAC prompt (this is Windows enabling a system feature). 5-15 minutes.
- **Vibechek + Essentia not in WSL?** → click "Install in Ubuntu". 3-5 minutes. No prompts.
- **Models not downloaded?** → click "Download models now". ~800 MB, takes a couple of minutes on a fast connection.

When everything's green, the dialog closes itself and analysis starts.

### Step 3 — Wait

A progress overlay at the bottom shows current track / total + elapsed time. You can:
- Click around other tabs — the analyze keeps running in the background.
- Hit **Cancel** to stop. Partial results aren't kept; restart from scratch when you're ready.

Before dispatching to a WSL-routed engine (essentia-tensorflow, ONNX, or the opt-in
CLAP / online-lookup genre engines), Vibechek quietly verifies that environment
still imports its ML stack and repairs it in place if not — including restoring GPU
libraries a WSL reinstall wiped out. If that happens you'll see a line like
"Restoring GPU libraries…" scroll through the progress overlay instead of the run
just failing; it's normal (a one-time multi-GB download of the cuDNN/cuBLAS wheels,
usually a few minutes) and then analyze proceeds. Set the environment
variable `VIBECHEK_NO_AUTOHEAL=1` if you'd rather Vibechek only report a broken
environment and never repair it automatically.

If you asked for more workers than fit in memory for the current engine and genre
classifier, the overlay also shows a line like *"Workers capped 16→2: CLAP needs
~4.4 GB each; the WSL VM has 15.5 GB"* — the run doesn't silently do less than you
asked for. See **Settings → Analysis** below for why the number varies.

When done, the track list populates. Each row shows genre, BPM, key, energy bar. A
toast in the corner reports how the run actually went — see the next section.

### What the completion toast tells you

The analyze completion toast is honest about anything that went differently than
"clean run, everything written":

- **Per-track failures** — "N tracks failed" with a pointer to the **Errors** filter.
- **Could not be saved** — if writing the results to disk failed (disk full, a
  OneDrive/Google Drive or antivirus lock on the analyses file), the toast says so
  explicitly and tells you to re-run or export before closing the app — the results
  are on screen but not yet safe on disk.
- **Rekordbox priors stopped matching** — if you've imported a Rekordbox XML (see
  below) and none of its entries matched this run, the toast tells you your library
  was likely moved or renamed and to re-import the XML.
- **A genre classifier or model fell back** — e.g. CLAP was selected but unavailable
  for some tracks and those were scored by Discogs instead, or a mood/vocal model
  failed to load (the affected fields are named).
- **The engine environment was auto-repaired** — "engine environment repaired
  automatically" if the self-heal above kicked in, or a caution if a GPU-library
  restore failed and the run fell back to CPU.

**"Analyze new (N)"** (the incremental option, next to "Analyze with ML" once a
library has already been analyzed once) only scans tracks it hasn't seen before and
merges them into your existing saved results. If Vibechek can't currently read the
existing saved analysis for this library — a transient file lock from antivirus or
cloud sync, or a truncated file from a crash — it **refuses to run** rather than risk
overwriting your whole library's results with just the newly scanned tracks. Close
whatever might be locking the file and try again, or run a full re-analyze.

### Step 4 — Click a track to see details

Right-side panel slides in:
- **Audio waveform + play button** — preview a clip
- **File metadata** — path, size, filename-derived hints
- **Tag diff** — current tags vs ML results, with an arrow on anything that'd change
- **Confidence indicator** — green check if above threshold, yellow warning if below
- **Genre sources** — when your existing tag, the audio model, and the (optional) web lookup disagree on the genre, this panel shows all three reads, which one won, and a plain-English reason. So when Vibechek changes a tag you can see *why* — and decide whether to trust it.

A track whose genre sources disagree also gets a small ⚠ marker in the list and is counted by the **"N to review"** filter in the toolbar (see Workflow 2). Nothing is written to your files from the detail panel unless you click **Apply ML tags to this file**; more commonly, you'll do bulk tagging next.

---

## Workflow 2: Apply ML tags in bulk

After an analysis, you have ML results in memory but they aren't written to your files yet. To commit them:

1. (Optional but recommended) **Tags tab → Create backup**. Pick a save location. You can restore from this if anything goes wrong.
2. **Library tab → Apply ML tags to all** (or select specific tracks with the checkboxes first, then **Apply ML tags to N**).
3. Confirm dialog shows:
   - Total tracks affected
   - **Genre breakdown** — e.g., "Will write: 2,400 House, 800 Techno, 23 across 5 other genres". Only counts tracks above your confidence threshold (default 85%).
   - Reminder that BPM and key are skipped by default (Rekordbox is more reliable)
4. Click **Yes, apply tags**.

Result: another confirm-ish toast at the bottom-right when done.

### What about tracks below the confidence threshold?

By default, tracks with ML genre confidence below 85% don't get their genre rewritten. Everything else (energy, mood, timeslot, etc.) still gets written. The threshold lives in Settings → Tagging → "Genre confidence threshold".

### Filtering before applying

Use the **filter chips** in the toolbar to narrow down which tracks you're targeting:

- **Genre** dropdown → multi-select genres
- **Energy** dropdown → energy levels 0-5
- **Mood** → Dark / Neutral / Bright
- **Vocal** → Instrumental / Light Vocal / Vocal
- **Direction** → Up / Steady / Down
- **Key** → the Camelot wheel, with compatible keys highlighted

Then select all visible tracks (header checkbox) and apply.

There are also two review toggles on the right of the chip bar:

- **"N to review"** — narrows to tracks whose genre sources disagree (your tag vs the audio model vs the web lookup). A fast way to audit just the model's uncertain calls — and the tags Vibechek would *change* — before a bulk write. It composes with the chip filters, so you can ask "show me the *House* tracks I should double-check".
- **"N errors"** — narrows to tracks that failed to scan or analyze.

---

## Workflow 3: Find and remove duplicates

Duplicates tab. Pick your library folder. Click **Scan**.

Two passes run:

1. **MD5 hash** — catches byte-identical files (the same MP3 saved twice).
2. **Chromaprint fingerprint** — catches re-encoded copies (same song as FLAC and MP3, or 320 kbps and 192 kbps).

(If you don't have `fpcalc` installed, only MD5 runs — a yellow "Fingerprint scan
skipped" banner above the results says so explicitly, so a scan that only found
exact-hash duplicates never reads as a clean, thorough sweep. It also names what
you're missing: re-encodes and re-tags of the same track weren't compared.)

### Variants aren't duplicates

By default Vibechek keeps the versions a DJ actually wants side by side — an **Extended**
vs **Radio** vs **Remix** edit, or a FLAC *Original Mix* next to an MP3 *Extended* — and
only flags true duplicates *within* the same version. So a clean "same song, two formats"
pair collapses, but your alternate edits survive. Tune it in **Settings → Duplicate
detection**: collapse across versions if you'd rather keep one per song, keep one file per
format, or adjust the duration tolerance that catches mislabeled lengths.

### The result

A summary strip up top: total files scanned, duplicate groups found, files that'd be removed, disk space freed.

Below: a **rules editor** that picks the "winner" per group automatically. By default:

1. Codec (FLAC > WAV > AIFF > ALAC > M4A > OGG > MP3 > AAC > WMA)
2. Bitrate (higher first)
3. File size (bigger first)
4. Modified date (newer first)
5. Path length (shorter first — top-level wins over deep subfolders)

You can toggle any rule off or drag to reorder. Every group card shows **why** the auto-pick was chosen (e.g., "picked by codec: FLAC").

### Override per-group

Click any file in a group to make it the new keeper. The status updates to show "manual override". If you want to leave a group alone entirely, click **don't change this group**.

### Actually resolve

Two big buttons:

- **Move to review folder** — non-destructive. Picks a folder, moves all duplicates there. You can delete them later when you're sure. This is journaled, so you can **Undo** it from the Recent operations panel (files go back to their originals).
- **Send to trash** — into your OS trash (recoverable until you empty it). Trash is journaled for transparency but flagged non-revertible (the OS recycle bin has no reliable programmatic restore) — recover from the bin itself if needed.

Both show a confirm dialog with the final count + space freed before doing anything.

---

## Workflow 4: Organize into genre folders

Organize tab.

### Step 1 — Pick a source

Two cards:

- **Currently loaded library** — uses what you just analyzed (in memory).
- **Load analysis.json** — pick a saved analysis file from a previous run.

### Step 2 — Set the rules

- **Min tracks per genre folder** (default 10) — genres with fewer tracks get bucketed into `Other/`. Keeps your top level from being polluted with one-off Vaporwave tracks.
- **Use subgenre subfolders** — when on, `House/Deep House/track.mp3`. When off, flat `House/track.mp3`.
- **Target root override** — by default, files move within their current parent. Override to move them to a totally different drive (e.g., from your messy `Downloads/Music/` to a clean `D:\DJ\`).

### Step 3 — Preview

Click **Preview plan**. You see:

- How many moves total
- A list of destination folders with file counts
- Small genres that'd go to `Other/`
- Any planning errors (e.g., file deleted between analysis and now)

### Step 4 — Execute

Click **Execute**. A polished confirm modal shows the first 5 moves as a preview and asks if you want to back up tags first (checked by default; uncheck if you've already backed up via Tags tab).

Click **Yes, move files**.

### Step 5 — Result panel

After execution, you get a full recap:

- "Library organized — Moved 11,847 of 12,000 files into 24 folders"
- A bar chart of where files landed
- Errors if any (expandable list with paths)
- Tag backup location if you opted in
- **View library** button to jump back to the Library tab
- **Organize another folder** to keep going

### Step 6 — Undo if needed

Organize writes an append-only journal as it moves files, so it's revertible. Open
**Recent operations** (in the sidebar), find this organize run, and click **Undo** —
every file goes back to where it came from (newest move first, never overwriting
anything that's since taken the original spot). The Organize result panel also has an
inline **Undo this organize** button. If you opted into the tag backup, you can
additionally restore tags via the Tags tab. The journal is your safety net *and* your
audit trail.

---

## Workflow 5: Tag backup / restore

Tags tab.

### Backup

1. Pick a folder (defaults to your current library).
2. Click **Create backup**.
3. Pick a save location.
4. Wait — it can be many GB for large libraries, mostly base64-encoded Rekordbox binary data.

Done. The backup shows up in **Past backups** with timestamp, file count, and size.

### Restore

Two paths:

- From a backup in your history: click **Restore** on that row.
- From a file: **Restore tags from a backup → Choose a backup file → pick it**.

A danger-style confirm modal warns: this overwrites every tag on every file referenced by the backup. Any changes you've made since the backup will be lost. No automatic undo.

### Stale backup warning

If your most recent backup for the current library is over a month old, Vibechek shows a yellow banner suggesting a fresh one before any tag-write operation.

### Forget without deleting

The **X** on a history row removes the entry from Vibechek's index but does **not** delete the backup file from disk. The file is yours; Vibechek just stops tracking it.

---

## Workflow 6: Play your FLAC library on old CDJs

Older Pioneer CDJs (CDJ-2000nexus and earlier) can't read FLAC. Rather than re-rip
your collection, Vibechek transcodes the FLACs to **AIFF** and rewrites a Rekordbox
XML so your cue points and beat grids come along for the ride.

Why AIFF and not MP3: an AIFF is a *sample-identical* decode of the FLAC, so the
`TEMPO` (beat grid) and `POSITION_MARK` (cue) data copy across with **zero drift**.
MP3 has a ~26 ms encoder delay that shifts the grid, so Vibechek never uses it. Your
original FLAC files are never modified — the export is strictly additive.

This is a CLI workflow (run `vibechek --help` to confirm it's available):

1. **In Rekordbox**, export your collection: **File → Export Collection in xml format**.
   Save it somewhere, e.g. `rekordbox.xml`.
2. **Run the export:**

   ```bash
   vibechek cdj-export rekordbox.xml --out ~/cdj-export
   ```

   (Add `--dry-run` first to see the counts and intended files without writing
   anything.) Vibechek writes the AIFFs plus a rewritten `rekordbox_cdj.xml` into the
   output folder. Non-FLAC tracks pass through unchanged.
3. **Back in Rekordbox**, import the new `rekordbox_cdj.xml` (File → Import).
4. **Export to USB** as usual. Your FLAC library now plays on the club's first-gen
   decks with every cue and grid intact.

Requires `soundfile` (the optional `[cdj]` extra) or `ffmpeg` on your PATH — see
[INSTALL.md](INSTALL.md).

---

## Settings

Top of the page:

- **Ready to analyze?** banner — green if the *engine you've selected* is ready
  (a green Essentia row never appears under a red title for an engine that can't
  actually serve it), otherwise tells you what's missing.
- **System** — CPU cores, RAM, GPU. The GPU row shows what the actual analyze
  engine sees, not just what your host has — see the next section.
- **Analysis** — a workers slider whose max is computed for your current engine +
  genre classifier + measured resources, GPU auto/on/off, models directory + download
  button.

### System — what the GPU row really means

Vibechek goes out of its way to never lie to you about GPU acceleration. The
GPU row in **Settings → System** asks the *actual analyze engine* what it can
see — Essentia's bundled TensorFlow inside WSL for essentia-tensorflow, ONNX
Runtime's provider list for the ONNX engine, and an honest "CPU-only" for the
bundled native engine (below). It is NOT a host-side `nvidia-smi` check. This
means the value reflects what analysis will really use, and GPU *workers* are
only counted when the engine can actually register the device — a card that's
visible but unusable never gets reported as "N GPU workers" that silently run
on CPU.

Possible states for essentia-tensorflow / ONNX:

- **GPU available** (green) — the engine registered the GPU. Analyze will use
  it. Shows your card name, e.g. *"NVIDIA GeForce RTX 4070 Laptop GPU"*, plus
  the driver version and TF/ONNX Runtime version.

- **GPU detected but the engine can&apos;t use it** (yellow) — your card is
  visible to WSL, but the required CUDA runtime libraries (typically
  `libcublas`, `libcufft`, `libcudnn`, `libcusparse`) aren't loadable. This is
  the case Vibechek now **self-heals automatically**: the next time you click
  Analyze, it checks the engine environment first and restores missing CUDA
  libraries in place, so this yellow state is usually transient — you'll see
  "Restoring GPU libraries…" in the progress overlay instead of a silent
  CPU fallback. **Enable GPU (install CUDA wheels)** here does the same repair
  on demand, without waiting for the next analyze (roughly 1-2 GB, a few
  minutes — no apt repo, no root). Set `VIBECHEK_NO_AUTOHEAL=1` to turn off the automatic
  repair-on-analyze and rely on this button instead.

- **No GPU** (grey) — either you have no NVIDIA card or the WSL kernel
  isn&apos;t passing it through. Analysis runs on CPU. On a modern multi-core
  CPU this is still fast — ~25-40 tracks/min with workers ≈ cores − 1.

The **native engine** (Windows default) is honest about a simpler fact: its
bundled ONNX Runtime ships no GPU execution provider, so this row just says
it's CPU-only, even if your host has a GPU. Switch to the ONNX or
essentia-tensorflow engine for GPU acceleration.

The probe is slow the first time (~10 sec to spin up the engine) and then
cached for 5 minutes. Click **Re-probe** to force a fresh check.

### Analysis — the worker slider

The worker slider's maximum isn't a fixed number — it's computed for your
*current engine + genre classifier* against measured resources, so it can
never offer more workers than will actually fit. On a WSL-routed engine
(essentia-tensorflow, ONNX, or the opt-in CLAP / online-lookup genre
engines) the memory it measures is the **WSL VM's RAM**, not your host's —
that's the pool the workers actually draw from, and the hint under the slider
names it ("15.5 GB measured" on "the WSL VM"). On the native engine it's your
host's RAM directly.

Per-worker cost depends on the genre classifier: the default Discogs-EffNet
model needs well under 1 GB per worker, while **CLAP audio needs ~4.5 GB per
worker** (it loads a full checkpoint into every process) — so switching to
CLAP can drop the slider's max sharply on the same machine. The hint under the
slider always shows the actual per-worker figure for whatever's selected.
GPU workers only appear in that count when the engine can genuinely register
the GPU — not just when a card is present.

If you'd saved a worker count that no longer fits (you switched to CLAP, or
moved to a smaller WSL VM), the slider shows a warning naming the number it'll
actually use instead of silently running fewer than your saved value. If
*nothing* fits — not even one worker — the run refuses outright with an
actionable message (switch classifier, or raise the WSL memory limit in
`.wslconfig`) rather than launching a worker that gets silently killed by the
OS. And if the run gets clamped below what you asked for, the progress overlay
during the run says why (see Workflow 1, Step 3).

Click **Advanced settings** to expand:

- **Tagging** — genre confidence threshold (slider); a **"Write these fields" grid** to turn each ML field on/off independently (Genre, Energy, Mood, Vocal, Timeslot, Direction, BPM, Key — BPM & Key default off because Rekordbox is usually better); a **"Vocal detection sensitivity"** dual slider (Instrumental ≤ / Vocal ≥) that re-labels tracks from their stored score without re-analyzing; preserve Rekordbox frames (always on by default — *don't turn this off unless you really know what you're doing*), write subgenre as main genre, backup before write.
- **Duplicate detection** — toggle MD5 / Chromaprint, similarity threshold, action (report / move / trash), and the **variant** controls (keep distinct Extended/Radio/Remix versions, keep one file per format, duration tolerance).
- **Organization** — use subgenres, min genre size, target root.

At the bottom:

- **Software updates** (opt-in) — check for a new version, download it, install, and
  relaunch, all from inside the app. *Note:* update artifacts are only verified when
  the project ships a signing key, and beta builds are unsigned, so this control is
  inert on beta releases until signing is enabled (it's wired up and ready for the
  stable release).
- **Restore all settings to defaults** — wipes your config back to factory.
- **About** — sidecar path, link to view logs.

All changes auto-save 500ms after you stop typing/dragging. No "Save" button.

### ONNX inference engine

**Settings → Analysis → Inference engine** picks how the ML models run:

- **Native** (default on Windows) — a WSL-free, fully in-process engine: a DSP-only
  native Essentia wheel (decode/BPM/key) + a pure-NumPy mel frontend + ONNX Runtime
  inference. Bundled into the Windows desktop installer, so a fresh Windows install
  analyzes with zero setup. CPU-only (the bundle ships CPU ONNX Runtime); switch to
  ONNX for GPU. macOS/Linux don't need it — they're already native via the ONNX
  engine. (The opt-in CLAP / online-lookup genre engines aren't supported on the
  native engine; switch to ONNX or essentia-tensorflow to use them.)
- **Essentia · TensorFlow** (default on macOS/Linux; runs in WSL on Windows) — the
  bundled, NVIDIA-only path.
- **ONNX Runtime** — runs the *same* models through ONNX Runtime on **plain
  Essentia with no TensorFlow at all**. Two draws. First, **cross-vendor GPU
  acceleration**: NVIDIA CUDA, AMD ROCm, and Apple CoreML, not just NVIDIA.
  Second, dropping the **end-of-life TensorFlow 2.5** runtime (the migration's
  real motivation). Validated to match the default engine's output
  on real tracks (genre/vocal/mood/BPM/key all match; embedding cosine 0.99942).
  CUDA is verified on an RTX 4070; ROCm and CoreML are wired but
  hardware-unverified.

Because the two Essentia builds can't share an environment, ONNX runs in its own
managed setup. To turn it on: select **ONNX Runtime**, click **Set up ONNX
engine** (a one-time install of plain Essentia + ONNX Runtime — on Windows this
is a second WSL venv, `~/.vibechek/venv-onnx`), then **re-analyze your library**
so every track is scored by the same engine. The installer auto-picks the GPU
runtime for the hardware it detects (NVIDIA → CUDA, AMD → ROCm, Apple → CoreML);
**Settings → System** then shows what the ONNX engine actually sees — its device
and provider, e.g. *"RTX 4070 · CUDA"*. Switch back any time. Most users can leave
the default; reach for ONNX if you have a non-NVIDIA GPU or want off TF.

### Genre classifier & online lookup

Genre is the hardest field to get right, so it has its own controls under **Settings →
Analysis** (BPM/key/mood are unaffected by these):

- **Genre classifier** — **Discogs-EffNet** (default, bundled) or **CLAP audio** (opt-in).
  CLAP is an audio-embedding model that's **~2× more accurate on pure audio** and,
  unlike a file tag, works on **untagged / white-label** tracks. Select it and click
  **Set up CLAP genre engine** (a one-time ~2.2 GB download), then re-analyze. CLAP
  installs into the ONNX / essentia-tensorflow engine's environment, so **the setup
  button isn't available while you're on the native engine** (Windows default) — you'll
  see an inline hint instead of the button. Switch **Settings → Analysis → Inference
  engine** to ONNX or Essentia · TensorFlow first, then come back and set up CLAP.
- **Online genre lookup** (toggle) — searches for each track's artist + title, opens the
  store pages the search returns, and reads the genre straight off the page. It only keeps
  a genre when that page names this exact track, and it refuses shop categories like
  "Dance/Pop" that aren't really genres. The result is layered in as **your tag › verified
  web › audio** — the most accurate option on tagged libraries (73% exact on our test
  library, against 52% for tags and audio alone). No AI model and no account: click
  **Set up online lookup** once. It adds a few seconds per track and is **off by default**.
- **Genre source** — how an *existing* tag is reconciled with the model read:
  **Prefer existing tag** (default — trust a specific Beatport-style tag, use the model to
  fill gaps + override only when very confident, ignore generic junk), **Prefer ML**,
  **Tag only**, or **ML only** (pure audio).

All of this feeds one reconciliation, so the defaults are unchanged unless you opt in — and
you can mix them (trust tags, fall back to CLAP, escalate to web only when unsure).

Whichever policy you pick, when the sources disagree the track is flagged for **review** on
the Library tab (the "N to review" filter + a per-row ⚠ marker), and Track Details shows
which source won and why. So a policy like **Prefer ML** that lets the model overrule a tag
never does it silently — you can see, and reverse, every change. (See Workflow 2.)

### Importing your Rekordbox tags (priors)

Rekordbox keeps the genre and comment edits you make in its **own database** — they never
reach your audio files, so no file-tag reader can see them. Export your collection from
Rekordbox (**File → Export Collection in xml format**), then use **Import Rekordbox XML**
on the Library tab: the genre you curated there joins the reconciliation at the same
trusted "your tag" tier as a file tag (Track Details labels it "Your tag (Rekordbox)").
The import is remembered for that library and re-applied on every future analyze. Nothing
is ever written to your files by an import.

Keys and Mixed In Key energy come along too, but deliberately as **context, not
overrides**: measured on real libraries, embedded key tags (from Rekordbox/Traktor/MIK's
own analyzers) are simply *less accurate* than Vibechek's audio read — so a disagreeing
tag key shows up in Track Details as a flag ("tag says 9B") instead of silently replacing
the better value, and agreement shows as a quiet confirmation. MIK's "Energy N" (1-10)
displays alongside Vibechek's energy without being mixed into it.

**If your library moves** (new drive, reorganized folders), the import is keyed to the
absolute paths in the XML you exported, so it stops matching anything. Rather than the
import silently going quiet, a full analyze run's completion toast tells you your
imported priors ("N tracks") no longer match any track in this library and to
re-import the Rekordbox XML. The same warning appears if the priors sidecar itself is
unreadable or corrupted. (This check only runs on a full analyze — an incremental
"Analyze new" run legitimately may not contain any track from an older export, so it
doesn't trigger a false warning.)

---

## Troubleshooting

### Analyze fails with "essentia not installed" on Windows

A fresh desktop install shouldn't see this — it uses the bundled native engine with
no setup. It appears on a fallback path (the CLI zip, or after switching to the
essentia-tensorflow / ONNX engine, or the CLAP / online-lookup genre engines).
The Preflight dialog should be handling it for you. If you closed it, click Analyze
again — it'll re-open. The dialog will walk you through:
1. Installing WSL Ubuntu (one-time, ~5-15 min, UAC prompt)
2. Installing Vibechek + Essentia inside WSL (3-5 min, no prompts)
3. Downloading the ML models (~800 MB, a couple of minutes)

Once everything is green, the analyze proceeds automatically. If the message you're
actually seeing names a *different* real error (Vibechek now shows the underlying
loader exception instead of a blanket "not installed" when the package is present but
broken), follow that message instead — it's telling you the truth about what failed.

### The WSL environment looks broken / GPU stopped working

You shouldn't need to manually reinstall or repair the WSL venv anymore. Before every
WSL-routed analyze, Vibechek checks that the environment actually imports its ML stack
and repairs it in place if not — including restoring CUDA libraries a WSL reinstall
wiped out. Just click **Analyze** again; if a repair is needed you'll see it announce
itself in the progress overlay ("Restoring GPU libraries…") and in the completion
toast ("engine environment repaired automatically"). The manual **Install in Ubuntu** /
**Enable GPU** buttons in Settings still exist as accelerators if you'd rather fix it
before an analyze run, or if you've set `VIBECHEK_NO_AUTOHEAL=1` to disable the
automatic repair.

If something still looks wrong, run `vibechek doctor` (or **Settings → About → View
logs** for the raw log) — it reports engine-aware readiness for whichever engine
you've actually selected (not just the default) and a "last analyze run" section
showing your most recent run's worker count, GPU decision and reason, and any
warnings, straight from `logs/run_history.jsonl`.

### Sidecar died / "sidecar is no longer running"

The Python process behind the GUI crashed. Restart the app. If it keeps happening, click **View logs** in the error banner (or Settings → About → View logs) and check the last few lines for the actual exception.

### Operation hangs

Click **Cancel** in the progress overlay at the bottom. The op gets a clean shutdown (sub-processes terminated, no half-written files in the middle of a tag operation).

### "fpcalc not found" during dedupe

Only affects audio-fingerprint dedup (Chromaprint) — the results screen shows a
"Fingerprint scan skipped" banner when this happens. MD5 dedup still works. To enable
Chromaprint:

- Linux: `sudo apt install libchromaprint-tools`
- macOS: `brew install chromaprint`
- Windows: download from <https://acoustid.org/chromaprint> and put `fpcalc.exe` somewhere on your PATH

Or just leave it — MD5 catches the bulk of duplicates anyway.

### Models download is slow / fails

`essentia.upf.edu` occasionally rate-limits. Hit "Download models now" again — it picks up where it left off (each model is a separate file). A failure now names the real cause where it can tell (corrupted/checksum-mismatched download vs. out of disk space vs. a network problem), instead of a generic network hint for everything.

### Settings reset themselves

If a saved setting is invalid for your platform or got corrupted, Vibechek now shows a
one-time "some saved settings were invalid and were reset" toast naming which ones —
so a reverted value never quietly renders as if it were your choice. For anything not
covered by that toast, look for write errors in the logs (Settings → View logs → filter by ERROR). The most common cause is the config folder being read-only or full. The config lives at:
- Windows: `%APPDATA%\Vibechek\Vibechek\config.json`
- macOS: `~/Library/Application Support/Vibechek/Vibechek/config.json`
- Linux: `~/.config/Vibechek/Vibechek/config.json`

### "Something went wrong" toast keeps appearing

Click **Copy details** to grab the full error, **View logs** to see what the sidecar was doing, or **Report on GitHub** to open a pre-filled issue with your platform + sidecar info. `vibechek doctor` (see above) is worth running first — it bundles most of what a bug report needs into one paste-able block.

---

## Keyboard shortcuts

- **Esc** — close the track detail panel (or any modal)
- **Ctrl/Cmd + R** — hard-reload the window (useful if the UI gets confused)

---

## Where your stuff lives

| What | Location |
|---|---|
| **Config** (auto-saved) | `<config_dir>/Vibechek/config.json` |
| **Recent libraries index** | `<config_dir>/Vibechek/library_state.json` |
| **Tag backup history** | `<config_dir>/Vibechek/backup_history.json` |
| **ML models** | `<data_dir>/Vibechek/models/` |
| **Auto-saved analysis JSONs** | `<data_dir>/Vibechek/analyses/` |
| **Logs** | `<data_dir>/Vibechek/logs/vibechek.log` (rotating, 10 MB × 5) |

To start fresh, delete the `Vibechek` folders in both your config and data dirs.

---

## Power-user tip: the CLI

The desktop app's Python backend is also a CLI. Run `vibechek --help` to see what's there. Useful for:

- Running an analyze on a remote / headless machine
- Scripting bulk operations
- Quick one-off checks (e.g., `vibechek system-info`)
- `vibechek doctor` — a paste-able diagnostic report (engine readiness, models,
  WSL/venv status, your last analyze run's worker/GPU decisions, recent log lines);
  `vibechek doctor --output report.md` writes it to a file
- `vibechek preflight --engine onnx` — check readiness for a specific engine instead
  of whatever's saved in your config

The CLI is the same code the GUI uses; anything the GUI can do, the CLI can do.

---

## What Vibechek won't do

- **Cloud sync** — everything stays on your machine. No accounts, no telemetry, no upload.
- **Music recommendation** — Vibechek organizes; it doesn't suggest what to play.
- **Format conversion** — Vibechek won't re-encode your FLACs into MP3s.
- **Modify cue points or beat grids** — those binary frames are preserved across every tag write.
- **Rekordbox / Serato / Traktor XML export** — for now. Standard tags + Rekordbox-preserved binary frames mean Rekordbox already sees what Vibechek wrote.

---

## I want to help / report a bug / request a feature

- **Bugs**: [github.com/captinjack99/Vibechek/issues](https://github.com/captinjack99/Vibechek/issues). The error toast has a "Report on GitHub" button that pre-fills the issue with your platform + sidecar info.
- **Features**: same place. Open an issue describing the use case.
- **Code**: see [CONTRIBUTING](../README.md#contributing) in the main README.
- **Money**: [GitHub Sponsors](https://github.com/sponsors/captinjack99) if Vibechek saved you a license fee. Entirely optional — starring the repo helps too.

Thanks for using Vibechek. Hope it saves you a weekend.
