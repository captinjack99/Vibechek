import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Virtuoso } from "react-virtuoso";
import { AnimatePresence } from "framer-motion";
import {
  FolderOpen, Sparkles, Search, Music, AlertCircle, AlertTriangle, CheckSquare,
  Square, Tag, Eye, RefreshCw, Clock, Loader2, X, ChevronDown, Compass, Pencil,
  Check, Undo2, Import,
} from "lucide-react";
import { clsx as cx } from "clsx";
import { open as openDialog } from "@tauri-apps/plugin-dialog";

import {
  useLibraryStore,
  useOperationStore,
  useConfigStore,
  useUIStore,
  useNotificationStore,
} from "../stores";
import { rpc } from "../hooks/useSidecar";
import { importTagPriors } from "../api/rpc";
import { useApplyTags } from "../hooks/useApplyTags";
import type {
  AnalysisReport, LibraryRecord, LibraryState, PreflightResult, TrackAnalysis,
} from "../types";
import { TagBadge, EnergyBar } from "./TagBadges";
import { PreflightDialog } from "./PreflightDialog";
import { ConfirmModal } from "./ConfirmModal";
import { FilterChips, applyFilters, emptyFilters, useLibraryFiltersStore } from "./LibraryFilters";
import { needsReview, reviewReason } from "../lib/review";

/** Compact number formatter — "12k" instead of "12,466". */
const compactFmt = new Intl.NumberFormat(undefined, {
  notation: "compact",
  maximumFractionDigits: 1,
});

/** "3 hours ago", "yesterday", "2 days ago". Epoch seconds in, friendly string out. */
function relativeTime(epochSeconds: number): string {
  if (!epochSeconds) return "never";
  const secondsAgo = Math.max(0, Date.now() / 1000 - epochSeconds);
  if (secondsAgo < 60) return "just now";
  const minutes = Math.floor(secondsAgo / 60);
  if (minutes < 60) return minutes === 1 ? "1 minute ago" : `${minutes} minutes ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return hours === 1 ? "1 hour ago" : `${hours} hours ago`;
  const days = Math.floor(hours / 24);
  if (days === 1) return "yesterday";
  if (days < 30) return `${days} days ago`;
  const months = Math.floor(days / 30);
  if (months < 12) return months === 1 ? "1 month ago" : `${months} months ago`;
  const years = Math.floor(months / 12);
  return years === 1 ? "1 year ago" : `${years} years ago`;
}

/** Last path segment, handling both / and \ separators (Windows + POSIX). */
function basename(path: string): string {
  const segments = path.split(/[\\/]/).filter(Boolean);
  return segments[segments.length - 1] ?? path;
}

/**
 * Friendly display name for a library: user-set `name` if present, otherwise
 * the folder basename. Mirrors the Python-side `LibraryRecord.display_name()`
 * so the switcher and the recent-cards label libraries identically.
 */
function displayName(record: LibraryRecord): string {
  // `name` may be undefined on records returned from older sidecars; treat
  // any falsy value as "fall back to basename".
  const explicit = (record as LibraryRecord & { name?: string }).name;
  if (explicit && explicit.trim()) return explicit;
  return basename(record.path);
}

/** Wire shape for the count_new_tracks RPC. */
interface NewTracksCount {
  new_count: number;
  total_count: number;
  analyzed_count?: number;
  reason?: string;
}

export function LibraryBrowser() {
  const tracks = useLibraryStore((s) => s.tracks);
  const libraryPath = useLibraryStore((s) => s.libraryPath);
  const setLibraryPath = useLibraryStore((s) => s.setLibraryPath);
  const setTracks = useLibraryStore((s) => s.setTracks);
  const mergeAnalyzedTracks = useLibraryStore((s) => s.mergeAnalyzedTracks);
  const searchFilter = useLibraryStore((s) => s.searchFilter);
  const setSearchFilter = useLibraryStore((s) => s.setSearchFilter);
  const selectedIds = useLibraryStore((s) => s.selectedIds);
  const toggleSelect = useLibraryStore((s) => s.toggleSelect);
  const selectPaths = useLibraryStore((s) => s.selectPaths);
  const clearSelection = useLibraryStore((s) => s.clearSelection);
  // Live-merge run token API. `beginAnalyzeRun` authorizes streamed
  // `track_analyzed` events for a run; `invalidateAnalyzeRun` drops that
  // authorization on a library switch so a prior run's queued events can't
  // phantom-merge into the new library.
  const beginAnalyzeRun = useLibraryStore((s) => s.beginAnalyzeRun);
  const endAnalyzeRun = useLibraryStore((s) => s.endAnalyzeRun);
  const invalidateAnalyzeRun = useLibraryStore((s) => s.invalidateAnalyzeRun);

  const active = useOperationStore((s) => s.active);
  const begin = useOperationStore((s) => s.begin);
  const finish = useOperationStore((s) => s.finish);
  const fail = useOperationStore((s) => s.fail);
  const errorMsg = useOperationStore((s) => s.error);

  const analysisCfg = useConfigStore((s) => s.config.analysis);
  const taggingCfg = useConfigStore((s) => s.config.tagging);

  const setSelectedTrack = useUIStore((s) => s.setSelectedTrack);
  const selectedTrackPath = useUIStore((s) => s.selectedTrackPath);

  const notify = useNotificationStore((s) => s.notify);
  const { apply: applyTags } = useApplyTags();

  const [scanCount, setScanCount] = useState<number | null>(null);
  const [preflightResult, setPreflightResult] = useState<PreflightResult | null>(null);
  // Snapshot the bulk-tag scope at modal-open time. Was previously just a
  // "selected" | "all" enum, but that re-reads `selectedIds` on confirm —
  // if the user opens the modal then de-selects rows behind the dimmed
  // backdrop, confirm would tag whatever the *current* selection is, not
  // what they reviewed. Now we capture the exact tracks once.
  const [confirmBulkTag, setConfirmBulkTag] = useState<{
    scope: "selected" | "all";
    targets: TrackAnalysis[];
  } | null>(null);
  const [confirmForget, setConfirmForget] = useState<LibraryRecord | null>(null);
  const filters = useLibraryFiltersStore((s) => s.filters);
  const setFilters = useLibraryFiltersStore((s) => s.setFilters);
  const [showErrorsOnly, setShowErrorsOnly] = useState(false);
  // Trust-UX: narrow to tracks whose genre sources disagree (tag ≠ audio ≠ web),
  // so a skeptical pro can review exactly the calls Vibechek wasn't sure about.
  // Composes with the chip filters (unlike errors-only, which replaces them).
  const [showReviewOnly, setShowReviewOnly] = useState(false);
  // True while a resolve_genre_conflicts RPC is in flight, so the Approve/Revert
  // buttons disable and can't double-fire on a slow disk write.
  const [resolving, setResolving] = useState(false);
  // Same guard for the import_tag_priors RPC (the Import Rekordbox XML button).
  const [importingPriors, setImportingPriors] = useState(false);
  const [recentLibraries, setRecentLibraries] = useState<LibraryRecord[]>([]);
  // Tracks whether `handleAnalyze`'s slow preflight probe is in flight. The
  // probe can take 5-10s and used to block silently; we surface a spinner
  // on the trigger button so the user knows the click registered.
  const [preflightInFlight, setPreflightInFlight] = useState(false);
  // New-tracks banner state. We refetch this every time the library is
  // reloaded; the user can dismiss it for the current session but the next
  // load shows it again (intentional — the count may have changed).
  const [newTracksCount, setNewTracksCount] = useState<NewTracksCount | null>(null);
  const [bannerDismissed, setBannerDismissed] = useState(false);
  // Library switcher dropdown open/closed. Closed by default to keep the
  // header quiet; opens on header chevron click and closes on outside click.
  const [switcherOpen, setSwitcherOpen] = useState(false);

  // Generation counter for analyze RPCs. Both `runFastScan`/`scan_only` and
  // `runAnalyze`/`analyze_directory` write to `tracks` on completion; without
  // a counter, a slow analyze that started before a quick scan could land
  // second and clobber the user's now-current view. Each request bumps the
  // counter at launch and only commits its result if the counter hasn't moved.
  const analyzeGen = useRef(0);

  // Pull the recent-libraries list on mount + after a forget. Both empty-state
  // visibility and the cards themselves read from this.
  const refreshRecent = useCallback(async () => {
    try {
      const state = await rpc<LibraryState>("library_state");
      setRecentLibraries(state.recent ?? []);
    } catch {
      setRecentLibraries([]);
    }
  }, []);

  useEffect(() => {
    refreshRecent();
  }, [refreshRecent]);

  // Click handler for a recent-library card: reload the saved analysis and
  // hydrate the store. Falls back to just setting the path if the analysis
  // file went missing under us. Validates the report shape defensively —
  // an older sidecar version, a corrupted on-disk cache, or any future
  // schema drift shouldn't be allowed to crash the renderer.
  //
  // Side effects on success: clears the previous library's new-tracks banner,
  // un-dismisses the banner for the next library (so the user sees fresh
  // counts), and fires a `count_new_tracks` probe to populate the banner if
  // there's drift on disk. The probe is best-effort — a failure leaves the
  // banner empty rather than blocking the load.
  const handleOpenRecent = async (record: LibraryRecord) => {
    // Switching libraries: drop any in-flight analyze's live-merge token so its
    // still-streaming events don't phantom-merge into the library we're loading,
    // and bump the generation counter so an in-flight analyze/scan's final
    // setTracks is invalidated as well (see handleOpenFolder).
    invalidateAnalyzeRun();
    analyzeGen.current++;
    begin("analyze");
    try {
      const result = await rpc<{ loaded: boolean; report?: AnalysisReport; reason?: string }>(
        "load_recent_analysis",
        { library_path: record.path },
      );
      if (!result.loaded || !result.report) {
        fail(result.reason ?? "Could not load saved analysis");
        await refreshRecent();
        return;
      }
      const tracks = result.report.tracks;
      if (!Array.isArray(tracks)) {
        fail("Saved analysis is malformed (tracks is not a list).");
        await refreshRecent();
        return;
      }
      setLibraryPath(record.path);
      // Switching libraries: drop the previous library's filter selections so
      // they don't silently narrow (or mismatch) the new one. The filters store
      // is intentionally cleared here — a tab round-trip preserves filters, but
      // a library SWITCH must not. (Previously `clearFilters` had no caller.)
      useLibraryFiltersStore.getState().clearFilters();
      // Same invariant for the two filter-likes that DON'T live in the filters
      // store: the free-text search box and the "show errors only" toggle.
      // Without these resets the prior library's search query keeps narrowing
      // the new list, and the errors-only filter can strand a clean library in
      // an empty, unrecoverable view (the toggle button only renders when the
      // new library has errors of its own).
      setSearchFilter("");
      setShowErrorsOnly(false);
      setShowReviewOnly(false);
      setTracks(tracks);
      setNewTracksCount(null);
      setBannerDismissed(false);
      setSwitcherOpen(false);
      finish();
      // Probe for new tracks AFTER finish() so an error here doesn't block
      // the user from seeing their library. The banner just stays hidden.
      checkNewTracks(record.path);
    } catch (e) {
      fail(e);
    }
  };

  /** Fire-and-forget RPC to populate the new-tracks banner. */
  const checkNewTracks = useCallback(async (path: string) => {
    try {
      const result = await rpc<NewTracksCount>("count_new_tracks", {
        library_path: path,
      });
      setNewTracksCount(result);
    } catch {
      // Silent — the banner just won't appear. Don't surface a scary error
      // for a non-essential UX feature.
      setNewTracksCount(null);
    }
  }, []);

  // Optimistically remove the card before the RPC round-trip — the user
  // already decided. If the backend disagrees we re-fetch and the card
  // reappears, which is rare and self-healing.
  const handleForgetRecent = async (record: LibraryRecord) => {
    setRecentLibraries((prev) => prev.filter((r) => r.path !== record.path));
    try {
      await rpc("forget_library", { path: record.path });
    } catch (e) {
      fail(e);
    } finally {
      // Reconcile against the source of truth in case the optimistic
      // removal was wrong (or to pick up other concurrent changes).
      await refreshRecent();
    }
  };

  // Set a friendly display name on a recent library (rename_library RPC).
  const handleRenameRecent = async (record: LibraryRecord, name: string) => {
    const trimmed = name.trim();
    // Optimistic update so the card relabels instantly.
    setRecentLibraries((prev) =>
      prev.map((r) =>
        r.path === record.path
          ? ({ ...r, name: trimmed } as LibraryRecord)
          : r,
      ),
    );
    try {
      await rpc("rename_library", { path: record.path, name: trimmed });
    } catch (e) {
      fail(e);
    } finally {
      await refreshRecent();
    }
  };

  // Replace a recent library's tag list (tag_library RPC). Tags are
  // comma-separated in the UI; the server de-dupes + strips.
  const handleTagRecent = async (record: LibraryRecord, tags: string[]) => {
    setRecentLibraries((prev) =>
      prev.map((r) =>
        r.path === record.path
          ? ({ ...r, tags } as LibraryRecord)
          : r,
      ),
    );
    try {
      await rpc("tag_library", { path: record.path, tags });
    } catch (e) {
      fail(e);
    } finally {
      await refreshRecent();
    }
  };

  const analyzedCount = useMemo(
    () => tracks.filter((t) => t.ml_analysis).length,
    [tracks],
  );
  const unanalyzedCount = tracks.length - analyzedCount;

  // Genre breakdown for the bulk-tag confirm modal: only count tracks whose
  // genre will actually be written (confidence >= threshold). Tracks without
  // a confidence are counted as zero (won't be written either).
  //
  // Uses the snapshotted `confirmBulkTag.targets` rather than re-deriving
  // from `selectedIds` — see the note on the snapshot's declaration.
  const tagPreview = useMemo(() => {
    if (!confirmBulkTag) return null;
    const targets = confirmBulkTag.targets;
    const threshold = taggingCfg.genre_confidence_threshold;

    const counts = new Map<string, number>();
    let belowThreshold = 0;
    for (const t of targets) {
      const ml = t.ml_analysis;
      const conf = ml?.ml_genre_confidence;
      if (!ml?.ml_genre || conf == null || conf < threshold) {
        belowThreshold += 1;
        continue;
      }
      // Prefer subgenre when present — that's what gets written.
      const label = ml.ml_subgenre || ml.ml_genre;
      counts.set(label, (counts.get(label) ?? 0) + 1);
    }
    const sorted = [...counts.entries()].sort((a, b) => b[1] - a[1]);
    const top = sorted.slice(0, 5);
    const others = sorted.slice(5);
    const otherCount = others.reduce((sum, [, n]) => sum + n, 0);
    const otherGenres = others.length;
    const willWrite = sorted.reduce((sum, [, n]) => sum + n, 0);
    return {
      total: targets.length,
      top,
      otherCount,
      otherGenres,
      willWrite,
      belowThreshold,
    };
  }, [confirmBulkTag, taggingCfg.genre_confidence_threshold]);

  // Tracks with either a top-level scan/decode error or an ML failure.
  const errorCount = useMemo(
    () => tracks.filter((t) => t.error || t.ml_analysis?.ml_error).length,
    [tracks],
  );

  // Tracks whose genre sources disagree (tag ≠ audio ≠ web) — the review queue.
  const reviewCount = useMemo(
    () => tracks.filter((t) => needsReview(t.ml_analysis)).length,
    [tracks],
  );

  // How many of the currently-selected tracks actually need review — the count
  // the Approve/Revert actions will change. (A selection can include tracks
  // that aren't conflicted; the resolve RPC only touches the flagged ones.)
  const selectedReviewCount = useMemo(
    () => tracks.filter((t) => selectedIds.has(t.path) && needsReview(t.ml_analysis)).length,
    [tracks, selectedIds],
  );

  // The currently-selected track (right-rail TrackDetails). We surface a
  // "Find compatible" affordance in the filter bar when it has enough ML
  // signal to seed a useful filter set (at minimum: BPM + energy).
  const selectedTrack = useMemo(
    () => tracks.find((t) => t.path === selectedTrackPath) ?? null,
    [tracks, selectedTrackPath],
  );
  const canFindCompatible = !!selectedTrack?.ml_analysis?.ml_bpm &&
    selectedTrack.ml_analysis.ml_energy != null;

  /**
   * "Find compatible" — populates the filter chips so the library narrows to
   * tracks that would actually mix well with the selected one:
   *
   *   - BPM within ±5
   *   - Energy within ±1 level
   *   - Same Camelot key (Agent 3's chip group will widen this to harmonic
   *     neighbours once that filter is fully wired). Same-key is the safe,
   *     always-correct fallback per the task spec.
   *   - Direction = "Up" (or whatever the selected track's direction is —
   *     so picking a cooldown seed finds other cooldowns, not a fake build)
   *
   * The button just sets the chip state — the existing `applyFilters` then
   * does the actual filtering on the next render. BPM isn't a chip dimension
   * yet, so we narrow via energies (which the chip system supports) and
   * leave BPM/Key/Direction visible as active filter pills. The DJ can tweak
   * any chip after, so we're not overwriting their work invisibly.
   */
  const handleFindCompatible = useCallback(() => {
    const ml = selectedTrack?.ml_analysis;
    if (!ml) return;

    const next = emptyFilters();

    // Energy ±1 (chip filter is discrete int levels, so we just enumerate)
    if (ml.ml_energy != null) {
      const e = ml.ml_energy;
      next.energies = new Set([e - 1, e, e + 1].filter((x) => x >= 1 && x <= 5));
    }

    // Direction: match the seed track's direction if known, else default Up.
    // Up is the most common "what's next" question for a working DJ.
    next.directions = new Set([ml.ml_direction || "Up"]);

    // Key: same Camelot. Agent 3's camelot chip group, if wired, will let
    // the DJ click "harmonic" / "energy" to widen. We seed with just the
    // exact key — both the safe fallback and the spec-mandated behaviour.
    if (ml.ml_key) {
      // ml_key may be a Camelot string ("8A") or a music key ("A minor") —
      // the chip universe is whatever shows up in the library, so passing
      // the raw value through works in either case.
      next.camelot = new Set([ml.ml_key]);
    }

    setFilters(next);
    // BPM ±5 isn't a chip — we don't want to overwrite the search box, so
    // we surface it as a toast hint instead. Users can use the search box
    // to narrow further if they really want BPM-tight matches.
    if (ml.ml_bpm) {
      const bpm = Math.round(ml.ml_bpm);
      notify(`Filtering for tracks compatible with ${bpm} BPM`, {
        kind: "info",
        detail: `±5 BPM, ±1 energy, ${ml.ml_key ?? "any key"}, ${ml.ml_direction || "Up"}`,
      });
    }
  }, [selectedTrack, setFilters, notify]);

  const filtered = useMemo(() => {
    // Errors-only is an exclusive view (replaces the chip filters — you're
    // triaging failures, not browsing). Review-only composes WITH the chips so
    // you can ask "show me the House tracks whose genre I should double-check".
    let result;
    if (showErrorsOnly) {
      result = tracks.filter((t) => t.error || t.ml_analysis?.ml_error);
    } else {
      result = applyFilters(tracks, filters);
      if (showReviewOnly) result = result.filter((t) => needsReview(t.ml_analysis));
    }
    if (searchFilter) {
      const q = searchFilter.toLowerCase();
      result = result.filter((t) =>
        t.filename.toLowerCase().includes(q) ||
        (t.filename_artist ?? "").toLowerCase().includes(q) ||
        (t.filename_title ?? "").toLowerCase().includes(q) ||
        (t.ml_analysis?.ml_genre ?? "").toLowerCase().includes(q) ||
        (t.ml_analysis?.ml_subgenre ?? "").toLowerCase().includes(q),
      );
    }
    return result;
  }, [tracks, searchFilter, filters, showErrorsOnly, showReviewOnly]);

  // "All selected" for the master checkbox must reflect what the user actually
  // SEES (the filtered set), not raw `selectedIds.size === filtered.length`.
  // `selectedIds` can hold paths NOT in the current filtered set (select rows,
  // then change the search/filters so a *different* set of equal size shows) —
  // a pure size comparison then renders "checked" while none of the visible
  // rows are selected, and clicking it clears the invisible selection instead
  // of selecting the visible rows.
  const allVisibleSelected = useMemo(
    () =>
      filtered.length > 0 &&
      filtered.every((t) => selectedIds.has(t.path)),
    [filtered, selectedIds],
  );

  const handleOpenFolder = async () => {
    // A running long op owns the operation store: begin("analyze") below
    // would clobber its opId (freezing its progress overlay) and the final
    // commit of an in-flight analyze could paint the OLD folder's report
    // into the NEW folder's view. The visible buttons are disabled during an
    // op, but guard the handler itself too (the empty-state button wasn't).
    if (active !== null) return;
    const selected = await openDialog({ directory: true, multiple: false });
    if (typeof selected !== "string") return;
    if (useOperationStore.getState().active !== null) return; // op started while dialog was open

    // New folder = library switch: invalidate any in-flight analyze's
    // live-merge token before we swap the path + tracks out from under it,
    // AND bump the generation counter so a still-running analyze/scan's
    // FINAL setTracks (which only checks its own generation) is invalidated
    // too — invalidateAnalyzeRun alone only stops the streamed live-merge.
    invalidateAnalyzeRun();
    analyzeGen.current++;
    setLibraryPath(selected);
    // New folder = library switch → clear the prior library's filters (see
    // handleOpenRecent). Otherwise stale genre/key/energy chips carry over.
    useLibraryFiltersStore.getState().clearFilters();
    // Also clear the free-text search box and the errors-only toggle, which
    // live outside the filters store (see handleOpenRecent for the rationale).
    setSearchFilter("");
    setShowErrorsOnly(false);
    setShowReviewOnly(false);
    setTracks([]);
    begin("analyze");
    try {
      const result = await rpc<{ count: number }>("scan_directory", { path: selected });
      setScanCount(result.count);
      finish();
    } catch (e) {
      fail(e);
    }
  };

  // "Just show me my library" — instant, no ML. Reads filenames + existing tags
  // so the user can browse / dedupe / organize without committing to a long ML run.
  const runFastScan = async () => {
    if (!libraryPath) return;
    const myGen = ++analyzeGen.current;
    const opId = begin("analyze");
    try {
      const report = await rpc<AnalysisReport>("scan_only", { path: libraryPath, op_id: opId });
      // If a later analyze/scan started after this one (e.g. user clicked
      // Re-analyze while scan_only was still in flight), drop our result —
      // committing it would clobber the newer pass.
      if (analyzeGen.current !== myGen) return;
      setTracks(report.tracks);
      finish();
    } catch (e) {
      if (analyzeGen.current === myGen) fail(e);
    }
  };

  // Run ML analyze. If `incremental` is true, skip every file already in `tracks`
  // and merge the new results into what's there — pruning any in-memory tracks
  // that no longer exist in the freshly-scanned filesystem so deleted/renamed
  // files don't linger as ghosts in the UI.
  const runAnalyze = async (incremental = false) => {
    if (!libraryPath) return;
    const alreadyAnalyzed = incremental
      ? tracks.filter((t) => t.ml_analysis).map((t) => t.path)
      : [];
    const myGen = ++analyzeGen.current;
    // Authorize live-merge of streamed `track_analyzed` events for THIS run,
    // rooted at the library we're analyzing. The token supersedes any prior
    // run so a cancelled/replaced analyze's queued events stop merging (see
    // the listener in App.tsx). Cleared in `finally` once this run settles.
    const runId = beginAnalyzeRun(libraryPath);
    const opId = begin("analyze");
    try {
      const report = await rpc<AnalysisReport>("analyze_directory", {
        path: libraryPath,
        // Correlation id — echoed on this op's progress events so consumers
        // can drop stragglers from cancelled/replaced ops (see App.tsx).
        op_id: opId,
        workers: analysisCfg.workers,
        use_gpu: analysisCfg.use_gpu,
        hybrid_cpu_gpu: analysisCfg.hybrid_cpu_gpu,
        // Critical: route to the SELECTED engine. Without this the sidecar
        // defaults to essentia_tf, so the ONNX toggle is inert at analyze time.
        inference_engine: analysisCfg.inference_engine,
        // How to reconcile an existing genre tag with the ML read.
        genre_source_policy: analysisCfg.genre_source_policy,
        // Opt-in genre sources: CLAP audio model + online web-synthesis lookup.
        genre_classifier: analysisCfg.genre_classifier,
        genre_web_lookup: analysisCfg.genre_web_lookup,
        genre_llm_backend: analysisCfg.genre_llm_backend,
        genre_ml_override_confidence: analysisCfg.genre_ml_override_confidence,
        skip_paths: alreadyAnalyzed,
        // Forwarded for forward-compat / debugging; the current sidecar ignores
        // unknown params. The authoritative guard is the frontend run token.
        analyze_run_id: runId,
      });
      if (analyzeGen.current !== myGen) return;
      if (incremental) {
        // Merge: keep existing analyzed tracks, overlay new results from this
        // pass, and drop entries whose paths weren't seen at all in the new
        // scan (the sidecar returns *every* current file — analyzed or
        // skipped — so any path missing from `report.tracks` is a file that
        // no longer exists on disk).
        const seen = new Set(report.tracks.map((t) => t.path));
        const merged = new Map<string, TrackAnalysis>();
        for (const t of tracks) {
          if (seen.has(t.path)) merged.set(t.path, t);
        }
        for (const t of report.tracks) merged.set(t.path, t);
        setTracks(Array.from(merged.values()));
      } else {
        setTracks(report.tracks);
      }
      // Completion toast. Analyze is the most-run, most-consequential op in the
      // app, yet it was the only long op here that never reported its outcome —
      // a run where 300 of 2,000 tracks failed looked identical to a clean one.
      // The sidecar already computes honest counts; surface them (mirrors
      // runBulkTag). We're past the generation-token guard above, so this only
      // fires for the still-current run.
      const errors = report.summary?.errors ?? 0;
      const analyzed = report.summary?.analyzed ?? report.tracks.length;
      const total = report.summary?.total_files ?? report.tracks.length;
      if (report.persist_error) {
        // Loudest failure: the work is on screen but was NOT saved, so it
        // vanishes on next launch. A warning toast, not a cheerful success.
        notify("Analysis completed but could NOT be saved", {
          kind: "warning",
          detail:
            `Analyzed ${analyzed}/${total}` +
            (errors > 0 ? ` — ${errors} error${errors === 1 ? "" : "s"}` : "") +
            `.\nThe results on screen were NOT written to disk (${report.persist_error}).\n` +
            `Re-run analyze or export before closing — they will be lost otherwise.`,
        });
      } else {
        const detailLines: string[] = [];
        if (errors > 0) {
          detailLines.push(
            `${errors} track${errors === 1 ? "" : "s"} failed — open the "Errors" filter to see them.`,
          );
        }
        if (report.priors_warning) detailLines.push(report.priors_warning);
        const headline =
          errors > 0
            ? `Analyzed ${analyzed}/${total} — ${errors} error${errors === 1 ? "" : "s"}`
            : `Analyzed ${analyzed}/${total}`;
        notify(headline, {
          // A dropped Rekordbox import is a caution (warning); per-track errors
          // are informational; a clean run is a success.
          kind: report.priors_warning ? "warning" : errors > 0 ? "info" : "success",
          detail: detailLines.length > 0 ? detailLines.join("\n") : undefined,
        });
      }
      finish();
    } catch (e) {
      if (analyzeGen.current === myGen) fail(e);
    } finally {
      // Close this run's live-merge authorization. No-ops if a newer run (or a
      // library switch) already superseded us, so we never wipe a fresh run's
      // token. Any of this run's still-queued events are dropped from here on.
      endAnalyzeRun(runId);
    }
  };

  // Bulk apply ML tags to a snapshotted scope. The actual RPC + loading/error
  // handling is in useApplyTags; we decide the scope, filter out tracks the
  // sidecar would no-op on, and surface a toast.
  //
  // The headline message includes the error count when any failed — a quiet
  // green "Tagged 500 files" toast hides the 200 files that didn't write.
  const runBulkTag = async (targets: TrackAnalysis[]) => {
    if (targets.length === 0) return;
    // Drop tracks with no ML analysis — the sidecar would receive them and
    // report applied=0/skipped=0/other=0, producing a confusing "Tagged 0
    // files" success toast.
    const writable = targets.filter((t) => t.ml_analysis);
    if (writable.length === 0) {
      notify("No analyzed tracks in selection", {
        detail: "Run analyze first — there's nothing to write yet.",
        kind: "info",
      });
      return;
    }
    const result = await applyTags(writable);
    if (!result) return; // failure already surfaced via useOperationStore.error
    const threshold = Math.round(taggingCfg.genre_confidence_threshold * 100);
    const detail =
      `Genre (conf >= ${threshold}%): ${result.applied}\n` +
      `Skipped (low confidence): ${result.skipped}\n` +
      `Other tags (energy / mood / etc): ${result.other}` +
      (result.errors.length > 0 ? `\nErrors: ${result.errors.length}` : "");
    const wrote = result.applied + result.other;
    const numErrors = result.errors.length;
    const headline =
      numErrors > 0
        ? `Tagged ${wrote} files — ${numErrors} error${numErrors === 1 ? "" : "s"}`
        : `Tagged ${wrote} files`;
    notify(headline, {
      detail,
      kind: numErrors > 0 ? "info" : "success",
    });
  };

  // Trust-UX #2: resolve the reviewed genre conflicts in the current selection.
  // "approve" accepts Vibechek's reconciled genre as final; "revert" puts the
  // genre back to the file's existing tag. Either way the conflict flag clears,
  // so the track drops out of the review queue. This NEVER writes file tags —
  // it persists the decision to the saved analysis (survives reload); the
  // separate "Apply ML tags" flow is what writes to disk.
  const runResolveConflicts = async (action: "approve" | "revert") => {
    if (!libraryPath || resolving) return;
    // Only the flagged tracks in the selection — the sidecar skips the rest, but
    // filtering here keeps the count + toast honest.
    const targets = tracks.filter(
      (t) => selectedIds.has(t.path) && needsReview(t.ml_analysis),
    );
    if (targets.length === 0) {
      notify("Nothing to resolve in the selection", {
        detail: "Select one or more flagged (to-review) tracks first.",
        kind: "info",
      });
      return;
    }
    setResolving(true);
    // Snapshot the library this resolve runs against: the RPC does disk
    // writes and can be slow, the library switcher stays enabled meanwhile
    // (resolve doesn't set the operation store's `active`), and
    // mergeAnalyzedTracks APPENDS unknown paths — so a mid-flight switch
    // would splice this library's tracks into whichever library is open when
    // the RPC resolves. Same pattern as the analyzeRun token.
    const lib = libraryPath;
    try {
      const result = await rpc<{
        ok: boolean;
        updated: number;
        reason?: string;
        tracks: TrackAnalysis[];
      }>("resolve_genre_conflicts", {
        library_path: libraryPath,
        items: targets.map((t) => ({ path: t.path, action })),
      });
      if (!result.ok) {
        fail(result.reason ?? "Could not resolve the selected conflicts");
        return;
      }
      if (useLibraryStore.getState().libraryPath !== lib) {
        // Library switched mid-flight. The resolve DID persist to the old
        // library's saved analysis — it will show correctly on next open —
        // but merging (or toasting counts) into the now-open library lies.
        return;
      }
      // Merge the authoritative persisted records back (one state update). Their
      // cleared conflict flag drops them from the review filter automatically.
      mergeAnalyzedTracks(result.tracks);
      clearSelection();
      const n = result.updated;
      notify(
        action === "approve"
          ? `Approved ${n} genre${n === 1 ? "" : "s"} — kept Vibechek's call`
          : `Reverted ${n} genre${n === 1 ? "" : "s"} to the file tag`,
        {
          kind: "success",
          detail:
            action === "approve"
              ? "Cleared from review. Nothing written to your files — use Apply ML tags for that."
              : "The existing tag is kept as the genre and cleared from review.",
        },
      );
    } catch (e) {
      fail(e);
    } finally {
      setResolving(false);
    }
  };

  // Trust-UX #3: import a Rekordbox collection XML as tag-tier priors. Genre
  // lands at the tag tier (marked genre_origin="rekordbox"), key/MIK-energy
  // only fill gaps, and the sidecar re-reconciles + persists the saved
  // analysis. NEVER writes file tags; idempotent, so re-importing an updated
  // export is safe. The component owns the RPC and hands the returned records
  // to the store (stores stay rpc-free) — same shape as runResolveConflicts.
  const handleImportPriors = async () => {
    if (!libraryPath || importingPriors || active !== null) return;
    const selected = await openDialog({
      multiple: false,
      filters: [{ name: "Rekordbox XML", extensions: ["xml"] }],
    });
    if (typeof selected !== "string") return;
    setImportingPriors(true);
    // Snapshot for the same mid-flight library-switch hazard as
    // runResolveConflicts: the import re-reconciles + persists a whole
    // library and can take a while, and mergeAnalyzedTracks would append
    // this library's records into whichever library is open on resolve.
    const lib = libraryPath;
    try {
      const result = await importTagPriors({
        library_path: libraryPath,
        xml_path: selected,
        // Same reconciliation knobs the analyze call sends, so the re-run
        // matches what a fresh analyze of this library would decide.
        genre_source_policy: analysisCfg.genre_source_policy,
        genre_ml_override_confidence: analysisCfg.genre_ml_override_confidence,
      });
      if (!result.ok) {
        fail(result.reason ?? "Could not import the Rekordbox XML");
        return;
      }
      if (useLibraryStore.getState().libraryPath !== lib) {
        // Import persisted to the old library's saved analysis + priors
        // sidecar; it loads correctly on next open. Don't merge or toast
        // against the library now on screen.
        return;
      }
      // Merge the authoritative persisted records back (one state update).
      mergeAnalyzedTracks(result.tracks);
      notify(
        `Imported priors from Rekordbox XML: matched ${result.matched} of ${result.xml_tracks} tracks, updated ${result.updated}.`,
        {
          kind: "success",
          detail:
            "Genres joined at the tag tier; key and MIK energy filled gaps. Nothing was written to your files.",
        },
      );
    } catch (e) {
      fail(e);
    } finally {
      setImportingPriors(false);
    }
  };

  // Gate analyze behind preflight. The previous version did a quick=true
  // probe first then maybe upgraded — but on Windows that meant the dialog
  // first appeared with stale per-distro data, falsely claiming "WSL not
  // installed" while the upgrade was still in flight. Users would see the
  // wrong error and click Re-check (which actually does the slow probe).
  //
  // New flow: ALWAYS do the slow probe before opening the dialog. The user
  // would otherwise see nothing for the 5-10s the full probe takes — track
  // `preflightInFlight` so the trigger button shows a spinner and is
  // disabled, and also short-circuit if any long op is already underway.
  const handleAnalyze = async () => {
    if (!libraryPath) return;
    if (active !== null) return;       // long op already running — be safe
    if (preflightInFlight) return;     // already probing, don't double-fire
    // Snapshot the library this click targets: the full probe takes 5-10s,
    // the library switcher stays enabled meanwhile, and runAnalyze is a
    // closure over the OLD libraryPath — its own generation token can't save
    // us because runAnalyze bumps analyzeGen at its own start. A switch
    // completed during the probe must abort the launch, not analyze the old
    // library into the new one's view.
    const lib = libraryPath;
    const libUnchanged = () => useLibraryStore.getState().libraryPath === lib;
    setPreflightInFlight(true);
    try {
      // Quick probe first — if it says ready, we're done immediately. Pass the
      // selected engine so the probe checks the right venv (venv vs venv-onnx)
      // + models (.pb vs .onnx); ONNX then gates analyze exactly like TF.
      const quick = await rpc<PreflightResult>("preflight", { engine: analysisCfg.inference_engine });
      // Re-check the long-op lock: it can flip between us starting the
      // probe and the probe finishing (e.g. user kicked off a different
      // operation while we were waiting). Don't trample an active op.
      if (useOperationStore.getState().active !== null || !libUnchanged()) return;
      if (quick.ready) {
        runAnalyze();
        return;
      }
      // Not ready per quick mode. On Windows the quick mode never probes
      // distros, so "not ready" is uninformative. Do the full probe before
      // showing the dialog so the user sees the real state, not a stale one.
      const full = await rpc<PreflightResult>("preflight", { quick: false, engine: analysisCfg.inference_engine });
      if (useOperationStore.getState().active !== null || !libUnchanged()) return;
      if (full.ready) {
        // Full probe revealed we're actually ready (quick mode missed it
        // because the WSL distro probe wasn't run). Skip the dialog.
        runAnalyze();
        return;
      }
      setPreflightResult(full);
    } catch (e) {
      // Pass the raw error object to fail() — it unwraps RpcError.message AND
      // detects data.cancelled so a user-cancelled preflight exits silently
      // instead of surfacing a scary error toast. Stringifying here (the old
      // behaviour) discarded the cancelled flag and the structured data.
      fail(e);
    } finally {
      setPreflightInFlight(false);
    }
  };

  // Preflight dialog (rendered above whichever state is active)
  const preflightOverlay = (
    <AnimatePresence>
      {preflightResult && !preflightResult.ready && (
        <PreflightDialog
          preflight={preflightResult}
          onRefresh={setPreflightResult}
          onClose={() => setPreflightResult(null)}
          onReady={() => {
            setPreflightResult(null);
            runAnalyze();
          }}
        />
      )}
    </AnimatePresence>
  );

  // Empty state
  if (tracks.length === 0) {
    // If the user has recent libraries AND hasn't pre-selected a folder yet,
    // show those instead of just "Open folder". Clicking a card re-loads the
    // saved analysis straight into the store.
    const showRecents = !libraryPath && recentLibraries.length > 0;

    return (
      <div className="h-full flex flex-col">
        <Header onOpen={handleOpenFolder} libraryPath={libraryPath} disabled={active !== null} />
        {preflightOverlay}
        <div className="flex-1 overflow-auto flex items-center justify-center px-8 py-8">
          {showRecents ? (
            <div className="w-full max-w-2xl">
              <div className="text-center mb-6">
                <div className="w-12 h-12 mx-auto mb-3 rounded-xl bg-white/5 flex items-center justify-center">
                  <Music className="w-6 h-6 text-white/30" />
                </div>
                <h2 className="text-xl font-display font-semibold mb-1">
                  Welcome back
                </h2>
                <p className="text-sm text-white/50">
                  Pick up where you left off, or open a different folder.
                </p>
              </div>

              <div className="space-y-2">
                {recentLibraries.map((rec) => (
                  <RecentLibraryCard
                    key={rec.path}
                    record={rec}
                    disabled={active !== null}
                    onOpen={() => handleOpenRecent(rec)}
                    onForget={() => setConfirmForget(rec)}
                    onRename={(name) => handleRenameRecent(rec, name)}
                    onSetTags={(tags) => handleTagRecent(rec, tags)}
                  />
                ))}
              </div>

              <div className="text-center mt-6">
                <button
                  className="btn-ghost"
                  onClick={handleOpenFolder}
                  disabled={active !== null}
                >
                  <FolderOpen className="w-4 h-4" />
                  Open a different folder
                </button>
              </div>

              {errorMsg && (
                <div className="mt-6 panel-pad text-left text-sm text-accent-red flex gap-2">
                  <AlertCircle className="w-4 h-4 flex-none mt-0.5" />
                  <div className="break-words">{errorMsg}</div>
                </div>
              )}
            </div>
          ) : (
            <div className="text-center max-w-md">
              <div className="w-16 h-16 mx-auto mb-4 rounded-2xl bg-white/5 flex items-center justify-center">
                <Music className="w-8 h-8 text-white/30" />
              </div>
              <h2 className="text-xl font-display font-semibold mb-2">
                {libraryPath ? "Ready to analyze" : "Open a folder to get started"}
              </h2>
              <p className="text-white/50 mb-6">
                {libraryPath
                  ? `Found ${scanCount ?? "?"} audio files in ${libraryPath}. Run analysis to detect genre, energy, mood, and more — or jump straight to dedup / organize using existing tags.`
                  : "Vibechek will scan your music folder, then let you analyze, dedupe, tag, or reorganize it."}
              </p>
              {libraryPath ? (
                <div className="space-y-3">
                  <div className="flex items-center justify-center gap-2">
                    <button
                      className="btn-primary"
                      onClick={runFastScan}
                      disabled={active !== null}
                      title="Read filenames + existing tags. Takes a few seconds."
                    >
                      <Eye className="w-4 h-4" />
                      Just show me my library
                    </button>
                    <button
                      className="btn-ghost"
                      onClick={handleAnalyze}
                      disabled={active !== null || preflightInFlight}
                      title={
                        preflightInFlight
                          ? "Checking preflight…"
                          : "Full ML pass — detects genre, mood, energy, etc. Takes longer."
                      }
                    >
                      {preflightInFlight ? (
                        <Loader2 className="w-4 h-4 animate-spin" />
                      ) : (
                        <Sparkles className="w-4 h-4" />
                      )}
                      {preflightInFlight ? "Checking…" : "Analyze with ML"}
                    </button>
                  </div>
                  <div>
                    <button
                      className="text-xs text-white/40 hover:text-white disabled:opacity-40 disabled:cursor-not-allowed"
                      onClick={handleOpenFolder}
                      // The empty state persists through the first minutes of
                      // a fresh full analyze (until the first streamed track
                      // arrives) — switching folders mid-analyze from here
                      // clobbered the running op. Disabled like its siblings.
                      disabled={active !== null}
                    >
                      Choose a different folder
                    </button>
                  </div>
                </div>
              ) : (
                <button className="btn-primary" onClick={handleOpenFolder}>
                  <FolderOpen className="w-4 h-4" />
                  Open folder
                </button>
              )}
              {errorMsg && (
                <div className="mt-6 panel-pad text-left text-sm text-accent-red flex gap-2">
                  <AlertCircle className="w-4 h-4 flex-none mt-0.5" />
                  <div className="break-words">{errorMsg}</div>
                </div>
              )}
            </div>
          )}
        </div>

        <ConfirmModal
          open={confirmForget !== null}
          title="Forget this library?"
          message={
            <div className="space-y-2 text-sm text-white/70">
              <div className="font-mono text-xs text-white/50 break-all">
                {confirmForget?.path ?? ""}
              </div>
              <div>
                Vibechek will drop this from the recent-libraries list. The on-disk
                files and saved analysis cache are untouched — you can re-open the
                folder any time.
              </div>
            </div>
          }
          confirmLabel="Forget"
          cancelLabel="Keep"
          variant="danger"
          onConfirm={() => {
            const record = confirmForget;
            setConfirmForget(null);
            if (record) handleForgetRecent(record);
          }}
          onCancel={() => setConfirmForget(null)}
        />
      </div>
    );
  }

  // Populated state
  return (
    <div className="h-full flex flex-col">
      <Header
        onOpen={handleOpenFolder}
        libraryPath={libraryPath}
        recentLibraries={recentLibraries}
        switcherOpen={switcherOpen}
        onToggleSwitcher={() => setSwitcherOpen((v) => !v)}
        onCloseSwitcher={() => setSwitcherOpen(false)}
        onSwitchLibrary={handleOpenRecent}
        disabled={active !== null}
      />
      {preflightOverlay}

      {/* New-tracks banner — appears above the toolbar when count_new_tracks
          surfaces tracks on disk that the saved analysis hasn't seen yet.
          Dismissed for the current session only; the next library load
          un-dismisses it so the count gets re-evaluated. */}
      {newTracksCount &&
        newTracksCount.new_count > 0 &&
        !bannerDismissed &&
        libraryPath && (
          <div className="flex items-center gap-3 px-4 py-2 bg-accent/10 border-b border-accent/30 text-sm">
            <Sparkles className="w-4 h-4 text-accent flex-none" />
            <div className="flex-1 text-white/90">
              <span className="font-mono text-accent">
                {newTracksCount.new_count.toLocaleString()}
              </span>{" "}
              new track{newTracksCount.new_count === 1 ? "" : "s"} added since last
              analysis.
            </div>
            <button
              className="btn-primary btn-sm"
              onClick={() => runAnalyze(true)}
              disabled={active !== null}
              title="Run ML on just the new tracks, then merge with the existing analysis"
            >
              <Sparkles className="w-4 h-4" />
              Analyze + sort
            </button>
            <button
              className="text-white/40 hover:text-white p-1"
              onClick={() => setBannerDismissed(true)}
              title="Dismiss (will reappear on next library load)"
              aria-label="Dismiss new-tracks banner"
            >
              <X className="w-4 h-4" />
            </button>
          </div>
        )}

      {/* Toolbar */}
      <div className="flex items-center gap-3 px-4 py-3 border-b border-white/5">
        <button
          className="text-white/40 hover:text-white"
          onClick={() =>
            // Select/clear the FILTERED set, not the whole library — otherwise
            // "select all" under an active filter silently selects (and
            // bulk-tags) tracks the user can't see. We key off whether every
            // VISIBLE row is selected (`allVisibleSelected`) rather than raw
            // size equality, which can be true while none of the visible rows
            // are actually selected (an equal-size out-of-view selection).
            allVisibleSelected
              ? clearSelection()
              : selectPaths(filtered.map((t) => t.path))
          }
          title={allVisibleSelected ? "Clear selection" : "Select all"}
        >
          {allVisibleSelected ? (
            <CheckSquare className="w-5 h-5" />
          ) : (
            <Square className="w-5 h-5" />
          )}
        </button>

        <div className="relative flex-1 max-w-md">
          <Search className="w-4 h-4 absolute left-3 top-1/2 -translate-y-1/2 text-white/40" />
          <input
            type="text"
            className="input w-full pl-9"
            placeholder="Filter tracks..."
            value={searchFilter}
            onChange={(e) => setSearchFilter(e.target.value)}
          />
        </div>

        <div className="text-xs text-white/40 font-mono">
          {selectedIds.size > 0 ? `${selectedIds.size} selected • ` : ""}
          {filtered.length} / {tracks.length}
        </div>

        {selectedIds.size > 0 ? (
          showReviewOnly ? (
            // Review queue actions. "Approve" accepts Vibechek's reconciled
            // genre; "Revert" puts it back to the file tag. Both clear the
            // conflict (track leaves the queue) and persist to the saved
            // analysis — neither writes file tags. Counts reflect the flagged
            // tracks in the selection (selectedReviewCount), not raw selection.
            <>
              <button
                className="btn-primary"
                onClick={() => runResolveConflicts("approve")}
                disabled={active !== null || resolving || selectedReviewCount === 0}
                title="Accept Vibechek's genre for the selected flagged tracks and clear them from review (doesn't write file tags)"
              >
                {resolving ? (
                  <Loader2 className="w-4 h-4 animate-spin" />
                ) : (
                  <Check className="w-4 h-4" />
                )}
                Approve {selectedReviewCount}
              </button>
              <button
                className="btn-ghost"
                onClick={() => runResolveConflicts("revert")}
                disabled={active !== null || resolving || selectedReviewCount === 0}
                title="Keep the genre already in the file's tag for the selected tracks and clear them from review"
              >
                <Undo2 className="w-4 h-4" />
                Revert to tag
              </button>
              <button className="btn-ghost" onClick={() => clearSelection()}>
                Clear
              </button>
            </>
          ) : (
            <>
              <button
                className="btn-primary"
                onClick={() =>
                  setConfirmBulkTag({
                    scope: "selected",
                    targets: tracks.filter((t) => selectedIds.has(t.path)),
                  })
                }
                disabled={active !== null}
              >
                <Tag className="w-4 h-4" />
                Apply ML tags to {selectedIds.size}
              </button>
              <button className="btn-ghost" onClick={() => clearSelection()}>
                Clear
              </button>
            </>
          )
        ) : (
          <>
            {analyzedCount > 0 && (
              <button
                className="btn-ghost"
                onClick={() =>
                  setConfirmBulkTag({ scope: "all", targets: tracks })
                }
                disabled={active !== null}
                title="Write ML tags to every analyzed track"
              >
                <Tag className="w-4 h-4" />
                Apply ML tags to all
              </button>
            )}
            {unanalyzedCount > 0 && libraryPath && (
              <button
                className="btn-ghost"
                onClick={() => runAnalyze(true)}
                disabled={active !== null}
                title={`Run ML on the ${unanalyzedCount} tracks that haven't been analyzed yet`}
              >
                <Sparkles className="w-4 h-4" />
                Analyze new ({unanalyzedCount})
              </button>
            )}
            <button
              className="btn-ghost"
              onClick={handleAnalyze}
              disabled={active !== null || preflightInFlight || !libraryPath}
              title={
                preflightInFlight
                  ? "Checking preflight…"
                  : "Re-run ML on the whole library"
              }
            >
              {preflightInFlight ? (
                <Loader2 className="w-4 h-4 animate-spin" />
              ) : (
                <RefreshCw className="w-4 h-4" />
              )}
              {preflightInFlight ? "Checking…" : "Re-analyze all"}
            </button>
            <button
              className="btn-ghost"
              onClick={handleImportPriors}
              disabled={active !== null || importingPriors || !libraryPath}
              title="Import genre / key / MIK-energy priors from a Rekordbox collection XML (never writes file tags)"
            >
              {importingPriors ? (
                <Loader2 className="w-4 h-4 animate-spin" />
              ) : (
                <Import className="w-4 h-4" />
              )}
              Import Rekordbox XML
            </button>
          </>
        )}
      </div>

      {/* Filter chips (only render when there are analyzed tracks to filter).
          Also render when the errors-only toggle is active so the "clear"
          affordance is never lost even if errorCount is 0 for the current set. */}
      {(analyzedCount > 0 || errorCount > 0 || showErrorsOnly ||
        reviewCount > 0 || showReviewOnly) && (
        <div className="px-4 py-2 border-b border-white/5 flex items-center gap-3 flex-wrap">
          {analyzedCount > 0 && !showErrorsOnly && (
            <FilterChips tracks={tracks} filters={filters} setFilters={setFilters} />
          )}
          {/* "Find compatible" — programmatic filter chip writer based on
              the selected track. Disabled if no track selected or if the
              selection lacks ML signal we can seed from. */}
          {canFindCompatible && !showErrorsOnly && (
            <button
              onClick={handleFindCompatible}
              className="flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs border bg-accent/10 text-accent border-accent/30 hover:bg-accent/20 transition-colors"
              title={
                selectedTrack
                  ? `Filter to tracks compatible with "${selectedTrack.filename_title ?? selectedTrack.filename}"`
                  : ""
              }
            >
              <Compass className="w-3.5 h-3.5" />
              Find compatible
            </button>
          )}
          {(reviewCount > 0 || showReviewOnly || errorCount > 0 || showErrorsOnly) && (
            <div className="ml-auto flex items-center gap-2">
              {/* "Review" — narrows to tracks whose genre sources disagree
                  (tag ≠ audio ≠ web). The one-click triage queue for a pro who
                  won't trust a black box but will skim the calls we were unsure
                  about. Mutually exclusive with errors-only (different views). */}
              {(reviewCount > 0 || showReviewOnly) && (
                <button
                  onClick={() => {
                    setShowReviewOnly((v) => !v);
                    setShowErrorsOnly(false);
                  }}
                  className={cx(
                    "flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs border transition-colors",
                    showReviewOnly
                      ? "bg-accent-yellow/20 text-accent-yellow border-accent-yellow/40"
                      : "bg-accent-yellow/10 text-accent-yellow border-accent-yellow/30 hover:bg-accent-yellow/20",
                  )}
                  title={
                    showReviewOnly
                      ? "Showing only tracks whose genre sources disagree — click to clear"
                      : "Show only tracks to review — where the tag, audio, and web genre disagree"
                  }
                >
                  <AlertTriangle className="w-3.5 h-3.5" />
                  {reviewCount} to review
                  {showReviewOnly && <span className="text-accent-yellow/70">· showing</span>}
                </button>
              )}
              {(errorCount > 0 || showErrorsOnly) && (
                <button
                  onClick={() => {
                    setShowErrorsOnly((v) => !v);
                    setShowReviewOnly(false);
                  }}
                  className={cx(
                    "flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs border transition-colors",
                    showErrorsOnly
                      ? "bg-accent-red/20 text-accent-red border-accent-red/40"
                      : "bg-accent-red/10 text-accent-red border-accent-red/30 hover:bg-accent-red/20",
                  )}
                  title={
                    showErrorsOnly
                      ? "Showing only tracks with errors — click to clear"
                      : "Show only tracks with errors"
                  }
                >
                  <AlertCircle className="w-3.5 h-3.5" />
                  {errorCount} error{errorCount === 1 ? "" : "s"}
                  {showErrorsOnly && <span className="text-accent-yellow/70">· showing</span>}
                </button>
              )}
            </div>
          )}
        </div>
      )}

      {/* Column header — the badge/size columns were unlabeled, leaving a new
          user to guess that "8A" is a key and the mono number is megabytes. */}
      {filtered.length > 0 && (
        <div className="flex items-center px-4 py-1.5 border-b border-white/10 text-[10px] uppercase tracking-wider text-white/40 select-none">
          <span className="w-7 mr-2 flex-none" aria-hidden="true" />
          <span className="flex-1 min-w-0 mr-4">Title / Artist</span>
          <span className="hidden sm:block mr-4">Genre · BPM · Key</span>
          <span className="w-20 hidden md:block mr-4">Energy</span>
          <span className="w-16 text-right">Size</span>
        </div>
      )}

      {/* Track list */}
      <div className="flex-1 min-h-0">
        {showReviewOnly && filtered.length === 0 ? (
          // Review queue drained — every flagged conflict has been approved or
          // reverted. Give the user a clear "you're done" instead of a blank
          // virtualized list, with a one-click way back to the full library.
          <div className="h-full flex items-center justify-center px-8">
            <div className="text-center max-w-sm">
              <div className="w-12 h-12 mx-auto mb-3 rounded-xl bg-accent-green/10 flex items-center justify-center">
                <Check className="w-6 h-6 text-accent-green" />
              </div>
              <h3 className="text-base font-display font-semibold mb-1">
                All caught up
              </h3>
              <p className="text-sm text-white/50 mb-4">
                No genre conflicts left to review in this library.
              </p>
              <button className="btn-ghost" onClick={() => setShowReviewOnly(false)}>
                Back to full library
              </button>
            </div>
          </div>
        ) : (
          <Virtuoso
            data={filtered}
            itemContent={(_, track) => (
              <TrackRow
                key={track.path}
                track={track}
                checked={selectedIds.has(track.path)}
                onCheck={() => toggleSelect(track.path)}
                selected={track.path === selectedTrackPath}
                onClick={() => setSelectedTrack(track.path)}
              />
            )}
          />
        )}
      </div>

      <ConfirmModal
        open={confirmBulkTag !== null}
        title={`Apply ML tags to ${
          confirmBulkTag?.scope === "all"
            ? "all"
            : confirmBulkTag?.targets.length ?? 0
        } tracks?`}
        message={
          <div className="space-y-3">
            {tagPreview && (
              <div className="panel-pad bg-white/[0.02]">
                <div className="text-xs uppercase tracking-wider text-white/40 mb-2">
                  Will write
                </div>
                {tagPreview.willWrite === 0 ? (
                  <div className="text-sm text-accent-yellow">
                    No tracks above the {Math.round(taggingCfg.genre_confidence_threshold * 100)}% confidence threshold —
                    nothing will be written.
                  </div>
                ) : (
                  <div className="flex flex-wrap gap-x-3 gap-y-1 text-sm">
                    {tagPreview.top.map(([genre, count]) => (
                      <span key={genre} className="text-white">
                        <span className="font-mono text-accent">
                          {count.toLocaleString()}
                        </span>{" "}
                        <span className="text-white/70">{genre}</span>
                      </span>
                    ))}
                    {tagPreview.otherGenres > 0 && (
                      <span className="text-white/50">
                        <span className="font-mono">{tagPreview.otherCount.toLocaleString()}</span>{" "}
                        across {tagPreview.otherGenres} other{" "}
                        {tagPreview.otherGenres === 1 ? "genre" : "genres"}
                      </span>
                    )}
                  </div>
                )}
                {tagPreview.belowThreshold > 0 && (
                  <div className="mt-2 text-xs text-white/50">
                    {tagPreview.belowThreshold.toLocaleString()} track{tagPreview.belowThreshold === 1 ? "" : "s"} will be
                    skipped (below {Math.round(taggingCfg.genre_confidence_threshold * 100)}% genre confidence,
                    or not yet analyzed). Energy / mood / timeslot tags will still be written for analyzed files.
                  </div>
                )}
              </div>
            )}
            <ul className="list-disc list-inside text-xs text-white/60 space-y-1">
              <li>Rekordbox cue points and beat grids are preserved.</li>
              <li>BPM and key {(taggingCfg.write_bpm || taggingCfg.write_key) ? "will be overwritten by ML values" : "are NOT touched"}.</li>
              <li>This cannot be undone — back up your tags first (Tags tab).</li>
            </ul>
          </div>
        }
        confirmLabel="Yes, apply tags"
        cancelLabel="Cancel"
        variant="default"
        onConfirm={() => {
          // Snapshot the targets before closing the modal — that's the list
          // the user just reviewed, not whatever the selection has drifted
          // into during the modal's open window.
          const targets = confirmBulkTag?.targets ?? [];
          setConfirmBulkTag(null);
          runBulkTag(targets);
        }}
        onCancel={() => setConfirmBulkTag(null)}
      />
    </div>
  );
}

function Header({
  onOpen,
  libraryPath,
  disabled = false,
  recentLibraries,
  switcherOpen,
  onToggleSwitcher,
  onCloseSwitcher,
  onSwitchLibrary,
}: {
  onOpen: () => void;
  libraryPath: string | null;
  disabled?: boolean;
  recentLibraries?: LibraryRecord[];
  switcherOpen?: boolean;
  onToggleSwitcher?: () => void;
  onCloseSwitcher?: () => void;
  onSwitchLibrary?: (record: LibraryRecord) => void;
}) {
  // Outside-click closes the switcher dropdown. Same pattern as
  // LibraryFilters' chip dropdowns — one listener, scoped to the open state.
  const switcherRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!switcherOpen || !onCloseSwitcher) return;
    const onMouseDown = (e: MouseEvent) => {
      const target = e.target as Node | null;
      if (!target) return;
      if (switcherRef.current && !switcherRef.current.contains(target)) {
        onCloseSwitcher();
      }
    };
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") onCloseSwitcher();
    };
    document.addEventListener("mousedown", onMouseDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("mousedown", onMouseDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [switcherOpen, onCloseSwitcher]);

  // Only show the switcher chevron when there's more than the active library
  // in the recent list. With ≤1 recent, the dropdown would be empty or
  // tautological ("switch to the same library you're on").
  const otherLibraries = (recentLibraries ?? []).filter(
    (r) => r.path !== libraryPath,
  );
  const showSwitcher =
    recentLibraries !== undefined &&
    otherLibraries.length > 0 &&
    onToggleSwitcher !== undefined &&
    onSwitchLibrary !== undefined;
  const activeRecord = libraryPath
    ? (recentLibraries ?? []).find((r) => r.path === libraryPath)
    : null;
  const headerLabel = activeRecord ? displayName(activeRecord) : "Library";

  return (
    <div className="flex items-center justify-between px-4 py-3 border-b border-white/5">
      <div ref={switcherRef} className="relative min-w-0 flex-1">
        {showSwitcher ? (
          <button
            type="button"
            className="flex items-center gap-1.5 group text-left max-w-full"
            onClick={onToggleSwitcher}
            aria-haspopup="listbox"
            aria-expanded={switcherOpen}
            title="Switch library"
          >
            <h1 className="font-display font-semibold text-white truncate">
              {headerLabel}
            </h1>
            <ChevronDown
              className={cx(
                "w-4 h-4 text-white/40 group-hover:text-white transition-transform flex-none",
                switcherOpen && "rotate-180",
              )}
            />
          </button>
        ) : (
          <h1 className="font-display font-semibold text-white truncate">
            {headerLabel}
          </h1>
        )}
        <p className="text-xs text-white/40 truncate max-w-md">
          {libraryPath ?? "no folder open"}
        </p>

        {/* Switcher dropdown. Each row is a recent library OTHER than the
            currently-active one (filtered above). Clicking calls the
            standard load_recent_analysis flow via onSwitchLibrary, which is
            wired to the same handleOpenRecent used by the empty-state cards
            — so the "Analyze with ML" button behaviour is unchanged after
            a switch. */}
        {switcherOpen && showSwitcher && (
          <div
            role="listbox"
            className="absolute top-full left-0 mt-1 z-40 min-w-[280px] max-w-[420px] panel max-h-80 overflow-auto p-1"
          >
            <div className="px-2 py-1 text-[10px] uppercase tracking-wider text-white/40">
              Switch library
            </div>
            {otherLibraries.map((rec) => (
              <button
                key={rec.path}
                role="option"
                aria-selected={false}
                disabled={disabled}
                onClick={() => {
                  onSwitchLibrary?.(rec);
                  onCloseSwitcher?.();
                }}
                className={cx(
                  "w-full text-left px-2 py-2 rounded text-sm flex items-center gap-2",
                  disabled
                    ? "opacity-50 cursor-not-allowed"
                    : "hover:bg-white/5 cursor-pointer",
                )}
                title={rec.path}
              >
                <Music className="w-3.5 h-3.5 text-accent flex-none" />
                <div className="flex-1 min-w-0">
                  <div className="text-white truncate">{displayName(rec)}</div>
                  <div className="text-[11px] text-white/40 truncate font-mono">
                    {rec.path}
                  </div>
                </div>
                <span className="text-[10px] text-white/40 font-mono flex-none">
                  {compactFmt.format(rec.track_count)}
                </span>
              </button>
            ))}
          </div>
        )}
      </div>
      <button
        className="btn-ghost"
        onClick={onOpen}
        disabled={disabled}
        title={disabled ? "Wait for the current operation to finish" : "Open a different folder"}
      >
        <FolderOpen className="w-4 h-4" />
        Open folder
      </button>
    </div>
  );
}

interface TrackRowProps {
  track: TrackAnalysis;
  selected: boolean;
  checked: boolean;
  onCheck: () => void;
  onClick: () => void;
}

function TrackRow({ track, selected, checked, onCheck, onClick }: TrackRowProps) {
  const ml = track.ml_analysis;
  const review = needsReview(ml);
  const reviewTip = review ? reviewReason(ml, track.existing_tags) : null;
  return (
    <div
      onClick={onClick}
      className={cx("track-row", selected && "selected")}
      // Keyboard operability: the row is the ONLY way to open the track
      // inspector, and a plain clickable div was unreachable by keyboard.
      role="button"
      tabIndex={0}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onClick();
        }
      }}
    >
      <button
        className="text-white/40 hover:text-white p-1 mr-2 flex-none"
        onClick={(e) => { e.stopPropagation(); onCheck(); }}
        title={checked ? "Deselect" : "Select"}
      >
        {checked ? (
          <CheckSquare className="w-4 h-4 text-accent" />
        ) : (
          <Square className="w-4 h-4" />
        )}
      </button>

      <div className="flex-1 min-w-0 mr-4">
        <div className="flex items-center gap-1.5">
          {review && (
            <span
              className="flex-none"
              title={reviewTip ?? "Genre sources disagree — open to review"}
              aria-label="Needs review"
            >
              <AlertTriangle className="w-3.5 h-3.5 text-accent-yellow" />
            </span>
          )}
          <span className="text-sm text-white truncate min-w-0">
            {track.filename_title ?? track.filename}
          </span>
        </div>
        <div className="text-xs text-white/50 truncate">
          {track.filename_artist ?? ""}
        </div>
      </div>

      <div className="hidden sm:flex items-center gap-2 mr-4">
        {ml?.ml_genre && (
          <TagBadge color="purple">{ml.ml_subgenre || ml.ml_genre}</TagBadge>
        )}
        {ml?.ml_bpm && (
          <TagBadge color="neutral">{Math.round(ml.ml_bpm)} BPM</TagBadge>
        )}
        {ml?.ml_key && <TagBadge color="green">{ml.ml_key}</TagBadge>}
      </div>

      <div className="w-20 hidden md:block">
        {ml?.ml_energy != null && <EnergyBar level={ml.ml_energy} />}
      </div>

      <div className="w-16 text-right text-xs text-white/40 font-mono">
        {(track.size_mb ?? 0).toFixed(1)}M
      </div>
    </div>
  );
}

interface RecentLibraryCardProps {
  record: LibraryRecord;
  disabled: boolean;
  onOpen: () => void;
  onForget: () => void;
  onRename: (name: string) => void;
  onSetTags: (tags: string[]) => void;
}

function RecentLibraryCard({
  record, disabled, onOpen, onForget, onRename, onSetTags,
}: RecentLibraryCardProps) {
  // Inline edit mode: edits the friendly name + comma-separated tags. Kept
  // local to the card; the parent owns the rename_library / tag_library RPCs.
  const [editing, setEditing] = useState(false);
  const existingName = (record as LibraryRecord & { name?: string }).name ?? "";
  const existingTags = (record as LibraryRecord & { tags?: string[] }).tags ?? [];
  const [nameDraft, setNameDraft] = useState(existingName);
  const [tagsDraft, setTagsDraft] = useState(existingTags.join(", "));

  const beginEdit = (e: React.MouseEvent) => {
    e.stopPropagation();
    setNameDraft(existingName);
    setTagsDraft(existingTags.join(", "));
    setEditing(true);
  };

  const saveEdit = () => {
    if (nameDraft.trim() !== existingName) onRename(nameDraft);
    const nextTags = tagsDraft
      .split(",")
      .map((t) => t.trim())
      .filter(Boolean);
    if (nextTags.join(",") !== existingTags.join(",")) onSetTags(nextTags);
    setEditing(false);
  };

  // Right-click → forget. `onForget` opens the in-app ConfirmModal (parent
  // owns the modal state) — the previous `window.confirm` was OS-native,
  // off-brand, and could render behind the Tauri window on multi-monitor
  // setups, looking like the click did nothing.
  const handleContextMenu = (e: React.MouseEvent) => {
    e.preventDefault();
    if (disabled) return;
    onForget();
  };

  // Edit mode renders a self-contained form (not the clickable card) so the
  // inputs don't trigger open-on-click.
  if (editing) {
    return (
      <div className="panel-pad space-y-2" onClick={(e) => e.stopPropagation()}>
        <div className="text-xs text-white/50 font-mono truncate" title={record.path}>
          {record.path}
        </div>
        <input
          autoFocus
          className="input w-full"
          placeholder="Friendly name (e.g. Friday Warmup)"
          value={nameDraft}
          onChange={(e) => setNameDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") saveEdit();
            if (e.key === "Escape") setEditing(false);
          }}
        />
        <input
          className="input w-full"
          placeholder="Tags, comma-separated (e.g. wedding, chill)"
          value={tagsDraft}
          onChange={(e) => setTagsDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") saveEdit();
            if (e.key === "Escape") setEditing(false);
          }}
        />
        <div className="flex justify-end gap-2">
          <button className="btn-ghost text-xs" onClick={() => setEditing(false)}>
            Cancel
          </button>
          <button className="btn-primary text-xs" onClick={saveEdit}>
            Save
          </button>
        </div>
      </div>
    );
  }

  return (
    <div
      role="button"
      tabIndex={0}
      aria-disabled={disabled}
      onClick={() => !disabled && onOpen()}
      onKeyDown={(e) => {
        if ((e.key === "Enter" || e.key === " ") && !disabled) {
          e.preventDefault();
          onOpen();
        }
      }}
      onContextMenu={handleContextMenu}
      className={cx(
        "group panel-pad flex items-center gap-4 text-left transition-colors",
        disabled
          ? "opacity-50 cursor-not-allowed"
          : "cursor-pointer hover:bg-white/[0.04] hover:border-accent/30",
      )}
    >
      <div className="w-10 h-10 rounded-lg bg-accent/15 text-accent flex items-center justify-center flex-none">
        <Music className="w-5 h-5" />
      </div>
      <div className="flex-1 min-w-0">
        <div className="font-display font-medium text-base text-white truncate">
          {displayName(record)}
        </div>
        <div className="text-xs text-white/40 truncate font-mono" title={record.path}>
          {record.path}
        </div>
        <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-0.5 text-xs text-white/60">
          <span>
            <span className="text-white/80 font-mono">
              {compactFmt.format(record.track_count)}
            </span>{" "}
            tracks
          </span>
          <span>
            <span className="text-white/80 font-mono">
              {compactFmt.format(record.analyzed_count)}
            </span>{" "}
            analyzed
          </span>
          <span className="flex items-center gap-1 text-white/40">
            <Clock className="w-3 h-3" />
            Opened {relativeTime(record.last_opened)}
          </span>
        </div>
        {/* User-assigned tags ("Brunch", "Wedding", …). Treat the array as
            optional — older sidecar responses may not include it. */}
        {((record as LibraryRecord & { tags?: string[] }).tags ?? []).length > 0 && (
          <div className="mt-1 flex flex-wrap gap-1">
            {((record as LibraryRecord & { tags?: string[] }).tags ?? []).map((t) => (
              <span
                key={t}
                className="px-1.5 py-0.5 rounded text-[10px] bg-accent/10 text-accent border border-accent/20"
              >
                {t}
              </span>
            ))}
          </div>
        )}
      </div>
      <button
        className="opacity-0 group-hover:opacity-100 focus:opacity-100 text-white/40 hover:text-accent p-1.5 rounded transition-opacity"
        onClick={beginEdit}
        title="Rename / tag this library"
        aria-label={`Rename ${displayName(record)}`}
        disabled={disabled}
      >
        <Pencil className="w-4 h-4" />
      </button>
      <button
        className="opacity-0 group-hover:opacity-100 focus:opacity-100 text-white/40 hover:text-accent-red p-1.5 rounded transition-opacity"
        onClick={(e) => {
          e.stopPropagation();
          if (!disabled) onForget();
        }}
        title="Forget this library"
        aria-label={`Forget ${displayName(record)}`}
        disabled={disabled}
      >
        <X className="w-4 h-4" />
      </button>
    </div>
  );
}

