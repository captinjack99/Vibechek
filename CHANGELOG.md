# Changelog

All notable changes to Vibechek are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Pre-release tags use the form `vMAJOR.MINOR.PATCH-beta` (git tag) which maps to `MAJOR.MINOR.PATCHb0` in PEP 440 (`pyproject.toml`). Pre-1.0 follows standard SemVer: bump PATCH for backwards-compatible fixes, MINOR for features or breaking changes. See [docs/RELEASING.md](docs/RELEASING.md).

---

## [Unreleased]

### Added
- **Organize can clear up the folders it empties — if you say so.** Re-filing a
  sorted library moves the last track out of a genre folder and leaves the empty
  folder behind. Organize now tells you how many folders that was and lists
  them, with a button to remove them. Nothing is removed as part of the organize
  itself: you see the list first, and confirm.

  It only ever removes folders, never a file. A folder that turns out not to be
  empty after all is left alone and reported with the reason, including one
  holding nothing but a hidden `.DS_Store` or `desktop.ini` — clearing that would
  mean deleting a file. A genre folder left holding only empty subgenre folders
  is cleared in the same pass. Nothing outside the library is touched. Undo still
  works afterwards: putting the files back recreates the folders.

### Fixed
- **Re-organizing a library you've already sorted now works.** If your library
  is already in genre folders and you re-analyze it, the tracks whose genre came
  back corrected can now be moved into the right folder — which is what Organize
  looked like it should already do. Two things prevented it. Naming your library
  as the target root was rejected outright ("pick a different folder"), even
  though organizing a library into itself is exactly what re-filing means. And
  leaving the target blank was worse than useless: Vibechek guessed the
  destination from the first track's folder, so for a sorted library it picked
  *a genre folder* and planned to move your whole library inside it —
  `House/track.mp3` to `Techno/House/track.mp3`.

  Organizing into your library is now allowed, with a note explaining what it
  does, and leaving the target blank uses the library you have loaded. Only
  tracks whose genre actually changed move; everything already in the right
  place stays put and isn't touched. As always you see the full plan in Preview
  before anything moves, and the run is undoable.
- **Sorting straight from tags spells genres the same way analysis does.**
  `vibechek route`, which files staged tracks by their existing genre tag
  without analyzing them, read the tag literally — so it made a `Hip-Hop` folder
  while analyzing the same track made `Hip Hop`, and a library fed by both ended
  up with two folders for one genre. It now applies the same spelling rules
  analysis uses. It also stops letting a tag that names no genre ("Dance",
  "EDM", "Unknown") create a top-level folder — those go to `Other/<tag>`, which
  is where a 12,000-track library's 878-file `Dance/` folder came from.

  It still files by *your* tags: an unusual or obscure genre tag keeps its own
  folder, and tags Vibechek distrusts when analyzing — "Electro", or a record
  pool's playlist name — are still taken at their word here, because overriding
  what your tag says is the one thing this command shouldn't do.
- **"Hip-Hop" and "Hip Hop" are one genre again.** The genre list matched tag
  spellings exactly, so a tag naming a genre it already knows under a different
  spelling was treated as an unknown genre: it became the track's label *and* its
  own Organize folder, so one genre ended up split across two destinations that
  couldn't be matched, filtered or grouped together. Vibechek now recognises the
  common variants — `Hip-Hop`, `D&B`, `Synth Pop`, `Rock & Roll` — and the store
  spellings that come with Beatport purchases, both the slash-joined names
  (`Nu Disco / Disco`, `Organic House / Downtempo`, `Indie Dance / Nu Disco`,
  `UK Garage / Bassline`) and the bracketed ones (`Techno (Peak Time / Driving)`,
  `Trance (Main Floor)`, `Trance (Raw / Deep / Hypnotic)`), which are read as the
  genre they qualify. `Électronique` joins `Electronic` as a bucket that names no
  genre. Across a 12,000-track library that is 287 files whose analyzed genre was
  previously a one-off label of its own. (Sorting *un-analyzed* files straight
  from their tag still uses the tag exactly as written — that path deliberately
  doesn't consult the genre list at all.)

  Each mapping was checked against the genre those files independently resolve
  to, because an alias into the wrong family would move Organize's destination
  and change what shows up for review. That check earned its keep twice:
  `Minimal / Deep Tech` reads like plain "Minimal", which Vibechek files under
  House, but 85% of those tracks are techno — so it maps to Minimal Techno, not
  Minimal. And `Breaks / Breakbeat / UK Bass` looked like exactly the same kind
  of easy win and was **dropped**: none of its tracks are actually Breaks.
  Accuracy on the 86-track test corpus is unchanged to the digit, which is the
  point — this fixes labels and folders, not the genre read itself. Genres that
  Vibechek's list simply doesn't have (Reggaeton, Soundtrack, Acid Jazz, Bossa
  Nova, Salsa) are untouched and still keep their own label. Existing analyses
  keep their current genre until re-analyzed.

## [0.9.0-beta] — 2026-07-25

### Changed
- **Online genre lookup now reads the store page instead of asking an AI model
  to.** The optional online lookup used to hand web-search snippets to a local
  language model and ask it for the subgenre. It now does the obvious thing
  directly: search for the artist + title, open the catalog pages the search
  returns, and read the genre out of the page's own genre field — keeping it
  only when that page names this exact track, quoting the field verbatim off the
  bytes we fetched, and refusing shop categories like "Dance/Pop" that aren't
  really genres. Measured on an 86-track adjudicated corpus: **73% exact / 87%
  family**, against 59%/73% for the model version and 52%/71% for tags and audio
  alone — and roughly 5× faster, because nothing loads a model. Twelve tracks
  fixed, none broken, versus the shipping default.

  What this means for you:
  - **Setup is now two small packages** instead of a 4.7 GB model download. If
    you set the lookup up before this release, run **Set up online lookup** once
    more; until you do, runs will say the lookup wasn't available rather than
    silently skipping it.
  - **"Verified" in the Genre sources panel is a stronger claim than it was.**
    It now means the genre was quoted off a page naming this exact artist and
    title, not that a model said it had a source. A single catalog we couldn't
    corroborate is shown as "single source" and can fill an empty or generic
    tag, but never overrides a specific one.
  - **A verified lookup can now refine a tag within its own family** — Tech
    House → Funky House, Trance → Psytrance — where before it had to disagree at
    the family level to be allowed to. That guard existed because "verified" used
    to be a model's word; against the direct read it only cost accuracy (6 tracks
    on the test corpus went from roughly-right to exactly right, none went the
    other way). Any replaced tag is still flagged for review — nothing changes
    behind your back.
  - Existing analyses are untouched until you re-analyze.
  - The `--genre-llm-backend` option and its config field are still accepted so
    old settings load cleanly, but they no longer do anything.

### Fixed
- **A playlist name in the genre field no longer becomes a genre — or a folder.**
  Record pools and download sites often write their own playlist name into the
  genre tag ("Hypeddit Top Weekly Picks"). Vibechek trusted that as a curated
  genre, so the track kept it *and* Organize created a top-level folder named
  after the playlist. A genre tag of four or more words that names nothing in the
  genre list is now treated as no tag at all, and the online lookup or the audio
  model answers instead.

  The rule is deliberately narrow, because the obvious wider versions measured
  worse: distrusting every tag the genre list doesn't recognize threw away real
  but obscure genres, and distrusting comma-separated tags threw away lists whose
  first genre was right. Only the long-phrase rule left accuracy untouched on the
  86-track test corpus, and across a 12,000-track library it fires on 187 files
  and nothing else. Short pool labels like "TMU" or "White Label" are *not*
  covered — nothing distinguishes them from a genuine niche genre by shape alone.
  Existing analyses keep their current genre until re-analyzed.
- **An "Electro" genre tag no longer outranks what the track actually is.**
  Vibechek trusts a specific genre tag over its own read, because a specific tag
  is usually somebody's deliberate choice. "Electro" is the exception: taggers
  and record pools spray it across whole releases, so it says almost nothing
  about the track. It's now treated the way "Dance" and "EDM" already were — as
  no tag at all, leaving the online lookup or the audio model to answer. Across
  a 12,000-track library the tag sits on 299 files and only 14 of those are
  actually filed under Electro; on the 86-track test corpus none of the
  Electro-tagged tracks were Electro, and dropping the tag fixed one outright
  and broke nothing (72% → 73% exact, 85% → 86% family with the online lookup
  on; 52% → 54% exact without it).

  "Electro" remains a perfectly good *answer* — if the online lookup reads
  Electro off a catalog page, or the audio model hears it, that stands. Only the
  inherited tag is distrusted. **"House" was considered for the same treatment
  and rejected**: it's the most common tag in a real library and it agrees with
  how those tracks are actually filed, and distrusting it measured worse.
  Existing analyses keep their current genre until re-analyzed.
- **Vocal detection finally hears chopped vocal hooks.** The vocal label came
  from the track-wide average of the model's per-segment voice score, so a
  club track whose vocal lives in the hook — most of dance music — averaged
  out to "Instrumental" (measured: half of a 51-track feat-credited set was
  mislabeled). Analysis now also looks at the shape of the vocal presence:
  strong vocal segments confined to a hook read as Vocal, while sustained
  voice-like synth leads (the reason the old thresholds were so conservative)
  still read as Instrumental. Calibrated against human-labeled tracks;
  existing analyses keep their old behavior until re-analyzed.
- **Organizing a few new tracks no longer shoves them into Other/.** The
  "rare genre" decision counted only the tracks being organized, so an
  incremental organize couldn't see that your library already has an
  established folder for that genre. It now counts the destination's existing
  genre folders too — 3 new House tracks join the House/ folder they belong
  in, and Other/ is reserved for genres that are genuinely rare across both.
- **The bundled model weights now ship with the license notice they always
  required.** The MTG-UPF model weights (Discogs-EffNet and every
  classification head) are CC BY-NC-SA 4.0 — not AGPL like Vibechek's own
  code — and were being redistributed in the repo and installers without the
  attribution and license notice that license requires. `THIRD_PARTY_MODELS.md`
  now carries the full inventory (including the ONNX conversions as adaptations,
  the CC0 CLAP checkpoint, Apache-2.0 Qwen, and LGPL fpcalc), and the README
  points to it.

## [0.8.2-beta] — 2026-07-21

### Fixed
- **`vibechek doctor` tells the truth about engine readiness.** Its readiness
  section used a quick probe that can't see inside the Linux analysis
  environment, so a perfectly healthy setup was reported as "not set up yet".
  It now runs the same full check the app itself uses. The CLI's analyze also
  joins the durable run history (doctor's "last analyze run" section was blind
  to command-line runs), and the CLI's engine help drops the same stale
  "cross-vendor GPU" claim the app lost in this release.
- **GPU claims are engine-aware and factually current.** The Settings
  "Why is my AMD/Intel GPU not used?" explainer still described the pre-0.6
  world where everything ran through essentia-tensorflow; it now tells the
  truth for the engine you actually have selected (TensorFlow = NVIDIA-only,
  ONNX = NVIDIA today with DirectML/CoreML planned, Native = CPU today for
  every card). The per-device "accelerated" badge follows the same rule — an
  NVIDIA card under the CPU-only native engine no longer wears a badge it
  hasn't earned — and refreshes when you switch engines. The engine picker
  stops calling essentia_tf "the default" (native is, on Windows), stops
  claiming present-tense cross-vendor GPU, and drops the stale "Experimental"
  label from the shipped Windows-default native engine.

### Changed
- **Dependency refresh — the entire dependabot queue (11 PRs) handled.**
  React 19, Tailwind CSS 4 (CSS-first theme, vite plugin, pixel-preserving
  class migration), typescript-eslint 8 with a newly wired-up, actually
  functioning lint (it had no config since inception — now 0 errors with
  real signal), lucide-react 1.x (all 56 icons verified), tauri 2.11.3 /
  tauri-build 2.6.3 / anyhow 1.0.104, postcss 8.5.20, and the GitHub Actions
  majors (checkout v7, upload-artifact v7, download-artifact v8) with every
  call site audited. One real migration regression was caught in a live
  style audit and fixed: Tailwind 4's layer semantics stacked a second
  focus outline onto buttons; keyboard focus once again shows the single
  ring it always did.

## [0.8.1-beta] — 2026-07-20

### Added
- **Dedupe sets itself up.** The audio-fingerprint tool (fpcalc) now installs
  itself the first time dedupe needs it — checked on PATH, then staged in the
  app's data folder, then downloaded from the pinned official Chromaprint
  release with its checksum verified. The old dead-end banner ("install
  fpcalc" with nowhere to click) is gone; if provisioning genuinely fails,
  dedupe still runs on exact + tag matching and says exactly what was skipped
  and why.
- **Errors now come with a way out.** Analysis-service errors carry a
  structured plain-language envelope end to end (Python → Rust → GUI):
  retryable failures show a "Try again" button that re-issues the exact
  request, a dead analysis service offers "Restart Vibechek", a memory
  refusal offers "Switch to the standard genre model" and "Give Vibechek
  more memory" (a new `increase_wsl_memory` RPC raises the WSL memory limit
  in `.wslconfig` — honestly telling you a restart is needed, never forcing
  one), and a missing Linux environment offers "Install WSL" right in the
  toast. If Vibechek's analysis service can't start at all, you now get a
  real error dialog instead of a silently broken window.
- **The managed engine environment heals itself too.** Linux/macOS managed
  installs get the same detect→repair→run pre-flight the WSL path got in
  0.8.0: a venv that stopped importing its ML stack is reinstalled in place
  before the run, with the repair reported in the completion summary.

### Fixed
- **The progress overlay stays put.** Pinned to the bottom-right corner with
  a fixed width: Cancel no longer wanders as text changes, and the bar no
  longer runs off the screen edge.
- **Partial failures are honest everywhere.** Dedupe, tag-apply, backup
  restore, organize, and undo now report exact counts with an expandable
  per-file list when some files fail — no more "Done!" over quiet losses.
- **The whole app speaks DJ, not engineer.** Every user-reaching message was
  reworded to a plain headline that says what happened and what happens next;
  the technical details moved into expandable detail sections and the log
  (demoted, never deleted). One vocabulary throughout: "analysis engine",
  "analysis service", "the Linux analysis environment (WSL)". GUI errors no
  longer recommend terminal commands; the false "switch to ONNX for GPU
  support" claim on the native engine now tells the truth (planned, CPU
  today); Rekordbox-import errors explain how to export the XML instead of
  reciting parser internals.
- **The progress overlay tells you what's actually running.** Installing WSL,
  a Linux distribution, or the analysis engine from the setup dialog used to
  show "Downloading ML models" the whole time; each step now shows its own
  label ("Installing WSL", "Setting up the analysis engine").
- **"Couldn't read the saved analysis" now has a Try again.** When a library's
  saved analysis is momentarily locked (antivirus, cloud sync), the error
  offers a Try again button that genuinely re-loads the library — not just a
  message telling you to retry by hand. Permanent failures (missing or
  corrupt files) stay honestly non-retryable.

## [0.8.0-beta] — 2026-07-11

### Added
- **The worker slider now tells the truth — and so does the run.** A single
  worker-budget model (new `worker_budget` RPC) computes the real maximum for
  your engine + genre model against MEASURED resources: the WSL VM's memory
  (not the host's) for WSL-routed engines, GPU workers only when the engine
  can actually register the GPU, per-worker cost for the selected classifier
  (CLAP ≈ 4.5 GB vs 0.8 GB). The Settings slider binds its max to that
  computation and explains it; a mid-run clamp streams its reason to the GUI
  ("Workers capped 16→2: CLAP needs ~4.4 GB each; the WSL VM has 15.5 GB")
  instead of silently running fewer workers than you asked for.
- **The engine environment heals itself.** Before dispatching an analyze, the
  app verifies the WSL venv actually imports its ML stack and repairs it in
  place when broken — including automatically restoring GPU libraries wiped
  by a WSL reinstall ("Restoring GPU libraries…" with live progress). No
  manual setup step is the only path anymore; repairs announce themselves in
  the completion summary. Opt out with VIBECHEK_NO_AUTOHEAL.
- **A durable run history.** Every analyze appends engine, workers requested
  vs used, the GPU decision and its reason, counts, and warnings to
  `logs/run_history.jsonl`; `vibechek doctor` gained an engine-aware
  readiness section plus a "last analyze run" section that reads it.

### Fixed
- **Two silent data-loss paths.** An analyze whose auto-save failed (disk
  full, cloud-sync/antivirus lock) looked successful and vanished on next
  launch — the failure now surfaces as a persistent warning. Worse:
  "Analyze new tracks only" treated a momentarily-unreadable saved analysis
  as "never analyzed" and overwrote the whole library's results with just
  the new tracks — it now aborts loudly, leaving your data untouched.
- **GPU honesty across all engines.** The hybrid pool reported "N GPU
  workers" sized from nvidia-smi VRAM even when TensorFlow/ONNX could not
  register the device (they all ran on CPU); GPU workers are now gated on a
  real registration probe and the progress line states what actually ran. A
  near-zero-VRAM reading no longer creates a doomed 1-worker GPU pool that
  aborted the entire run. The native engine's Settings row now says honestly
  that the bundled ONNX Runtime is CPU-only. `onnxruntime-gpu` is pinned to
  the CUDA-12 line (an unpinned install resolved to a CUDA-13 build that
  crashed the ONNX engine on import).
- **Silent result degradation now surfaces.** CLAP falling back to Discogs
  (globally or per-track), a mood/vocal model failing to load, and a dedupe
  fingerprint phase skipped because fpcalc is missing all stamp provenance
  and warn in the completion summary. A web-genre lookup whose search
  returned nothing no longer lets an ungrounded LLM guess masquerade as a
  verified "online source".
- **Error messages name the real cause.** "onnxruntime is not installed"
  (it was installed but broken — the real loader error now shows), "check
  your network" for disk-full/corrupted downloads, a CLAP checksum hint
  pointing at a command that never checks CLAP, and "likely out of memory"
  for every worker death (now gated on the exit code) are all fixed.
- **Settings can't contradict reality.** The readiness banner no longer
  shows a green Essentia row under a "Not ready" title (it now evaluates the
  engine you selected, with an explicit "installed but can't serve this
  engine" state); invalid saved settings that were silently reset now
  announce themselves once; the CLAP setup button is gated off the native
  engine; venv probes verify the interpreter actually runs before reporting
  READY; a settings change made just before quitting is no longer lost to
  the autosave debounce.

## [0.7.0-beta] — 2026-07-02

Two review rounds in one release: the 17 adversarially-confirmed fixes from
the takeover review, then the remaining 39 findings verified against the code
and every confirmed one fixed.

### Added
- **Import your Rekordbox library's tags as priors (trust-UX #3).** A new
  "Import Rekordbox XML" action reads a Rekordbox collection export and feeds
  the genre you curated there into the same tag-tier reconciliation your file
  tags get (labelled "Your tag (Rekordbox)" in the Genre sources panel). The
  import persists in a sidecar and re-applies on every future analyze; it
  never writes file tags. Key (Tonality) and Mixed In Key "Energy N" comments
  are imported too — as read-only context, not overrides (see below).
- **Key tags surfaced, never trusted blindly.** Tracks whose embedded key tag
  disagrees with the audio analysis now show it in Track Details. Measured on
  the gold corpus first (the measure-first rule): embedded key tags are other
  tools' algorithmic reads — 49% exact vs the audio path's 63% on the same
  tracks, and wrong 10:1 when they disagree — so the audio key stays
  effective, and the tag becomes a review signal (agreement shows as a quiet
  confirmation). Open Key notation ("2d"/"10m"), zero-padded Camelot ("01A"),
  and parenthetical forms ("8B (C major)") now all parse; dirty tag words like
  "Ambient" no longer misread as A minor.
- **Mixed In Key energy is read from your files.** "Energy N" in comments or
  the grouping field (MP3/FLAC/AIFF/WAV/M4A) surfaces as MIK energy (1-10) in
  Track Details, kept separate from Vibechek's own 0-5 energy scale.
- **Gold-corpus accuracy gate in CI.** Three openly-licensed 45s reference
  clips (CC0/CC-BY, attribution in `tests/fixtures/gold/ATTRIBUTION.md`) are
  committed with the genre/BPM/key the production pipeline is known to produce
  for them — pinned identically on the essentia_tf and native engines. The
  Windows release build (`selftest-native --gold-dir`) and the native-smoke
  full tier (rpc_smoke step 11) now analyze them for real and fail on any
  drift, so a silent result regression (wrong genre/BPM/key with no crash) can
  no longer ship. Shared checker in `vibechek/gold_gate.py`; the assertion
  tiers (exact / tolerance / floor / presence) live in the fixtures'
  `manifest.json`.

### Security
- **The CLAP checkpoint is revision- and content-pinned.** The opt-in CLAP
  genre engine's 2.2 GB PyTorch checkpoint (a pickle — executable on load)
  was fetched from a mutable third-party `main` ref with only a size check.
  It now downloads from an immutable Hugging Face revision, and its SHA256
  (verified byte-identical between the locally-validated copy and HF's LFS
  record) is enforced after download in both setups AND before every load.
- **Engine installs no longer track GitHub `main`.** The WSL/managed-venv
  vibechek install — and the version-drift auto-update that reuses it — was a
  de-facto unsigned auto-updater pulling whatever `main` HEAD was at that
  moment. It now installs this build's own release tag
  (`git+…@v<version>`), so the engine converges on exactly the sidecar's
  version and unreviewed commits can no longer ship silently to users.
- **The Ollama tarball and the ONNX backbone now have SHA256 pins**, verified
  after download (backbone also on cached reuse) — closing the last unpinned
  fetched-and-executed/loaded artifacts.
- **CI tokens are least-privilege:** release.yml build jobs now run with a
  read-only GITHUB_TOKEN (only the publish job gets `contents: write`), and
  codeql.yml's actions are pinned to commit SHAs like every other workflow.

### Fixed
- **The native engine's WSL fallback works end-to-end.** Everything that
  picks a WSL/managed venv for an engine now routes through one shared
  mapping (`native` shares the ONNX stack's `venv-onnx`). Previously,
  preflight validated venv-onnx while the install put essentia-tensorflow
  into `venv` (a 10-minute setup that still said "not ready"), the dispatch
  ran a venv that couldn't serve the engine (crash after preflight said
  READY), and the auto-update repaired a different venv than the one probed.
  On Linux/macOS, a saved `native` engine (e.g. a config copied from Windows)
  snaps back to the platform default instead of misrouting; the Settings
  button is Windows-only.
- **CLAP / web-resolver setup refuses the native engine honestly.** On the
  Windows-default native engine, the multi-GB setup used to install into a
  WSL venv the in-process analyzer can never import — reporting success while
  the feature silently never activated.
- **The sidecar can't be wedged by native print noise anymore (3 crash
  paths).** A non-UTF-8 byte on stdout/stderr, or a >200-byte line splitting
  a multibyte character, could kill the desktop shell's reader tasks: RPC
  demuxing died while the sidecar lived on (every call "timed out", analyze
  hung forever), or stderr stopped draining until the sidecar blocked on a
  full pipe. Both readers now lossy-decode raw bytes, truncation is
  char-safe — and when the shell declares the sidecar dead, it now actually
  kills the process instead of leaking it to keep mutating files after the
  UI said "aborted".
- **Startup warnings can't be lost to a fast sidecar start.** The install-path
  hang warning (My Drive/OneDrive/long paths) fired before the UI had mounted
  its listeners and was dropped; the shell now buffers startup notifications
  until the frontend collects them.
- **A Rekordbox import can't re-open your resolved review decisions.** The
  import re-reconciles only the fields it actually changed — a key/energy-only
  fill no longer recomputes the genre decision, so a track you Reverted stays
  reverted (Approved was already guarded) and the review queue stays drained.
- **Rekordbox network (NAS) libraries match.** `file://<server>/…` locations
  kept only the path and matched zero tracks; the UNC host is now preserved.
- **Undo follows the files back in the UI.** Reverting an organize/dedupe
  journal now rewrites the in-memory track paths (as organize does forward),
  so post-undo Preview, tag apply, and re-organize stop targeting paths that
  no longer exist.
- **Cross-library race guards.** Switching libraries while a Rekordbox
  import, batch Approve/Revert, or the pre-analyze preflight probe was in
  flight could merge one library's tracks (or a whole stale report) into the
  other library's view; all three now detect the switch and drop the stale
  commit. The empty state's "Choose a different folder" is disabled during an
  active run like its siblings.
- **`vibechek analyze` / `download-models` without `--engine` now use the
  saved config's engine** (falling back to the platform default) instead of a
  hardcoded essentia_tf — a stock Windows box no longer routes into a WSL
  that was never set up.
- **"Check for updates" no longer pretends.** Auto-update is deliberately
  disabled until release signing is funded, but Settings shipped a live
  button that failed on every click; it now says so and links to GitHub
  Releases instead.
- **Incremental "Analyze new tracks only" no longer destroys the saved
  analysis.** The new-tracks-only report used to be persisted as the WHOLE
  library's analysis — one click replaced a 12k-track analysis on disk with
  just the new tracks, and the UI dropped the rest of the library from view.
  The previously analyzed records are now re-attached (with user-resolved
  review decisions preserved) before saving, deleted files still drop out,
  and the skip list is forwarded to the WSL/managed-venv routes so
  incremental runs stop silently re-analyzing everything there.
- **Imported Rekordbox priors now survive WSL/managed-venv analyzes** — the
  sidecar re-applies the priors sidecar to the subprocess report (it only
  reached the in-process engine before).
- **"Download models" now downloads the right set for the active engine.**
  The native (Windows-default) and ONNX engines' model downloads used to
  fetch the unused TensorFlow set and never stage the ONNX backbone/heads —
  the preflight remediation button could never fix a missing-models error.
  The CLI gained `--engine native` for both `analyze` and `download-models`.
- **Restore-from-backup now undoes frames a tag apply ADDED.** Restore was
  merge-only: ENERGY/MOOD/TIMESLOT/DIRECTION/VOCAL (and genre/BPM/key frames
  added to files that had none) survived a restore on every format. The
  Vibechek-managed frame set is now cleared when the snapshot lacks it —
  other frames are untouched. Backups also follow the same Unicode path
  resolution as apply (NFC/NFD-divergent files were mutated without being
  backed up) and unreadable-tag files are counted as not-fully-backed-up.
- **Model downloads are integrity-pinned.** The essentia_tf `.pb` set now has
  real SHA256 pins (verified against the canonical set) — a corrupted or
  poisoned mirror download is deleted and refused instead of silently loaded
  into TensorFlow.
- **A worker dying mid-track (OOM) no longer aborts the whole analyze.** The
  hybrid pool re-enqueues the dead worker's in-flight track (bounded retries,
  then an error record) instead of stalling 5 minutes and discarding every
  result; cancelled runs also no longer leak the input queue's feeder thread.
- **Cancel works everywhere it's offered:** the fast scan, remap restores, and
  mid-move dedupe cancels now actually stop — and a cancelled dedupe move
  returns its partial summary with the undo-journal path (matching organize).
- **"Trash duplicates" works in the packaged app** — send2trash is now a real
  dependency (it was optional, and a user can't pip-install into the frozen
  exe, so the shipped action always errored).
- **Forgetting a library removes its saved analysis + imported priors** — the
  analysis path is derived from the library path, so re-opening the folder
  used to silently resurrect months-old results and a priors import the user
  believed discarded.
- **Organize targets are validated sidecar-side before planning/moving**
  (same-as-source, writability) and the writability probe no longer leaves
  behind directories for paths the user typed and abandoned.
- **An RPC result that fails to serialize now returns a structured error**
  instead of leaving the GUI's request hanging forever with no reply.
- **Misc:** multi-valued FLAC comments no longer garble into stringified
  Python lists on CDJ-export AIFF tag copies; SECURITY.md points at GitHub
  private vulnerability reporting (the old security@ address was
  undeliverable) and the supported-versions table covers 0.6.x; test runs no
  longer write undo journals into the real user profile.
- **AIFF/WAV/M4A custom tags now read back.** Vibechek writes ENERGY/MOOD/
  TIMESLOT/DIRECTION/VOCAL (and the subgenre grouping) to AIFF/WAV ID3 chunks
  and M4A freeform atoms, but only ever read them back from MP3/FLAC — so its
  own tags on those formats vanished from the diff view on re-scan.

### CI / packaging
- **A build failure can no longer publish a release.** The release job ran
  under a stale `always()` guard, so a hard failure in any build job still
  published — v0.6.3-beta shipped with `.deb` internals (control/data.tar.gz)
  and the raw sidecar `vibechek.exe` as release assets, and a failed desktop
  build would have shipped with the installer simply missing. The release now
  requires every build to succeed, empty bundle sets fail loudly, and the
  artifact globs exclude deb internals + sidecar binaries.
- **The native engine is release-gated on Windows.** Native is the Windows
  default, but its wheel build was best-effort — a transient failure shipped
  a green release whose default engine wasn't in the installer. Tag builds
  now hard-require the bundled native engine (non-tag builds stay
  best-effort).
- **The model-mirror failover is real now.** The advertised GitHub fallback
  pointed at a release that didn't exist, with a URL layout that could never
  resolve — every UPF outage broke installs (and took the weekly smoke down).
  Mirror URLs are now built flat for release bases, the backbone is pinned,
  and the weekly smoke HEAD-checks every mirror so the failover can't
  silently rot. (The `models-v1`/`models-onnx-v1` releases carry the
  verified assets.)
- **The Rust shell is in CI.** `cargo clippy -D warnings` now runs on every
  push/PR — the Tauri shell previously compiled for the first time on tag
  push, so a Rust compile error merged green and killed every desktop build
  at release time. The dataclass→TypeScript codegen is CI-checked too
  (`generate_ts_types.py --check`), and the gold-corpus pins are now verified
  on all three engines (essentia_tf, native, and ONNX on real
  ubuntu/macos runners). PyInstaller is version-pinned on all three build
  scripts, and the native wheel dependency build fails on the first broken
  step instead of trusting a warm cache.

### Docs
- Windows setup docs (INSTALL, USER_GUIDE, README, PROJECT_SUMMARY,
  MAINTAINERS) now lead with the bundled native engine — a fresh install
  needs no WSL; the WSL walkthrough is the clearly-labelled fallback. README's
  GPU claims are qualified to what's actually validated (NVIDIA CUDA) vs
  wired-but-unverified (AMD/Apple via ONNX; the Windows native default is
  CPU). ROADMAP marks the native default + trust-UX #3 shipped. The release
  workflow's notes template, CONTRIBUTING's lint claims, and ui/README's RPC
  table (49 methods incl. `resolve_genre_conflicts` / `import_tag_priors`)
  are current.

## [0.6.3-beta] — 2026-06-24

### Changed
- **Windows now defaults to the native (WSL-free) engine.** 0.6.2-beta bundled
  and CI-validated the native engine; it's now the default on Windows, so a
  fresh install analyzes fully in-process — no WSL, no managed venv, nothing to
  set up. preflight falls back to WSL (then the managed venv) when native isn't
  importable, so a plain `pip install` or a lean build degrades gracefully
  rather than breaking. Existing installs keep whatever engine their saved
  config has — this only changes the default for a fresh/unset config.
  Linux/macOS are unchanged (default essentia_tf).
- **UI polish:** BPM and Key badges now read distinctly (BPM neutral vs the
  green Key); Settings section icons are no longer duplicated; Track Details
  shows the full existing tag on hover; loading states use skeletons.

### Fixed
- **The native build self-test now decodes audio, not just loads.**
  `selftest-native` (the release-build gate) synthesizes a clip and decodes it
  through essentia's FFmpeg/libav path + runs the feature extractors — so a
  release that defaults to native cannot ship unless the frozen exe can
  actually decode + analyze, closing the gap where a missing FFmpeg DLL passes
  a load-only smoke but fails at the first real decode.

### Docs
- README/INSTALL/USER_GUIDE/ROADMAP readability pass — removed decorative
  emoji and formulaic headings, fixed a stray HTML entity in INSTALL.

## [0.6.2-beta] — 2026-06-23

### Fixed
- **The native Windows engine now actually builds in CI.** 0.6.1-beta added the
  installer bundling + the build-time self-test, but the essentia wheel build
  itself failed on the GitHub `windows-latest` runner: it pinned the
  `Visual Studio 17 2022` CMake generator, which the runner image has moved past
  ("could not find any instance of Visual Studio"). The wheel never built, so the
  bundle was skipped and 0.6.1-beta shipped the lean CLI after all (the
  best-effort fall-back worked — it just meant native didn't ship). The wheel
  build now lets CMake auto-detect the installed Visual Studio, the same way the
  C/C++ dependency build does. Native bundling remains best-effort and
  off-by-default; flipping the default still gates on the gold-corpus parity run.

## [0.6.1-beta] — 2026-06-23

### Added
- **Native-engine bundling for the Windows installer (build machinery + self-test
  gate).** `0.6.0-beta` wired `inference_engine="native"` and exposed it in
  Settings, but the DSP-only essentia wheel was never folded into the published
  Windows installer, so the engine couldn't actually run on a stock Windows box.
  The release build now builds the cp312 essentia wheel on the Windows runner and
  `packaging/vibechek.spec` folds essentia + onnxruntime + numpy into the
  PyInstaller onefile, so native runs fully in-process — no WSL, nothing for the
  user to install. The bundle stays best-effort: a wheel-build failure degrades
  to the lean CLI and never blocks the release. (In 0.6.1-beta the wheel build
  itself failed on the CI runner — see 0.6.2-beta — so this release still shipped
  the lean CLI.)

### Changed
- **A green Windows build now *proves* the native bundle loads.** The spec
  swallows bundling errors (a missing DLL silently degrades to the lean CLI), so
  a green build never used to distinguish "native bundled and working" from
  "silently fell back". `build-windows.bat` now runs a hidden `selftest-native`
  command against the frozen `dist/vibechek.exe` whenever the wheel was bundled:
  it imports essentia inside the onefile, runs a bedrock DSP op (exercising the
  compiled extension + its delvewheel DLLs + numpy interop), checks the
  analyze-path algorithms are registered, and verifies onnxruntime + the bundled
  ONNX heads — failing the build loudly instead of shipping a broken installer.

### Fixed
- **Hardened the native wheel bundling against silent breakage.** Pin build- and
  freeze-side numpy to `<2` (matching the wheel's own declared dependency) so the
  compiled extension's ABI can't drift; install essentia's `pyyaml`/`six` runtime
  deps explicitly so PyInstaller can fold them in; pin PyInstaller to 6.x so the
  onefile staging behaviour stays reproducible; drop the compiled `_essentia*.pyd`
  from `collect_all`'s data set so it can't collide with the explicit binary
  entry; and print the bundled `.pyd` + `essentia.libs/*.dll` counts to the build
  log for auditing. (Default engine stays `essentia_tf`; flipping the default to
  native still gates on the full gold-corpus parity run.)

## [0.6.0-beta] — 2026-06-20

### Added
- **Review queue: approve or revert genre conflicts in one click.** The conflict
  surfacing that shipped earlier flagged tracks where the file tag, audio model,
  and online lookup disagreed — now you can act on the whole queue. In the
  **N to review** filter, select tracks (or select-all) and **Approve** to
  accept Vibechek's reconciled genre, or **Revert to tag** to keep the genre
  already in the file. Either clears the conflict (the track drops out of the
  queue, and the queue shows an "all caught up" state once drained) and the
  decision is persisted to the saved analysis so it survives a reload — an
  approved track records `ml_genre_source="approved"`, so a reopened track shows
  "you approved this" rather than a stale conflict warning. This **never writes
  file tags**: the existing **Apply ML tags** flow (backup-first) is still the
  only thing that touches disk. New `resolve_genre_conflicts` RPC (48 total).

### Changed
- **Vocal detection: a feat-credit prior fixes vocal tracks misread as
  Instrumental.** The voice/instrumental model means its per-frame voice
  probability over the whole track, so a vocal track with long instrumental
  intros/breaks (the norm in dance music) reads as a low mean and was labelled
  "Instrumental" — measured on the gold corpus, 4 of 5 feat-credited tracks
  (which definitely have vocals) fell below the cutoff. A "feat."/"ft." title
  credit is a near-certain vocal signal with no false-positives on
  instrumentals, so reconciliation now upgrades **only** the Instrumental→vocal
  case on feat-credited tracks. Zero regression by construction: it never
  touches non-feat tracks, so the deliberately-tuned 0.72 cutoff (which keeps
  melodic-but-instrumental dance tracks like "Children"/"Pjanoo" out of Vocal)
  is preserved. Mirrors the prefer_tag philosophy; sets `ml_vocal_source`.
- **CLAP genre classifier is more accurate (~50% → ~54% exact / 59% → 69%
  family on the gold corpus).** The bundled kNN reference was built with a
  filter that excluded "House" — the single largest genre class (407 of ~2500
  reference tracks) — plus "Electro", because the build reused the prefer_tag
  *tag-distrust* generic-set (where a bare "House" file tag is rightly
  untrusted). For a kNN *reference*, though, House/Electro are real, useful
  classes (and gold truth labels) — excluding them meant a House track could
  never be predicted House. The reference is now rebuilt keeping those classes,
  dropping only truly content-free labels and clearly non-DJ genres
  (Pop/Hip Hop/…), and without the per-class cap (distance-weighted kNN already
  favours the nearest neighbours, so capping only discarded useful ones).
  Measured through the production `clap_genre.knn_predict` on the essentia
  decode path. Default genre source is unchanged (prefer_tag); this lifts the
  opt-in CLAP audio path, which now matches the metadata default while working
  on untagged tracks.

### Internal
- **`inference_engine="native"` — a WSL-free Windows analyze path (backend wired,
  end-to-end validated).** Selecting it runs ONNX inference + the pure-NumPy mel
  frontend + a DSP-only **native essentia wheel** (decode/BPM/key) **in-process**:
  preflight already routes `analyze_via="native"` whenever essentia imports in the
  sidecar's Python, so analysis skips WSL entirely. `load_models("native")` forces
  the NumPy frontend (the DSP-only wheel ships no `TensorflowInputMusiCNN`) and
  uses the same ONNX model set as `onnx`. A DSP-only essentia wheel was built on
  Windows from the wo80 CMake fork (MSVC/VS2022) and packaged self-contained with
  delvewheel; validated in a fresh wheel-only venv (`import essentia` + DSP run
  with no DLL path) and end-to-end: a real `analyze` of Darude FLACs ran fully
  native (preflight `analyze_via=native`, no WSL, no TensorFlow) producing
  genre/BPM/key/energy/vocal. Selectable in **Settings → Engine ("Native · no
  WSL")**. **Cross-engine parity (release Python 3.12 + cp312 wheel, in-process,
  vs essentia ground truth, 40 tracks): genre top-1 class 40/40 (100%), key
  40/40 (100%), BPM 39/40 (98%)** — Windows-native results match the Mac/Linux
  path because it is the *same* ONNX model weights, a bit-exact NumPy mel
  frontend, and the same essentia DSP algorithms (compiled as a native wheel).
  **Installer bundling is wired (experimental):** `release.yml` builds the cp312
  wheel on the Windows runner and `packaging/vibechek.spec` folds essentia +
  onnxruntime + numpy into the onefile sidecar (essentia's `_essentia*.pyd` and
  its delvewheel `essentia.libs/` DLLs placed where the runtime loader expects),
  so the engine needs no separate setup. `preflight.essentia_serves_engine()`
  gates the in-process route so the bundled **DSP-only** wheel serves *only*
  `native` — the default `essentia_tf`/`onnx` paths still route through WSL
  (they need TensorFlow, which the DSP-only wheel lacks). The bundle step is
  best-effort: a wheel-build failure leaves the sidecar as the lean CLI, never
  blocking the release. Config accepts `"native"`; default stays `essentia_tf`
  pending the CI installer-build smoke + the full gold-corpus gate.
- **Groundwork for a native-Windows (WSL-free) analyze path — opt-in, default
  unchanged.** Windows routes ML analysis through WSL only because essentia has
  no Windows wheel; the ONNX engine already moved every neural forward pass to
  ONNX Runtime (Windows wheels exist), leaving four pure-DSP jobs still on
  essentia: decode, the MusiCNN mel-frontend, BPM, and key. Two of those now
  have validated native replacements:
  - `vibechek/numpy_frontend.py` — a pure-NumPy reimplementation of essentia's
    `TensorflowInputMusiCNN` log-mel (Slaney mel, unit-triangle-area filterbank,
    `log10(1+10000·x)`). Validated against essentia on 5 real tracks through the
    production `discogs-effnet-bsdynamic-1.onnx` backbone: per-frame log-mel
    **L1 = 0.0000**, embedding **cosine = 1.00000**, **genre top-1 5/5** — a
    bit-close reproduction, not an approximation. Wired into the ONNX backbone
    behind a flag (`load_onnx_models(numpy_frontend=…)` / `VIBECHEK_NUMPY_FRONTEND`),
    OFF by default until the full gold-corpus parity gate runs.
  - `vibechek/native_decode.py` — soundfile (libsndfile) + soxr decode with an
    ffmpeg fallback (new `[native]` extra). On a 25-track sample, soundfile
    decoded **24/25** natively; the one damaged MP3 falls back to ffmpeg,
    restoring essentia/MonoLoader's full codec coverage with no WSL.
  - Validated end-to-end (soundfile+soxr decode → NumPy mel → ONNX backbone) at
    embedding cosine ≥0.998 + genre top-1 match vs the essentia path. New
    `scripts/native_frontend_parity.py` harness + a CI parity test against a
    committed essentia-mel fixture. Decode/BPM/key still run on essentia (the
    default path is byte-unchanged); BPM/key remain the last DSP on essentia.
- **Split the model catalog + downloader out of `analyzer.py`** into a new
  `vibechek/model_download.py` (the god file dropped ~650 lines to ~2.5k). The
  module owns `MODELS`, the SHA256 pins, the mirror base URLs, the ONNX head
  layout, and the streaming/mirror-failover/hash-verified/cancellable
  downloader; it's deliberately import-light (essentia/numpy-free) so the CLI,
  doctor, and preflight keep importing the catalog cheaply. `analyzer.py`
  re-exports every moved name, so no call site changed — purely internal, no
  behavior change. PyInstaller picks it up via `collect_submodules`.

### Added
- **Trust UX: genre source conflicts are surfaced for one-click review.** The
  analyzer already reconciles up to three genre signals — your in-file tag, the
  pure-audio model, and the optional online lookup — into one effective value and
  records the provenance (`ml_genre_source`, `ml_genre_conflict`, plus the
  pre-reconcile audio/web reads). Those fields are now carried to the UI: the
  library shows a per-row **review marker** on tracks where the sources disagree
  and an **"N to review"** toolbar filter (composes with the chip filters;
  mutually exclusive with "errors only"), and the Track Details panel gains a
  **"Genre sources"** breakdown — your tag vs audio vs web, which one won, and a
  plain-English reason (e.g. "Changed your tag 'Tech House' → 'Trance' — the audio
  model disagreed"). Read-only: it flags disagreements for a look, it never
  silently overwrites a hand-curated tag. The vocal diff also notes when a label
  came from a "feat." credit rather than the audio model. (The eight provenance
  fields now live on the `MLResult` dataclass so the generated TS types carry
  them; they default `None`, so the raw per-track wire record is byte-unchanged
  until reconciliation.)
- **Native smoke workflow** (`native-smoke.yml`, manual + weekly). The unit-test
  matrix already covers all three OSes, but nothing ever exercised the LIVE
  sidecar on real Linux/macOS hardware. The new `scripts/rpc_smoke.py` driver
  spawns the actual `vibechek rpc` entry the desktop shell uses against a
  base (`pip install -e .`, no dev extras) install — proving the runtime
  dependency closure — and asserts ping / venv probe / scan / no-engine
  analyze behavior end-to-end on ubuntu + macos runners.
- **Native smoke FULL tier** (second `native-smoke.yml` job, ubuntu/macos ×
  essentia_tf/onnx). Past the no-engine error path, the smoke now drives the
  REAL one-click engine setup through the live sidecar — managed-venv pip
  install, model download/staging, then a real ML analyze of synthetic
  fixtures with strict assertions (2/2 track records, `ml_bpm`/`ml_key`/
  `ml_energy` populated, zero errors) plus the post-install venv probe (the
  invariant the unix site-packages glob bug had silently broken). Install
  RPCs (`install_essentia_native`, `setup_onnx_engine`) accept an optional
  `vibechek_source` local-directory override so CI installs the commit under
  test instead of GitHub main; everything runs sandboxed under a throwaway
  HOME.

- **Native (Linux/macOS) one-click setups for the opt-in genre engines.** The
  CLAP audio classifier and the online genre resolver previously had one-click
  setup on Windows/WSL only — Linux/macOS users got a "Windows-only" notice
  and a manual pip recipe. `setup_clap_engine` / `setup_genre_resolver` now
  route to native installers (`native_install.setup_clap_native` /
  `setup_resolver_native`) that mirror the WSL scripts against the same
  managed-venv and artifact paths (`~/.vibechek/venv[-onnx]`,
  `~/.vibechek/clap/music_clap.pt`, `~/ollama/bin/ollama`) — torch-CPU +
  laion-clap + the checkpoint for CLAP; ddgs + a no-sudo platform-matched
  Ollama tarball (linux amd64/arm64 `.tar.zst`, darwin `.tgz`) + the model
  pull for the resolver. The Settings buttons now show on every platform.
- **Model/checkpoint downloads cancel mid-stream.** The shared download path
  (`.pb` models, ONNX backbone, and now the 2.2 GB CLAP checkpoint) polls
  cancellation per chunk — Cancel used to let a multi-GB fetch run to
  completion behind the dialog — and a cancel no longer fails over to the
  next mirror to start the same download again.

### Fixed
- **The native (Linux/macOS) GPU engine install no longer dies at 15 minutes.**
  `install_essentia_native` auto-selects `onnxruntime-gpu` + the CUDA 12 wheel
  set on NVIDIA machines — a multi-GB download that the ML-stack step's hard
  15-minute ceiling killed mid-fetch on ordinary connections (live-reproduced:
  cudnn alone is 721 MB). The ceiling is now branch-aware: 2 h for the GPU
  stacks (matching the WSL genre setups' multi-GB precedent), 30 min for the
  CPU sets (matching the WSL essentia install). The step was already
  cancellable + progress-streaming either way.
- **Operation ids on progress events.** Long-op RPCs accept a client-generated
  `op_id`; the sidecar echoes it (plus the op `kind`) on every `progress` /
  `track_analyzed` notification, and every GUI consumer of the shared progress
  stream (global overlay, ONNX/genre setup dialogs, preflight live log, CUDA
  install banner, backup stall timer) now ignores events stamped with a
  different op's id — stragglers from a cancelled or just-finished operation
  can no longer repaint another dialog's progress. Unstamped events keep the
  old behavior, so the CLI and older sidecars are unaffected.
- **CLAP pure-audio genre classifier** (opt-in, `genre_classifier = "clap"`). A CLAP
  audio embedding is matched by kNN against a small bundled reference library
  (`vibechek/clap_assets/genre_reference.npz`, ~2 MB) — roughly **2× the genre
  accuracy** of the bundled Discogs-EffNet head on pure audio (~28% → ~54%), and
  unlike a file tag it works on untagged / white-label tracks. BPM/key/mood are
  unchanged. One-click **Set up CLAP genre engine** (Settings) installs the deps +
  downloads the ~2.2 GB checkpoint into the analysis venv. Falls back to Discogs if
  not set up. The kNN + reference loading is pure-numpy (CI-tested); the embedder is
  a lazy opt-in dependency (`vibechek[clap]`).
- **Online web-synthesis genre lookup** (opt-in, `genre_web_lookup`). A local LLM
  reads keyless web-search results for a track's artist+title and synthesizes the
  musically-specific subgenre, distrusting commercial chart buckets and verifying
  the result matches the track. Layered into reconciliation as a high-trust source
  (**tag › grounded web › audio**): a grounded web read overrides only a *stale*
  specific tag (taxonomy drift) — zero-regression. One-click **Set up online
  resolver** installs ddgs + a local Ollama (`vibechek[resolver]`). `resolve()`
  never raises — it degrades to the audio read on any failure.
- `reconcile_genre` gained a web tier; the genre taxonomy now recognizes modern
  Beatport subgenres (Tech/Bass/Funky/Future/Afro/Organic House, Melodic House &
  Techno, Midtempo Bass, Future Rave, …). New CLI flags `--genre-classifier`,
  `--genre-web-lookup`, `--genre-llm-backend`, `--genre-override-confidence`;
  new RPCs `setup_clap_engine`, `setup_genre_resolver`.
- **Variant-aware de-duplication.** Acoustic dedupe now keeps the versions a DJ
  wants side by side — Extended vs Radio vs Remix edits, or a FLAC *Original Mix*
  next to an MP3 *Extended* — collapsing only true duplicates *within* a version.
  Configurable: `keep_distinct_versions` (default on), `keep_all_formats`,
  `version_duration_tolerance`; CLI `--across-versions` / `--keep-all-formats`.
- **Configurable existing-tag ↔ ML genre reconciliation**
  (`genre_source_policy`: prefer_tag default / prefer_ml / tag_only / ml_only,
  with `genre_ml_override_confidence`). Specific curated tags are trusted,
  generic junk ("Dance/Pop") is ignored, and a confident disagreeing model read
  can override. The WSL analyzer now **auto-updates in place** on version drift
  (and on same-version code drift via the "No such option" self-heal) instead of
  failing or silently degrading.

### Changed
- **Key detection is materially more accurate.** Switched the Essentia key
  profile from `edma` to Shaath's (a gold-corpus shoot-out of every profile
  ranked it highest for electronic music) and replaced the single full-track
  read with a **3-segment majority vote**. A full-track read systematically
  reported major tracks as their *parallel minor* (right tonic, wrong mode);
  voting across thirds dilutes that confusion. Measured on the 72-track gold
  corpus: **exact-Camelot 65% → 71%**, harmonically-mixable (exact + relative +
  adjacent) **69% → 78%**, at no meaningful extra cost. (The long-standing "~28%"
  figure was a *measurement* bug — the internal scorer string-compared keys
  without normalizing non-Camelot ground truth like "Ab Major" to "4B"; the real
  single-read number was always ~65%.)
- **UI elevation across every view (all OSs).** Bundled Inter Variable +
  JetBrains Mono (identical rendering on Windows/macOS/Linux); dark
  `color-scheme` so native widgets (select popups, checkboxes, scrollbars) stop
  rendering light; dark titlebar + no white startup flash; visible keyboard
  focus rings app-wide; `prefers-reduced-motion` respected; desktop-app
  text-selection + context-menu behavior; library column headers; keyboard-
  operable track rows; modal a11y (role/aria-modal/Esc) on every dialog;
  toasts above modals with pause-on-hover + an amber warning kind; WCAG
  contrast bumps for informational text; solid-red destructive buttons;
  unified modal/button skins.

### Fixed
- **Linux/macOS always reported "essentia not installed"** for the managed
  native venv: `probe_native_venv` put the `python3.*` wildcard in the glob's
  *parent* path, which `Path.glob()` treats as a literal directory name, so
  the Unix `lib/python3.x/site-packages` layout never matched — the Settings
  engine row showed the install as missing even immediately after a
  successful one. Globs now run from the venv root; locked with
  unix/windows-layout regression tests.
- A fresh end-to-end audit (19 findings): CLAP-aware worker memory budgeting
  (prevents an OOM storm on 32 GB boxes), cancellable + progress-emitting web
  lookup that probes/restarts the local LLM first, checkpoint files no longer
  claim "complete" mid-run, genre canonicalization no longer invents
  specificity ("house" → "Tech House"), grounding requires real web results,
  CLAP confidence calibrated to the tag-gate scale, Psytrance-family mapping,
  organize path updates now use the sidecar's actual moved pairs (cancelled
  runs no longer point the library at phantom locations), setup-dialog cancel
  no longer renders as an error, and more (see commit for the full list).

---

## [0.5.0-beta] — 2026-06-04

First release on the simplified `0.x` versioning — the `-beta.N` iteration counter
is retired (the `0.x` line already signals pre-1.0/beta status). Ships the bundled,
one-click ONNX engine setup, the full-stack bug-hunt fixes from the beta.10 cycle,
**and a second, deeper bug hunt (16-domain code/RPC/CLI audit + live-GUI driving)
that found and fixed 46 more issues** — see "Fixed (second bug hunt)" below.

### Added

- **`backup_before_write` now actually backs up.** The Tags-settings toggle
  (default on) reached the config but nothing acted on it — applying ML tags
  did NOT snapshot first, so the safety was illusory. The apply flow now
  snapshots the exact files being tagged (to a timestamped backup recorded in
  history) before mutating them, and reports the backup path.
- **`write_subgenre_as_main_genre` now takes effect.** When off, the parent
  family (e.g. "House") goes in the sortable main-genre frame while the precise
  subgenre ("Deep House") is preserved in its own subgenre frame. (Was a dead
  toggle that did nothing.)
- **Tag apply now supports AIFF / WAV / M4A** (previously errored "unsupported
  format" despite those formats being analyzed and tag-capable).

### Fixed (second bug hunt)

- **Dedupe could delete a file it marked as a "keeper."** Overlapping
  audio-duplicate groups (multi-probe bucketing with no global merge) let one
  file be a keeper in one group and a duplicate in another; the move/trash then
  removed the kept copy. Now clusters are merged globally (union-find) and
  `handle_duplicates` never acts on any group's keeper. Also fixes inflated
  duplicate counts and spurious "file not found" double-processing.
- **Tag backup was lossy / non-identity.** Multi-valued FLAC/M4A artist, genre,
  and composer were truncated to the first value (permanent loss on a
  backup→restore round-trip), and ID3 `TXXX` descriptions were upper-cased on
  restore (renaming ReplayGain / EnergyLevel / MixedInKey frames). Both now
  round-trip verbatim.
- **Cancel during the dedupe move/trash batch was inert** — a destructive
  operation couldn't be stopped. Now cancellable. And a **cancelled organize**
  now returns its partial stats + undo journal (so "Undo" still works) instead
  of erroring out and stranding half-moved files.
- **CDJ export** never down-sampled >48 kHz FLAC (the AIFF wouldn't play on the
  target CDJ) and left a stale SampleRate in the rewritten Rekordbox XML — both
  fixed; resampled tracks are reported.
- **`revert` / `journals` crashed on one malformed journal line**, and `revert`
  silently "succeeded" (reverted 0, exit 0) when pointed at a non-journal file —
  now skipped / validated with clean errors.
- **Audio preview**: a rapid track switch showed the previous track's abort
  error on the new track; large valid files hit a false 15s timeout; the player
  kept a stale path after an organize moved the loaded track — all fixed.
- **~30 more**: clean JSON-RPC errors for non-dict params + no replies to
  notifications; engine-aware `verify_models`; non-empty preflight reasons;
  ONNX crash on ultra-short audio; energy-0 timeslot; organizer `Unknown/`
  subfolder + route dry-run undercount; one bad `library_state` record no longer
  wipes the recent list; clearing an optional path no longer persists `.`; many
  CLI tracebacks (tag/organize/restore-tags/export/dedupe on corrupt or
  wrong-shape input) turned into clean errors; and frontend fixes
  (filters/search/errors-only reset on library switch, select-all on filtered
  lists, dedupe "moved N" count, the ONNX setup dialog gaining a Cancel, the
  risky-install-path warning now surfaced). Every fix has a regression test.

### Added

- **One-click "Set up ONNX engine"** — the ONNX engine is now self-provisioning
  from a single button, so there is no hosting/setup gate for the user to hit.
  A new `setup_onnx_engine` RPC does everything in order, emitting `progress` at
  each step so the GUI shows a live dialog (bar + step message — the app visibly
  works, never looks hung): (1) stages the bundled converted heads into
  `<models>/onnx/`, cleaning interrupted `.partial` leftovers; (2) installs the
  ONNX engine env (WSL or native managed venv) **only if not already usable**, so
  a re-click is a fast no-op; (3) fetches just the EffNet backbone from essentia;
  (4) verifies via the ONNX preflight. Self-healing and idempotent. Verified end
  to end through the live GUI and the frozen sidecar (button → dialog → ready →
  ONNX analyze: genre/bpm/key/mood all correct).
- **The converted ONNX classification heads (~5 MB) now ship inside the app.**
  The seven EffNet heads (`genre_discogs400`, `danceability`, `voice_instrumental`,
  `mood_aggressive/happy/relaxed/sad`) have no official ONNX upstream, so they are
  bundled in `vibechek/onnx_assets/` and embedded by PyInstaller — the ONNX engine
  works offline with no head download (only the official backbone is fetched).
  Heads are stored under a `models/onnx/` subdir so their metadata JSONs never
  collide with essentia's same-named `.pb` set.

### Fixed

Found via a full-stack bug hunt that drove every RPC method, every CLI command,
and the live GUI (tauri-driver + WebDriver) against a real library.

- **Accented track names silently dropped from organize/tag in the desktop app.**
  The packaged app launches the sidecar without a console, where Python picks the
  legacy ANSI code page (cp1252) for `sys.stdin` even with `PYTHONUTF8=1` set — so
  non-ASCII paths the GUI sends back ("Tiësto", "Ultra Naté", "Années 90") arrived
  mojibake'd ("TiÃ«sto"), failed `Path.exists()`, and were reported "not found" and
  skipped. `rpc.serve()` now pins stdin **and** stdout to UTF-8 regardless of the
  inherited locale (cli.py already forced stdout/stderr, never stdin).
- **Organize crashed on a scan-only (non-ML) library** — `plan_organization` did
  `track.get("ml_analysis", {})`, but scan-only records carry the key set to
  `None`, so `None.get(...)` raised. Now `... or {}`; null-ML tracks route to
  `Unknown/` as intended.
- **Path/file resolution is now NFC/NFD-normalization tolerant** in organize + tag
  (`resolve_existing_path`) — a cross-platform analysis.json (macOS NFD) no longer
  skips accented files.
- **Nonexistent / unmounted paths** (removed USB, network share) now return a clean
  `INVALID_PARAMS` instead of an `APP_ERROR` with a raw traceback (dispatch maps
  `FileNotFoundError`/`NotADirectoryError`). Same for a malformed dedupe `report`.
- **CLI `export`/`tag`/`organize` on a corrupt analysis.json** dumped a raw
  `JSONDecodeError`; now a clean Click error. `organize` on an empty analysis
  likewise (was a leaked `ValueError`).
- **Library filters carried across a library switch** — `clearFilters` had no
  caller; now cleared when opening a different library/folder.
- **Settings GPU probe** used a stale captured inference-engine on mount (ONNX
  users saw the wrong engine's GPU status); read at call time now.
- **Open-track inspector** was lost when an organize moved that track
  (`updateTrackPaths` migrated `selectedIds` but not `selectedTrackPath`).
- **Re-running "Set up WSL" did not fix WSL version drift.** The drift guard
  tells users to re-run setup, but the full bootstrap ran `pip install --upgrade
  git+...` for vibechek *without* `--force-reinstall` — and a VCS `--upgrade`
  doesn't re-pull when a version is already present, so the stale package
  survived. The bootstrap now force-reinstalls just the vibechek package (deps
  untouched), so re-running setup reliably clears drift. Verified: WSL
  beta.9 → beta.10 → real analyze works.
- **Drift error named a non-existent RPC** (`repair_wsl_install`) → now
  `upgrade_vibechek_in_wsl` / "Update WSL install".
- **preflight reported "Ready to analyze" while a stale WSL install would fail
  the drift guard.** It now detects WSL version drift on a full probe, reports
  not-ready, and surfaces an actionable "Update WSL install" reason — closing
  the confusing "set up → Ready → analyze errors out of date" loop.
- **Track preview hung 15s then timed out.** WaveSurfer's WebAudio backend
  fetches the track over `asset:` (which the beta.10 CSP fix allowed) and then
  fetches a `blob:` URL to decode — but `blob:` was missing from the CSP
  `connect-src`, so the decode fetch was blocked and the player fired neither
  `ready` nor `error`. Added `blob:` to `connect-src`; preview now loads + plays.
- **A failed model re-download deleted the valid cached model.** `_needs_download`
  re-fetched a locally-present, SHA-pinned file whenever the mirror's HEAD probe
  failed (or a same-named file on another mirror had a different size), and the
  download-failure path then `unlink`ed the existing file — so a transient
  outage (or the not-yet-hosted ONNX head mirror) DELETED good models. Now a
  pinned file is verified locally by its hash (no network refetch needed), and a
  failed download never deletes the existing file (the download streams to a
  `.partial`). Found driving the ONNX engine, which wiped its staged heads on
  every analyze; affects the essentia path too under a flaky mirror.
- **Organize confirm dialog wrongly said "There is no automatic undo"** — organize
  writes a revertible journal and offers an inline "Undo this organize" button
  plus Recent-operations revert. Copy corrected.

---

## [0.4.0-beta.10] — 2026-06-02

Makes the ONNX engine genuinely usable end-to-end — it runs, it's GPU-accelerated,
and the app reflects it — plus audio-preview and release-pipeline fixes found in a
whole-stack bug sweep.

### Fixed
- **Audio track previews** ("failed to fetch") — the bundled CSP had no
  `connect-src`, so WaveSurfer's `fetch()` of the audio was blocked; also added
  the `http://asset.localhost` scheme Windows actually serves.
- **ONNX engine was inert at analyze time** — the analyze RPC never received
  `inference_engine`, so the toggle silently ran essentia_tf. Plus the model
  *download* (button, RPC, CLI, analyze's preflight) ignored the engine and
  fetched the `.pb` set. Both fixed + regression-tested.
- **ONNX GPU acceleration now works** (the whole point of the migration): the
  installer provisions `onnxruntime-gpu` + the `nvidia-*-cu12` CUDA runtime
  wheels when an NVIDIA GPU is present, and `load_onnx_models` calls
  `onnxruntime.preload_dlls()` so the CUDA EP initializes. **Validated: the
  EffNet backbone runs on an RTX 4070's CUDAExecutionProvider, TF-free.** New
  `vibechek[onnx-gpu]` extra; `vibechek download-models --engine onnx`.
- **ONNX GPU status + cross-vendor** — Settings now shows the ONNX engine's real
  GPU via an onnxruntime execution-provider probe in `venv-onnx` (validated:
  "NVIDIA RTX 4070 · CUDA"). The installer auto-picks the runtime per platform:
  NVIDIA→`onnxruntime-gpu`+CUDA (validated), Apple→CoreML (ships in the macOS
  wheel), AMD-Linux→`onnxruntime-rocm` (best-effort). Re-running setup cleanly
  swaps CPU↔GPU `onnxruntime`.
- **ONNX robustness**: genre class-labels are now required for readiness (was
  silently dropping genre), tiny head class-label JSON no longer rejected by the
  size gate, `inference_engine` config value validated, engine-accurate preflight
  messaging, and the ONNX launcher no longer sources TF's `cuda-env.sh`.
- **Release pipeline**: tag pushes now **publish** the GitHub Release (was
  `draft: true`, which left invisible `untagged-*` drafts for every version);
  re-runs are idempotent (pre-existing release for the tag is deleted first).

---

## [0.4.0-beta.9] — 2026-06-01

Completes the ONNX migration into a shippable, TensorFlow-free analysis engine.

### Added
- **ONNX inference engine is now user-selectable and TensorFlow-free.** Builds on
  beta.8's validated backend into a shippable feature: a **Settings → Analysis →
  Inference engine** toggle + a **Set up ONNX engine** button provision a separate
  managed environment (`~/.vibechek/venv-onnx`, a second WSL venv on Windows) with
  **plain Essentia + ONNX Runtime and zero TensorFlow**. Confirmed end-to-end by
  running the real analyzer in a plain-essentia venv on a real track — genre/vocal
  match the TF path and `tensorflow` is never imported. The melspec linchpin is
  settled: plain Essentia ships `TensorflowInputMusiCNN` with **bit-identical**
  output to the TF build, so no NumPy reimplementation is needed. New:
  `download_models(engine="onnx")` fetches SHA256-pinned converted heads from the
  `models-onnx-v1` mirror; `vibechek analyze --engine {essentia_tf,onnx}`;
  engine-aware install/routing across `wsl.py` + `native_install.py`; `vibechek[onnx]`
  extra; `scripts/build_onnx_model_bundle.py`. Default stays `essentia_tf` until the
  head bundle is hosted + cross-platform GPU smoke tests land. See `docs/ONNX_MIGRATION.md`.
- **ONNX preflight matches the TensorFlow path.** Selecting ONNX and starting an
  analyze now runs the same readiness check + one-click setup flow as TF instead of
  failing mid-analyze: `preflight` inspects the `venv-onnx` environment and the
  `.onnx` models for the selected engine, so a missing ONNX engine drives the same
  "Set up" / "Download models" prompts. `preflight` / `detect_wsl` / `check_models`
  are engine-aware (defaults preserve the TF path byte-for-byte); the PreflightDialog
  copy and install routing follow the selected engine.

---

## [0.4.0-beta.8] — 2026-06-01

End-to-end engineering audit + remediation, two new flagship features (FLAC→CDJ
export, opt-in ONNX inference), and the auto-update pipeline. Full suite: 664
Python tests, ruff (now enforcing) clean, 32→38 frontend tests, cargo check clean.

### Added
- **FLAC → CDJ export** (`vibechek cdj-export <rekordbox.xml> --out <dir>`). Lets DJs play a FLAC library on older Pioneer CDJs (CDJ-2000nexus and earlier) that don't support FLAC, **without losing cues or beat grids**: each FLAC is transcoded to a sample-identical 16-bit **AIFF**, and the Rekordbox XML is rewritten so the `TEMPO` (grid) + `POSITION_MARK` (cues) copy across with zero offset math (never MP3 — its ~26 ms encoder delay shifts the grid). Strictly additive; source files never modified. Optional `[cdj]` extra (`soundfile`) with an `ffmpeg` fallback. New `vibechek/cdj_export.py` + 20 tests.
- **ONNX inference backend** (`AnalysisConfig.inference_engine = "onnx"`, opt-in; default stays `essentia_tf`). A path off the end-of-life bundled TensorFlow 2.5 onto ONNX Runtime (`vibechek/onnx_backend.py`), using MTG's official EffNet ONNX backbone (CUDA→ROCm→DirectML→CoreML→CPU execution-provider chain; cross-vendor GPU) + `tf2onnx`-converted heads, with essentia kept only for DSP (melspec/BPM/key). **Validated end-to-end**: on a real track every categorical field (genre, subgenre, vocal, mood, energy, BPM, key, direction, timeslot) matches the TF path, with sub-0.005 float deltas (embedding cosine 0.99942) — see `scripts/onnx_parity.py`. The essentia-tensorflow path is byte-unchanged. (Release follow-up: host the converted head `.onnx` on the model mirror so the engine needs no local conversion.)
- **In-app auto-updater** (`tauri-plugin-updater`, opt-in). Settings → "Software updates" → check / download / install / relaunch. CI signs update artifacts + publishes `latest.json` when a signing key is configured; ships inert (unsigned) until you enroll one — see `docs/RELEASING.md`. Public-key-verified payloads.
- **Configurable key-detection profile** + **BPM octave-error guard** (folds 70↔140 / 87↔174 and cross-checks the filename BPM).

### Fixed (audit — 4 HIGH + 18 MED + 18 LOW)
- **Tag backup was lossy** — captured only ~7 fixed fields for FLAC/M4A and **nothing** for AIFF/WAV/OGG/AAC, while advertised as "no loss." Now format-complete: FLAC reads every Vorbis comment, M4A every atom (binary atoms base64'd), and **AIFF/WAV now capture their ID3 GEOB/PRIV cue frames** (previously a silent cue-loss risk on restore). Restore reports unsupported entries instead of skipping silently.
- **Direction classifier was silently dead** — it averaged both softmax columns → "Steady" for ~every track. Now indexes the aggressive column.
- **UNC / network-share library paths** raised an opaque failure inside WSL → now a clear, actionable error.
- **Concurrent index writes** (`rename_library`/`tag_library`/`forget_*`) raced a fixed `.partial` temp file → unique per-write temp name + module locks.
- **Key accuracy**: switched to Essentia's EDM-tuned `edma` key profile (meaningful accuracy lift on electronic music).
- **De-dup recall**: Chromaprint matching now uses sliding-offset alignment + multi-probe bucketing (catches transcodes index-0-only matching missed); the similarity-threshold setting is now actually forwarded.
- RPC write-path inputs are validated/clamped (`id3_text_encoding`, confidence thresholds, worker counts, inverted vocal bands); `sanitize_folder_name` rejects `..`/reserved device names; multi-GPU VRAM probe pins device 0; voice/genre head class order resolved by label; WSL shim rewrites made atomic; install-distro PowerShell-injection allowlist; filename-BPM false-positive guard; ConfirmModal a11y (focus Cancel on destructive, dialog semantics, Escape); analyze-run streaming guard; asset-protocol + shell capability scoping; and many more — see `internal/AUDIT_2026-06-01.md`.

### Changed
- **CI**: `ruff check` is now enforcing (was advisory); third-party GitHub Actions pinned to commit SHAs; removed the stale `installer.iss` (NSIS-via-Tauri is the supported path).
- **Docs**: competitor comparison claims independently verified against 2026 sources (`docs/COMPETITORS.md`); the ONNX migration plan reframed — official MTG ONNX models already exist, so it's "retire EOL TensorFlow 2.5" (security-driven) + a MAEST backbone, not self-conversion (effort ~3 weeks → ~1 week).

---

## [0.4.0-beta.7] — 2026-05-29

Three user-reported accuracy/UX bugs.

### Fixed
- **Vocal detection mislabelled instrumental dance as "Vocal".** Instrumental tracks with prominent melodic leads (e.g. Robert Miles "Children", Eric Prydz "Pjanoo") score ~0.64–0.71 on essentia's voice/instrumental model — above the old 0.6 cutoff, so they were wrongly tagged "Vocal". Recalibrated the cutoffs against measured scores: voice probability `< 0.72` → **Instrumental**, `< 0.88` → **Light Vocal**, else **Vocal**. Verified: Children (0.703) and Pjanoo (0.642) now classify Instrumental; Adele "Chasing Pavements" (0.972) stays Vocal.
- **Audio preview started a new track at the previous track's elapsed time.** Loading track B while track A was 35s in began B at 0:35 (and could seek past B's end if B was shorter). The global player now `stop()`s before loading and seeks to 0 on `ready`, so every preview starts at 0:00.
- **macOS release build hard-failed at the codesign step.** `release.yml` passed `APPLE_CERTIFICATE: ${{ secrets.* }}` directly into the build step's `env:`; an unset secret arrives as an empty-but-defined variable, and Tauri 2's Rust bundler (`std::env::var` → `Ok("")`) treated that as "a cert is present", ran `security import` on empty data, and died with `SecKeychainItemImport: One or more parameters passed to a function were not valid`. Signing is now genuinely opt-in: a new "Configure code signing (opt-in)" step exports each cert var to `$GITHUB_ENV` only when its secret is non-empty, so an unconfigured repo builds an **unsigned** `.dmg`/`.app` instead of failing. Beta macOS builds are unsigned for now — the release notes + README document the one-time `xattr -dr com.apple.quarantine` / right-click-Open Gatekeeper bypass.

### Added
- **Raw vocal score is now stored** (`MLResult.ml_vocal_score`, 0–1), so the Instrumental/Light Vocal/Vocal label can be **retuned and re-applied at tag time without re-analyzing**. (Tracks analyzed before beta.7 lack the raw score and need one re-analysis to benefit.)
- **Configurable vocal sensitivity.** New `TaggingConfig.vocal_instrumental_max` (0.72) and `vocal_full_min` (0.88), surfaced in Settings as a "Vocal detection sensitivity" dual slider (Instrumental ≤ / Vocal ≥). Plumbed through the `apply_ml_tags` RPC.
- **Per-field write toggles.** Replaced the single `skip_bpm_and_key` flag with independent `write_genre / write_bpm / write_key / write_energy / write_mood / write_timeslot / write_direction / write_vocal` toggles (BPM & Key default **off** — Rekordbox's own detection is usually better). Each ML field can now be written independently; genre remains additionally gated by its confidence thresholds. Surfaced as a "Write these fields" grid in Settings. This is what makes non-genre tags writable independent of genre confidence — they were always computed independently, and the granular toggles make that explicit and controllable.

### Changed
- **BREAKING (config):** `TaggingConfig.skip_bpm_and_key` removed in favor of the `write_bpm` / `write_key` toggles. The RPC still accepts the legacy `skip_bpm_and_key` param for back-compat (maps to `write_bpm = not skip`); the CLI `tag` command maps `--skip-bpm-key` the same way.

---

## [0.4.0-beta.6] — 2026-05-18

### Added
- **Hybrid CPU + GPU analysis (work-stealing).** Previously analyze ran ONE device — either ~3 GPU workers (VRAM-capped) OR N CPU workers — so a modest GPU's low worker cap throttled throughput while most cores sat idle. Now, when a GPU is available and GPU mode isn't "off", the analyzer runs GPU workers (`CUDA_VISIBLE_DEVICES=0`) AND CPU workers (`=-1`) concurrently against a single shared work queue. The queue *is* the load balancer: whichever device finishes a track grabs the next, so fast and slow devices self-balance with no predictive scheduling. Total workers are bounded by RAM; the GPU subset by VRAM; CPU fills the rest. Per-device throughput (count + avg latency) is measured and reported. Verified on an RTX 4070 Laptop: a 50-track run split GPU 9 (17.5s/track) + CPU 41 (23.5s/track), using all resources. New `AnalysisConfig.hybrid_cpu_gpu` (default on), `--hybrid/--no-hybrid` CLI flag, and a **Settings toggle**. Worker recycling (process-exit every 200 tracks) and the stall watchdog / cancellation are preserved. Linux-CI hybrid-pool tests added.
- **Single global audio player.** Replaced the per-track embedded WaveSurfer (which kept playing after you navigated away, and let multiple previews sound at once) with one persistent player bar mounted at the app root. It survives tab/menu changes, always shows a stop control, and loading a new track stops the previous one (a single WaveSurfer instance — two previews can never overlap). `usePlayerStore` is the single source of truth; TrackDetails just calls `play(path, title)`. Removed the dead `AudioPreview` component.

### Fixed
- Audio playback: navigating away no longer leaves a track playing with no way to stop it; clicking a new track no longer stacks a second simultaneous preview.

---

## [0.4.0-beta.5] — 2026-05-18

Undo journal + the remaining audit LOW/informational fixes.

### Added
- **Operation undo journal** (`vibechek/journal.py`). `organize` and `dedupe` (move-to-review) now write an append-only JSONL journal — each completed move is recorded + flushed BEFORE the next, so a partial run (disk full, crash, power loss) is recoverable AND a finished operation can be reverted. New `revert_journal` moves files back to their origins (newest-first, never clobbering an occupied origin); `list_journals` powers an undo list. Trash entries are journaled for transparency but flagged non-revertible (send2trash → OS recycle bin has no reliable restore). New RPCs `list_journals` + `revert_journal` (+ typed TS wrappers), CLI `vibechek journals` / `vibechek revert <file>`, and `OrganizeStats.journal_path` / dedupe summary `journal_path`. 9 new journal tests.
- **Undo UI**: a "Recent operations" modal (sidebar entry) lists every organize / dedupe and offers one-click Undo (reverts via `revert_journal`); the Organize result screen gained an inline "Undo this organize" button; dedupe completion toasts now point to the undo surface (or the recycle bin for trash).
- **Wired the rest of the backend into the UI** — these shipped RPCs previously had no UI path:
  - **DJ profiles** picker in Settings (`list_profiles` / `load_profile`) — one-click presets.
  - **Copy diagnostic** (`doctor`), **Verify model integrity** (`verify_models`), and **Update WSL install** (`upgrade_vibechek_in_wsl`, the fast version-drift repair) buttons in a new Settings "Diagnostics & maintenance" section.
  - **Rename / tag** a recent library (`rename_library` / `tag_library`) via an inline editor on the recent-library cards.
- **Sidebar version** now reflects the real sidecar version (`version` RPC) instead of a hardcoded string that had drifted to "v0.3.0-dev".

### Fixed (audit LOW + informational)
- **Double `aggressive` model inference per track** — the Direction calc re-ran the most expensive ML head on the full embedding; now reuses the array already computed in the mood loop (measurable speedup on large libraries).
- **`find_audio_files` aborted the whole scan** on one unreadable entry (broken symlink, MAX_PATH, permission denied) → now skips the bad entry and continues.
- **`sanitize`/install fragility**: `_run_phase` reverse-parsed the staged inner-script path out of the launcher text (broke on paths with spaces, leaked tempfiles) → now captures the `Path` up front. `_resolve_cuda_packages` had an operator-precedence smell in cu12 detection → parenthesized. `repair_wsl_shim` decoded stdout as utf-8 only → now multi-encoding like the other WSL probes.
- **JSON-RPC**: failed *notifications* (no `id`) wrongly emitted an error response → now silent per spec. Sidecar shutdown sets the cancellation flag before pool teardown so an in-flight analyze/install can unwind instead of orphaning subprocesses.
- **CLI `export`** crashed on a malformed `analysis.json` with non-dict track entries → skips them.
- **M4A BPM restore** raised `ValueError` on a non-numeric backup value (`"128 BPM"`) and failed the whole file → coerces defensively.
- **keys.py**: an explicit-but-unknown mode (`"C dim"`) no longer silently resolves to major; `is_compatible_with` is now genuinely symmetric for the directional `energy-boost` mode (checks both directions).
- **genres.py**: removed dead "promote more specific subgenre" branch (the guard could never fire given descending sort).
- **config int coercion** accepts string/float forms uniformly (`int(round(float(v)))`) instead of truncating/raising inconsistently.
- **resources.py** `nvidia-smi` device probe now checks the exit code before parsing (NVML driver-mismatch errors).
- **Frontend**: Settings engine-GPU error unwraps `RpcError.message` instead of `String(e)` → no more `[object Object]`; `operation.fail` dropped the fragile `includes("cancelled by user")` substring heuristic (relies on the reliable `RpcError.cancelled`); Rust shell logs post-timeout late responses as expected-noise, not errors.
- Added a real (CI-runnable, synthetic-MP3-backed) **Rekordbox GEOB/PRIV preservation regression test** — guards the product's #1 feature against a tag write ever stripping cue points / beat grids, including across a double re-apply.

---

## [0.4.0-beta.4] — 2026-05-18

End-to-end codebase audit. Fixed all HIGH + MED findings.

### Fixed — data safety (HIGH)
- **Non-atomic JSON writes in the analyzer.** The final report write, the every-50-tracks checkpoint (`_write_partial`), and the WSL path-rewrite all used `Path.write_text(json.dumps(...))` — a kill/power-loss/disk-full mid-write truncated the report (up to 32 MB / 30+ min of GPU time). All now use `vibechek.io.atomic_write_json`. Checkpoint writes are also wrapped so a transient write error logs-and-continues instead of aborting the whole run.
- **Path traversal via genre tags** (`utils.sanitize_folder_name`). A track's existing genre tag (attacker-controlled on a downloaded file) flows into `organizer.route_new_tracks` as a destination folder; a genre of `..` or `../../Windows` escaped the library root. `sanitize_folder_name` now strips leading/trailing dots + separators and rejects `.`/`..` outright.
- **organize move could overwrite an existing file** (data loss). `shutil.move` overwrites silently. Two source files with the same basename routing to one genre folder both planned the same destination (neither existed on disk at plan time), and the second move clobbered the first. Fixed with an intra-batch `claimed` destination set in `plan_organization` plus an execute-time `_unique_destination` re-check that never overwrites.
- **`duplicates.save_report` non-atomic** → switched to `atomic_write_json` (the report drives destructive delete/move decisions).

### Fixed — correctness (HIGH/MED)
- **WSL installs used `setsid` without `-w`** (the documented fork-and-exit landmine) in `install_vibechek_in_wsl` and `install_cuda_libs_in_wsl` — the parent saw instant exit 0 while apt/pip ran orphaned, reporting "Install complete" before anything installed. Both now use `setsid -w` like the analyze path.
- **stderr pipe deadlock**: `run_vibechek_in_wsl` / `run_vibechek_in_native_venv` only drained stderr when an `on_stderr_line` callback was supplied; a verbose child filling the ~64KB stderr pipe buffer while the parent blocked on stdout would deadlock. stderr is now always drained on a background thread.
- **Two-stage tagger over-tagged legacy reports.** Re-applying a pre-`ml_genre_raw_confidence` analysis tagged ~30% more files with parent genres than the user saw when that report was the live behaviour. Stage 2 (parent fallback) is now disabled when `ml_genre_raw_confidence` is absent, matching the documented "legacy behaviour exactly."
- **Duplicate keeper selection** could keep a 0-byte corrupt file over real audio (format priority alone won) and was non-deterministic on ties. Now deprioritizes empty files and adds a path tiebreaker.
- **Cancellation ignored** in the duplicate trash/move loops and `organizer.route_new_tracks` — a Cancel mid-batch kept moving/copying files. All now check `cancellation.check()`.
- **`restore_tags_with_remap`** leaked raw `JSONDecodeError`/`KeyError` on a corrupt backup (the non-remap path was already hardened). Both now share `_load_backup_files` validation.
- **Truncated WSL/venv output** raised an opaque `UnicodeDecodeError` instead of the friendly "doesn't parse as JSON" message. Both analyze paths now read bytes once + decode with `errors="replace"`.
- **`nvidia-smi` device probe** parsed stdout without checking the exit code (NVML mismatch errors). Now bails on non-zero return.
- **CLI `analyze`** accepted negative `--workers`/`--skip`/`--limit` (e.g. `--skip -5` analyzed only the last 5 tracks). Now `click.IntRange(min=0)`.

### Fixed — frontend (HIGH/MED)
- **RPC sync guardrail was self-referential** — 7 Python methods (`rename_library`, `tag_library`, `count_new_tracks`, `doctor`, `verify_models`, `list_profiles`, `load_profile`) were missing from `RPC_METHODS` AND its hand-maintained test mirror, so the drift test stayed green while the TS wrappers didn't exist. Added all 7 (typed wrappers + param types), and added an authoritative cross-language check (`tests/test_rpc_method_sync.py`) that reads both `vibechek/rpc.py` and `ui/src/api/methods.ts` directly.
- **`track_analyzed` stale-event corruption**: a cancelled/superseded analyze kept streaming events that got merged as phantom tracks into a freshly-opened library. App.tsx now drops events whose path isn't under the current `libraryPath`.
- **`fail(String(e))` reintroduced** in LibraryBrowser's preflight catch — discarded the RpcError `cancelled` flag (user-cancel surfaced as an error toast). Reverted to `fail(e)`.
- **OrganizeView executed from live state, not the confirmed plan**: `currentParamsKey` only fingerprinted track *count*, so a content change (same count) left a stale plan looking valid before a destructive, no-undo move. Now includes a path+genre content fingerprint.
- **"Select all" ignored active filters** → selected (and could bulk-tag) hidden tracks. Now selects the filtered set via a new `selectPaths` store action.
- **DuplicatesView "space to free"** went stale after a rule reorder (used the backend's precomputed `recoverable_mb` instead of the rule-picked keeper). Now computes from `currentKeeper` with `rulesSig` in deps.
- **`scan_directory` mis-classed as a 60s QUICK op** in the Rust shell — timed out on large network shares. Moved to MEDIUM.
- Removed a `dangerouslySetInnerHTML` footgun in the Settings `Toggle` (rendered static labels as raw HTML).

---

## [0.4.0-beta.3] — 2026-05-18

### Fixed
- **WSL multi-track analyze silently exited 1.** Root cause: a WSL `vibechek` install older than the sidecar (worst case: v0.1.0-dev from the first setup) is missing the worker cap and stall watchdog, so the CLI dispatched `--workers 19` straight to essentia and the resulting 19 TF processes OOM-crashed in seconds. The bounded 80-line stderr tail filled with per-worker essentia `MusicExtractorSVM: no classifier models were configured by default` noise so the real error never made it back to the GUI.
  - `_analyze_via_wsl` now refuses to dispatch when the WSL vibechek version differs from the sidecar (`__version__`), surfacing a clear "WSL vibechek is out of date — re-run setup" error instead.
  - Stderr noise filter strips per-worker essentia INFO and TF GPU init chatter from the bounded tail buffer so genuine tracebacks and `VIBECHEK_WORKER_INIT_FAIL` markers survive.
  - `install_vibechek_in_wsl` now uses `pip install --upgrade` so re-running Set up WSL is an idempotent way to fix drift.
- **Tauri sidecar failed to load Python DLL at startup.** Root cause: PyInstaller's old `--onedir` mode produced an EXE that loaded a sibling `_internal/` folder containing `python3*.dll`, but Tauri 2's `externalBin` config is a single-file contract — it only copies the EXE to `target/<profile>/`, never `_internal/`. The dev sidecar died with `[PYI-xxxx:ERROR] Failed to load Python DLL`. The same gap silently broke macOS code-signing (Apple notarytool rejects bundles whose `_internal/.dylib`s are unsigned) and the Linux AppImage layout.
  - Switched `packaging/vibechek.spec` to `--onefile` mode. One self-contained binary per platform, one signing op per platform, no `_internal/` staging.
  - Rewrote `packaging/stage-sidecar.bat` and `packaging/stage-sidecar.sh` to copy that single file into both `ui/src-tauri/binaries/` (for Tauri's externalBin) and `ui/src-tauri/target/{debug,release}/` (for `cargo run` / `npm run tauri dev`).
  - Build scripts (`build-{windows.bat,macos.sh,linux.sh}`) updated for the new `dist/vibechek(.exe)` path (no `dist/vibechek/vibechek` subfolder).
  - `.github/workflows/release.yml` `sidecar_source` values updated to match.
  - Trade-off: cold sidecar startup is ~500ms slower (700ms vs 200ms — measured on Windows). Acceptable because the sidecar is a long-lived RPC server (one startup per app session, not per RPC call).

### Added
- New `upgrade_vibechek_in_wsl` RPC (Python) and `upgradeVibechekInWSL` TS wrapper. Fast-path re-install of just the vibechek package inside WSL (skips apt + essentia) — the one-click repair for the version-drift case.
- `DistroInfo.vibechek_version` populated by the WSL probe from `site-packages/vibechek-*.dist-info` so the GUI can show what's actually installed.
- `ui/src-tauri/entitlements.plist` with the standard PyInstaller-on-macOS hardened-runtime entitlements (`disable-library-validation`, `allow-jit`, `allow-unsigned-executable-memory`, `allow-dyld-environment-variables`). Wired into `tauri.conf.json` `bundle.macOS.entitlements` so `tauri-action`'s codesign picks them up automatically. Documented in `docs/RELEASING.md`.
- **Streaming progress + per-track results during analyze.** The GUI used to sit at "starting…" for 30-60 s while preflight + WSL boot + worker spawn ran with zero feedback. Now there's a structured event channel:
  - `vibechek/analyzer.py:_emit_event(type, **payload)` writes `VIBECHEK_EVENT\t<type>\t<json>` lines to stderr when `VIBECHEK_STREAM_PROGRESS=1` is set. Activated automatically inside the WSL launcher (`vibechek/wsl.py:run_vibechek_in_wsl`) and the managed-venv launcher (`vibechek/native_install.py:run_vibechek_in_native_venv`).
  - Events fired at every stage: `scanning`, `preflight`, `wsl_dispatch`, `venv_dispatch`, `analyzing`, `loading_models`, `spawning_workers`, plus one `track` event per completed file carrying the full ML record.
  - `_make_event_aware_line_handler` parses these out of subprocess stderr and routes them to the existing `on_progress` callback (stage events update the status message) AND a new `on_track` callback (per-track records).
  - New `track_analyzed` JSON-RPC notification (`vibechek/rpc.py:_emit_track_analyzed`) → Rust shell re-emits as `sidecar:track_analyzed` Tauri event → frontend's `App.tsx` merges each record into `useLibraryStore.tracks` in real time via the new `mergeAnalyzedTrack(record)` action.
  - 9 new regression tests in `tests/test_analyzer_event_stream.py` cover the emitter, the parser, the noise filter, and exception silencing.
- **Audio preview fixes:**
  - WSL-streamed `track_analyzed` events carried POSIX paths (`/mnt/c/...`) so Tauri's `convertFileSrc()` produced broken `asset://` URLs and the AudioPreview waveform refused to load. `_analyze_via_wsl` now wraps `on_track` to translate paths back to Windows form before forwarding (mirrors the existing post-analyze translation on the report's `tracks` array).
  - `TrackDetails` was rendered outside the viewMode switch in `App.tsx`, so AudioPreview (and its WaveSurfer instance) stayed mounted across every tab — playback continued after navigating to Duplicates / Organize / etc. with no visible control. Gated `<TrackDetails />` on `viewMode === "library"` so the panel unmounts on tab change and WaveSurfer's destroy() stops playback.
  - AudioPreview's error display used `truncate` (single-line ellipsis), hiding the actually-useful part of long error messages like "Could not decode audio: …". Switched to wrap + `whitespace-pre-wrap` + scrollable max-height so the full error is readable inline.
- **GPU worker cap re-tuned to prevent mid-analyze stalls.** Bumped `_GPU_WORKER_MB` from 1500 to 2500 MB after the user hit a 5-min stall watchdog with workers=5 (the old cap on an RTX 4070 Laptop 8 GB). Empirical testing: 5 workers stalled after ~12 tracks (TF growth-allocator fragmentation pushed each worker's footprint past 1500 MB), 4 workers stalled at startup under contention, 3 workers ran cleanly to completion. The new 2500-MB budget reflects steady-state per-worker memory including CUDA context + model graphs + activation buffers + fragmentation overhead. Trade-off: a 24-GB card now gets 9 workers (was 13), but the previous cap was producing stall-watchdog errors with no useful diagnostic — the worst possible failure mode. We prefer "always finishes" over "sometimes faster".
- **Cap message now actionable.** Previously: "Capped workers from 19 to 5 due to 7 GB free VRAM". Now: `"Capped workers from 19 to 3 (7 GB free VRAM (~2500 MB per worker)). Set 'GPU mode' to 'off' in Settings to use your full 17-worker CPU budget instead."` — surfaces the tradeoff and the override path. Emitted on both the legacy progress channel AND the structured `worker_cap` stage event for the GUI overlay.

### Added
- `vibechek doctor` CLI command — prints a complete environment report for bug reports (Python version, OS, sidecar location, venv / WSL status, GPU detection, recent log tail).
- Typed RPC wrapper `ui/src/api/rpc.ts` — every UI call now goes through a single typed surface backed by `ui/src/types/generated.ts`.
- CI: CodeQL workflow for Python and TypeScript, weekly + on PR.
- CI: enforcing lint on the main matrix once cleanup is done; advisory `lint-strict` job in the meantime.
- CI: `tsc --noEmit` and `npm run build` checks in the frontend job.
- CI: Python coverage report uploaded as a per-run artifact.
- CI: README stats freshness check (fails the build if `README.md` is out of date relative to the code).
- Dependabot: weekly grouped PRs for pip, npm, cargo, github-actions.
- Issue templates, PR template, contributing guide, code of conduct, security policy, funding manifest.
- Intel Mac (`macos-13`) build in the release matrix alongside the existing Apple Silicon job.
- Public bus-factor doc ([docs/MAINTAINERS.md](docs/MAINTAINERS.md)) and contracts walkthrough ([docs/CONTRACTS.md](docs/CONTRACTS.md)).
- Competitor citations ([docs/COMPETITORS.md](docs/COMPETITORS.md)) for every claim in the README comparison table.

### Changed
- README: headline now leads with Rekordbox cue preservation (was buried six paragraphs in). Stats line is auto-regenerated by `scripts/update_readme_stats.py`.

---

## [0.3.0-beta.11] — 2026-05-17

Beta cycle wrap-up. Eleventh beta exists because early user feedback surfaced real edge cases faster than any test matrix could.

### Fixed
- Long-tail stability fixes from public beta feedback.
- Improved error surfacing when the sidecar dies mid-operation.

### Changed
- Final docs polish ahead of v0.3.0 stable.

---

## [0.3.0-beta.10] — 2026-05-12

### Fixed
- Race condition between cancellation and progress reporting on very fast operations.
- Tag backup history corruption when two libraries shared a backup directory.

---

## [0.3.0-beta.9] — 2026-05-06

### Added
- Per-library auto-save of the last analysis so re-opening the app restores state immediately.

### Fixed
- Library state index growing unbounded across sessions.

---

## [0.3.0-beta.8] — 2026-04-29

### Fixed
- Windows-only: WSL shim repair when the user upgraded WSL outside the app.
- Subgenre fallback when Discogs-EffNet returns a low-confidence top label.

---

## [0.3.0-beta.7] — 2026-04-21

### Added
- `native_venv_status` RPC method so the UI can show macOS / Linux venv state without spawning a subprocess on every poll.

### Changed
- Tightened error codes returned by the JSON-RPC layer so the UI can branch on `INVALID_REQUEST` vs `APP_ERROR` cleanly.

---

## [0.3.0-beta.6] — 2026-04-14

### Fixed
- Organizer dry-run plan now correctly previews path collisions before the user commits.
- Chromaprint dedupe no longer panics on zero-length files.

---

## [0.3.0-beta.5] — 2026-04-07

### Added
- Backup history view: every snapshot is timestamped and restorable.
- `forget_backup` RPC method to prune stale entries from the history.

---

## [0.3.0-beta.4] — 2026-03-31

### Fixed
- `__TRY_CHAIN__` self-substitution bug in the WSL bootstrap shell wrapper.
- NVIDIA repo sanity check before attempting CUDA install (prevents adding a broken apt source).

---

## [0.3.0-beta.3] — 2026-03-24

### Fixed
- CUDA install regression: keyring placement on Ubuntu 22.04 vs 24.04.

---

## [0.3.0-beta.2] — 2026-03-17

### Fixed
- Cross-platform GUI install resilience — the Preflight dialog now correctly recovers from a half-installed venv.
- Improved CUDA install logging so the failure mode is visible without trawling WSL logs.

---

## [0.3.0-beta.1] — 2026-03-10

First public beta. Feature-complete, headed for stable.

### Added
- End-to-end ML pipeline: genre + subgenre across ~400 Discogs categories, BPM, key, energy 0-5, mood, timeslot, direction, vocal type, danceability.
- Acoustic duplicate detection via Chromaprint (MD5 fallback for byte-identical files).
- One-click organize into `Genre/Subgenre/` tree with dry-run preview.
- Full tag backup / restore including binary GEOB and PRIV frames (Rekordbox-safe).
- Cross-platform GPU detection that probes the actual analysis engine, not just the host.
- Tauri desktop app with five tabs: Library, Duplicates, Organize, Tags, Settings.
- CLI parity: every GUI action is a `vibechek` subcommand.
- Windows auto-install of WSL Ubuntu + Essentia inside it, with transparent path translation.
- macOS / Linux hermetic venv at `~/.vibechek/venv/` so the system Python is never touched.
- JSON-RPC stdin/stdout bridge between the Tauri shell and the Python sidecar.
- Auto-generated TypeScript types mirroring Python dataclasses.
- Cross-platform CI release pipeline producing signed (when secrets are configured) installers.

---

[Unreleased]: https://github.com/captinjack99/Vibechek/compare/v0.9.0-beta...HEAD
[0.9.0-beta]: https://github.com/captinjack99/Vibechek/compare/v0.8.2-beta...v0.9.0-beta
[0.8.2-beta]: https://github.com/captinjack99/Vibechek/compare/v0.8.1-beta...v0.8.2-beta
[0.8.1-beta]: https://github.com/captinjack99/Vibechek/compare/v0.8.0-beta...v0.8.1-beta
[0.8.0-beta]: https://github.com/captinjack99/Vibechek/compare/v0.7.0-beta...v0.8.0-beta
[0.7.0-beta]: https://github.com/captinjack99/Vibechek/compare/v0.6.3-beta...v0.7.0-beta
[0.6.3-beta]: https://github.com/captinjack99/Vibechek/compare/v0.6.2-beta...v0.6.3-beta
[0.6.2-beta]: https://github.com/captinjack99/Vibechek/compare/v0.6.1-beta...v0.6.2-beta
[0.6.1-beta]: https://github.com/captinjack99/Vibechek/compare/v0.6.0-beta...v0.6.1-beta
[0.6.0-beta]: https://github.com/captinjack99/Vibechek/compare/v0.5.0-beta...v0.6.0-beta
[0.5.0-beta]: https://github.com/captinjack99/Vibechek/compare/v0.4.0-beta.10...v0.5.0-beta
[0.4.0-beta.10]: https://github.com/captinjack99/Vibechek/compare/v0.4.0-beta.9...v0.4.0-beta.10
[0.4.0-beta.9]: https://github.com/captinjack99/Vibechek/compare/v0.4.0-beta.8...v0.4.0-beta.9
[0.4.0-beta.8]: https://github.com/captinjack99/Vibechek/compare/v0.4.0-beta.6...v0.4.0-beta.8
[0.4.0-beta.6]: https://github.com/captinjack99/Vibechek/compare/v0.4.0-beta.3...v0.4.0-beta.6
[0.4.0-beta.3]: https://github.com/captinjack99/Vibechek/compare/v0.3.0-beta.11...v0.4.0-beta.3
[0.3.0-beta.11]: https://github.com/captinjack99/Vibechek/compare/v0.3.0-beta.10...v0.3.0-beta.11
[0.3.0-beta.10]: https://github.com/captinjack99/Vibechek/compare/v0.3.0-beta.9...v0.3.0-beta.10
[0.3.0-beta.9]: https://github.com/captinjack99/Vibechek/compare/v0.3.0-beta.8...v0.3.0-beta.9
[0.3.0-beta.8]: https://github.com/captinjack99/Vibechek/compare/v0.3.0-beta.7...v0.3.0-beta.8
[0.3.0-beta.7]: https://github.com/captinjack99/Vibechek/compare/v0.3.0-beta.6...v0.3.0-beta.7
[0.3.0-beta.6]: https://github.com/captinjack99/Vibechek/compare/v0.3.0-beta.5...v0.3.0-beta.6
[0.3.0-beta.5]: https://github.com/captinjack99/Vibechek/compare/v0.3.0-beta.4...v0.3.0-beta.5
[0.3.0-beta.4]: https://github.com/captinjack99/Vibechek/compare/v0.3.0-beta.3...v0.3.0-beta.4
[0.3.0-beta.3]: https://github.com/captinjack99/Vibechek/compare/v0.3.0-beta.2...v0.3.0-beta.3
[0.3.0-beta.2]: https://github.com/captinjack99/Vibechek/compare/v0.3.0-beta.1...v0.3.0-beta.2
[0.3.0-beta.1]: https://github.com/captinjack99/Vibechek/releases/tag/v0.3.0-beta.1
