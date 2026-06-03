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

1. Download the installer from [Releases](https://github.com/papapew/Vibechek/releases).
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

### Step 2 — (Windows only) Auto-setup if needed

The first time you click **Analyze with ML** on Windows, a dialog appears showing what's missing:

- **WSL not installed?** → click "Install WSL + Ubuntu". You'll get a UAC prompt (this is Windows enabling a system feature). 5-15 minutes.
- **Vibechek + Essentia not in WSL?** → click "Install in Ubuntu". 3-5 minutes. No prompts.
- **Models not downloaded?** → click "Download models now". ~800 MB, takes a couple of minutes on a fast connection.

When everything's green, the dialog closes itself and analysis starts.

### Step 3 — Wait

A progress overlay at the bottom shows current track / total + elapsed time. You can:
- Click around other tabs — the analyze keeps running in the background.
- Hit **Cancel** to stop. Partial results aren't kept; restart from scratch when you're ready.

When done, the track list populates. Each row shows genre, BPM, key, energy bar.

### Step 4 — Click a track to see details

Right-side panel slides in:
- **Audio waveform + play button** — preview a clip
- **File metadata** — path, size, filename-derived hints
- **Tag diff** — current tags vs ML results, with an arrow on anything that'd change
- **Confidence indicator** — green check if above threshold, yellow warning if below

You can hit **Apply ML tags to this file** to commit just this one. More commonly, you'll do bulk tagging next.

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

Then select all visible tracks (header checkbox) and apply.

---

## Workflow 3: Find and remove duplicates

Duplicates tab. Pick your library folder. Click **Scan**.

Two passes run:

1. **MD5 hash** — catches byte-identical files (the same MP3 saved twice).
2. **Chromaprint fingerprint** — catches re-encoded copies (same song as FLAC and MP3, or 320 kbps and 192 kbps).

(If you don't have `fpcalc` installed, only MD5 runs. The app says so.)

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

- **Ready to analyze?** banner — green if Essentia + models are ready, otherwise tells you what's missing.
- **System** — CPU cores, RAM, GPU. The GPU row shows what the actual analyze
  engine sees, not just what your host has — see the next section.
- **Analysis** — workers slider (snaps to recommended = cores − 1), GPU auto/on/off, models directory + download button.

### System — what the GPU row really means

Vibechek goes out of its way to never lie to you about GPU acceleration. The
GPU row in **Settings → System** asks the *actual analyze engine* (Essentia's
bundled TensorFlow, running inside WSL on Windows) what it can see. It is
NOT a host-side `nvidia-smi` check. This means the value reflects what
analysis will really use.

Three possible states:

- **GPU available** (green) — TF registered the GPU. Analyze will use it.
  Shows your card name, e.g. *"NVIDIA GeForce RTX 4070 Laptop GPU"*, plus
  the driver version and TF version.

- **GPU detected but TensorFlow can&apos;t use it** (yellow) — your card is
  visible to WSL, but Essentia's TF couldn&apos;t load the CUDA runtime
  libraries it needs (typically `libcublas`, `libcufft`, `libcudnn`,
  `libcusparse`). Analysis would silently fall back to CPU. Click
  **Enable GPU (install CUDA libs)** to install them as NVIDIA&apos;s
  PyPI wheels into the managed venv (~200 MB, ~30 sec — no apt repo, no root).

- **No GPU** (grey) — either you have no NVIDIA card or the WSL kernel
  isn&apos;t passing it through. Analysis runs on CPU. On a modern multi-core
  CPU this is still fast — ~25-40 tracks/min with workers ≈ cores − 1.

The probe is slow the first time (~10 sec to spin up TF inside WSL) and then
cached for 5 minutes. Click **Re-probe** to force a fresh check.

Click **Advanced settings** to expand:

- **Tagging** — genre confidence threshold (slider); a **"Write these fields" grid** to turn each ML field on/off independently (Genre, Energy, Mood, Vocal, Timeslot, Direction, BPM, Key — BPM & Key default off because Rekordbox is usually better); a **"Vocal detection sensitivity"** dual slider (Instrumental ≤ / Vocal ≥) that re-labels tracks from their stored score without re-analyzing; preserve Rekordbox frames (always on by default — *don't turn this off unless you really know what you're doing*), write subgenre as main genre, backup before write.
- **Duplicate detection** — toggle MD5 / Chromaprint, similarity threshold, action (report / move / trash).
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

- **Essentia · TensorFlow** (default) — the bundled, NVIDIA-only path.
- **ONNX Runtime** — runs the *same* models through ONNX Runtime on **plain
  Essentia with no TensorFlow at all**. Two draws: **cross-vendor GPU
  acceleration** — NVIDIA **CUDA**, AMD **ROCm**, and Apple **CoreML**, not just
  NVIDIA — and dropping the **end-of-life TensorFlow 2.5** runtime (the
  migration's real motivation). Validated to match the default engine's output
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

---

## Troubleshooting

### Analyze fails with "essentia not installed" on Windows

The Preflight dialog should be handling this for you. If you closed it, click Analyze again — it'll re-open. The dialog will walk you through:
1. Installing WSL Ubuntu (one-time, ~5-15 min, UAC prompt)
2. Installing Vibechek + Essentia inside WSL (3-5 min, no prompts)
3. Downloading the ML models (~800 MB, a couple of minutes)

Once everything is green, the analyze proceeds automatically.

### Sidecar died / "sidecar is no longer running"

The Python process behind the GUI crashed. Restart the app. If it keeps happening, click **View logs** in the error banner (or Settings → About → View logs) and check the last few lines for the actual exception.

### Operation hangs

Click **Cancel** in the progress overlay at the bottom. The op gets a clean shutdown (sub-processes terminated, no half-written files in the middle of a tag operation).

### "fpcalc not found" during dedupe

Only affects audio-fingerprint dedup (Chromaprint). MD5 dedup still works. To enable Chromaprint:

- Linux: `sudo apt install libchromaprint-tools`
- macOS: `brew install chromaprint`
- Windows: download from <https://acoustid.org/chromaprint> and put `fpcalc.exe` somewhere on your PATH

Or just leave it — MD5 catches the bulk of duplicates anyway.

### Models download is slow / fails

`essentia.upf.edu` occasionally rate-limits. Hit "Download models now" again — it picks up where it left off (each model is a separate file).

### Settings reset themselves

Look for write errors in the logs (Settings → View logs → filter by ERROR). The most common cause is the config folder being read-only or full. The config lives at:
- Windows: `%APPDATA%\Vibechek\Vibechek\config.json`
- macOS: `~/Library/Application Support/Vibechek/Vibechek/config.json`
- Linux: `~/.config/Vibechek/Vibechek/config.json`

### "Something went wrong" toast keeps appearing

Click **Copy details** to grab the full error, **View logs** to see what the sidecar was doing, or **Report on GitHub** to open a pre-filled issue with your platform + sidecar info.

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

- **Bugs**: [github.com/papapew/Vibechek/issues](https://github.com/papapew/Vibechek/issues). The error toast has a "Report on GitHub" button that pre-fills the issue with your platform + sidecar info.
- **Features**: same place. Open an issue describing the use case.
- **Code**: see [CONTRIBUTING](../README.md#contributing) in the main README.
- **Money**: [GitHub Sponsors](https://github.com/sponsors/papapew) if Vibechek saved you a license fee. Entirely optional — starring the repo helps too.

Thanks for using Vibechek. Hope it saves you a weekend.
