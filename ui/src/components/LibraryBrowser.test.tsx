/**
 * Regression tests for three LibraryBrowser bugs found in the bug hunt:
 *
 *  1. "Show errors only" toggle stays on across a library switch and traps the
 *     new (error-free) library in an empty, unrecoverable list.
 *  2. The free-text search box (searchFilter) persists across a library switch,
 *     silently narrowing the new library.
 *  5. The "select all" master checkbox keyed off raw size-equality and could
 *     mis-fire when an equal number of out-of-view tracks were selected.
 *
 * All three are exercised through the rendered component + the real Zustand
 * stores (mocked Tauri layer from src/test/setup.ts). The library switch is
 * driven through the same `load_recent_analysis` RPC the header switcher uses.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, act } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { invoke } from "@tauri-apps/api/core";
import { open as openDialog } from "@tauri-apps/plugin-dialog";

import { LibraryBrowser } from "./LibraryBrowser";
import { useLibraryStore, useNotificationStore, useOperationStore } from "../stores";
import { useLibraryFiltersStore } from "./LibraryFilters";
import type { LibraryRecord, MLResult, TrackAnalysis } from "../types";

function ml(overrides: Partial<MLResult> = {}): MLResult {
  return {
    ml_genre: "House",
    ml_subgenre: null,
    ml_genre_confidence: 0.9,
    ml_genre_raw_confidence: 0.9,
    ml_bpm: 124,
    ml_key: "8A",
    ml_energy: 3,
    ml_mood: "Happy",
    ml_timeslot: "Peak",
    ml_direction: "Up",
    ml_vocal: "Instrumental",
    ml_vocal_score: 0.1,
    ml_danceability: 0.8,
    ml_mood_scores: null,
    ml_error: null,
    ml_genre_audio: null,
    ml_subgenre_audio: null,
    ml_genre_web: null,
    ml_genre_web_grounded: null,
    ml_genre_source: null,
    ml_genre_conflict: null,
    ml_vocal_audio: null,
    ml_vocal_source: null,
    ml_key_tag: null,
    ml_key_conflict: null,
    ml_genre_classifier: null,
    ml_degraded_heads: null,
    ...overrides,
  };
}

function track(path: string, opts: Partial<TrackAnalysis> = {}): TrackAnalysis {
  return {
    path,
    filename: path.split(/[\\/]/).pop() ?? path,
    extension: ".mp3",
    size_mb: 5,
    existing_tags: {},
    ml_analysis: ml(),
    error: null,
    ...opts,
  };
}

const recordB: LibraryRecord = {
  path: "D:/LibraryB",
  track_count: 2,
  analyzed_count: 2,
  last_opened: 0,
} as LibraryRecord;

/**
 * Route invoke() by RPC method so the component's startup probes and the
 * library-switch RPC return sane shapes. `reportB` is the analysis returned
 * for the LibraryB switch.
 */
function routeInvoke(reportB: { tracks: TrackAnalysis[] }) {
  (invoke as ReturnType<typeof vi.fn>).mockImplementation(
    async (_cmd: string, args: { method: string }) => {
      switch (args.method) {
        case "library_state":
          return { recent: [recordB], active: null };
        case "load_recent_analysis":
          return { loaded: true, report: { tracks: reportB.tracks } };
        case "count_new_tracks":
          return { new_count: 0, total_count: reportB.tracks.length };
        default:
          return {};
      }
    },
  );
}

describe("<LibraryBrowser /> — state reset on library switch", () => {
  beforeEach(() => {
    useLibraryFiltersStore.getState().clearFilters();
  });

  // Open the header library switcher and click LibraryB. The switcher button's
  // accessible name is the header label ("Library" when no friendly name); the
  // recent list is fetched async on mount, so wait for the option to appear.
  async function switchToLibraryB(user: ReturnType<typeof userEvent.setup>) {
    const switcher = await screen.findByRole("button", { name: /^Library/i });
    await user.click(switcher);
    await user.click(await screen.findByRole("option", { name: /LibraryB/ }));
  }

  it("clears the search box when switching libraries (bug #2)", async () => {
    const user = userEvent.setup();
    routeInvoke({ tracks: [track("D:/LibraryB/clean1.mp3"), track("D:/LibraryB/clean2.mp3")] });

    // Library A loaded with a stale search query.
    useLibraryStore.setState({
      libraryPath: "D:/LibraryA",
      tracks: [track("D:/LibraryA/remix.mp3")],
      searchFilter: "remix",
    });

    render(<LibraryBrowser />);

    const searchBox = await screen.findByPlaceholderText(/filter tracks/i);
    expect((searchBox as HTMLInputElement).value).toBe("remix");

    await switchToLibraryB(user);

    // The stale query must be gone — otherwise it keeps narrowing LibraryB.
    await waitFor(() => {
      expect(useLibraryStore.getState().searchFilter).toBe("");
    });
    expect((screen.getByPlaceholderText(/filter tracks/i) as HTMLInputElement).value).toBe("");
    // Both of LibraryB's tracks are in scope (filtered === tracks). The toolbar
    // counter reads "<filtered> / <total>" — assert no stale narrowing.
    expect(screen.getByText("2 / 2")).toBeInTheDocument();
  });

  it("clears the errors-only filter when switching to a clean library (bug #1)", async () => {
    const user = userEvent.setup();
    routeInvoke({ tracks: [track("D:/LibraryB/clean1.mp3"), track("D:/LibraryB/clean2.mp3")] });

    // Library A has an error track; pre-render so the error chip appears.
    useLibraryStore.setState({
      libraryPath: "D:/LibraryA",
      tracks: [
        track("D:/LibraryA/good.mp3"),
        track("D:/LibraryA/bad.mp3", { error: "decode failed", ml_analysis: null }),
      ],
      searchFilter: "",
    });

    render(<LibraryBrowser />);

    // Toggle "show errors only" on. Only the 1 error track is in scope now,
    // so the toolbar counter reads "1 / 2".
    const errorChip = await screen.findByRole("button", { name: /1 error/i });
    await user.click(errorChip);
    expect(screen.getByText("1 / 2")).toBeInTheDocument();

    // Switch to the clean LibraryB (zero errors).
    await switchToLibraryB(user);

    // The new library's tracks must ALL be in scope ("2 / 2") — NOT trapped
    // behind a stale errors-only filter that would render "0 / 2" with no way
    // to clear (the error chip wouldn't render for an error-free library).
    await waitFor(() => {
      expect(screen.getByText("2 / 2")).toBeInTheDocument();
    });
  });
});

describe("<LibraryBrowser /> — select-all master checkbox (bug #5)", () => {
  it("does not show 'all selected' when an equal-size out-of-view set is selected", async () => {
    (invoke as ReturnType<typeof vi.fn>).mockResolvedValue({ recent: [], active: null });

    // Three tracks; the search query surfaces only one of them, but a DIFFERENT
    // (out-of-view) track is selected — an equal-size selection (1 === 1).
    useLibraryStore.setState({
      libraryPath: "D:/LibraryA",
      tracks: [
        track("D:/LibraryA/alpha.mp3"),
        track("D:/LibraryA/beta.mp3"),
        track("D:/LibraryA/gamma.mp3"),
      ],
      // gamma is selected but NOT visible under the "alpha" filter below.
      selectedIds: new Set(["D:/LibraryA/gamma.mp3"]),
      searchFilter: "alpha",
    });

    render(<LibraryBrowser />);

    // The toolbar master checkbox (title "Select all") must NOT read as
    // "Clear selection"/checked: none of the visible rows are actually
    // selected, so the old size-equality check (1 === 1) would wrongly flip
    // it to checked and clicking would clear the invisible selection.
    const master = await screen.findByRole("button", { name: /select all/i });
    expect(master).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /clear selection/i })).not.toBeInTheDocument();
  });

  it("reads as 'all selected' only when every visible row is selected", async () => {
    (invoke as ReturnType<typeof vi.fn>).mockResolvedValue({ recent: [], active: null });

    useLibraryStore.setState({
      libraryPath: "D:/LibraryA",
      tracks: [
        track("D:/LibraryA/alpha.mp3"),
        track("D:/LibraryA/beta.mp3"),
      ],
      selectedIds: new Set(["D:/LibraryA/alpha.mp3"]),
      searchFilter: "alpha", // only alpha is visible, and it IS selected
    });

    render(<LibraryBrowser />);

    // Every visible row (just alpha) is selected → master shows "Clear selection".
    const master = await screen.findByRole("button", { name: /clear selection/i });
    expect(master).toBeInTheDocument();

    // Clicking it clears the selection (acts on what's visible).
    const user = userEvent.setup();
    await user.click(master);
    await waitFor(() => {
      // The visible row was selected, so clear empties the selection.
      expect(useLibraryStore.getState().selectedIds.size).toBe(0);
    });
  });
});

describe("<LibraryBrowser /> — select-all checkbox", () => {
  it("selecting all visible rows selects exactly the filtered set", async () => {
    (invoke as ReturnType<typeof vi.fn>).mockResolvedValue({ recent: [], active: null });

    useLibraryStore.setState({
      libraryPath: "D:/LibraryA",
      tracks: [
        track("D:/LibraryA/alpha.mp3"),
        track("D:/LibraryA/beta.mp3"),
        track("D:/LibraryA/gamma.mp3"),
      ],
      selectedIds: new Set(),
      searchFilter: "a", // matches alpha, beta, gamma — all contain 'a'
    });

    const user = userEvent.setup();
    render(<LibraryBrowser />);

    const master = await screen.findByRole("button", { name: /select all/i });
    await user.click(master);

    await waitFor(() => {
      expect(useLibraryStore.getState().selectedIds.size).toBe(3);
    });
  });
});

describe("<LibraryBrowser /> — review (genre conflict) filter", () => {
  beforeEach(() => {
    useLibraryFiltersStore.getState().clearFilters();
  });

  it("counts conflicting tracks and narrows to them when toggled", async () => {
    (invoke as ReturnType<typeof vi.fn>).mockResolvedValue({ recent: [], active: null });

    useLibraryStore.setState({
      libraryPath: "D:/LibraryA",
      tracks: [
        track("D:/LibraryA/clean.mp3"), // tag == audio, no conflict
        track("D:/LibraryA/conflict.mp3", {
          existing_tags: { genre: "Tech House" },
          ml_analysis: ml({
            ml_genre_source: "ml_override",
            ml_genre_conflict: true,
            ml_genre: "Trance",
            ml_subgenre: "Trance",
            ml_genre_audio: "Trance",
          }),
        }),
      ],
      selectedIds: new Set(),
      searchFilter: "",
    });

    const user = userEvent.setup();
    render(<LibraryBrowser />);

    // The review pill reports exactly the one conflicting track; both tracks
    // are still in scope until the filter is engaged.
    const reviewChip = await screen.findByRole("button", { name: /1 to review/i });
    expect(screen.getByText("2 / 2")).toBeInTheDocument();

    // Engaging it narrows the list to just the conflicting track.
    await user.click(reviewChip);
    await waitFor(() => {
      expect(screen.getByText("1 / 2")).toBeInTheDocument();
    });
  });

  it("Review and Errors filters are mutually exclusive", async () => {
    (invoke as ReturnType<typeof vi.fn>).mockResolvedValue({ recent: [], active: null });

    useLibraryStore.setState({
      libraryPath: "D:/LibraryA",
      tracks: [
        track("D:/LibraryA/bad.mp3", { error: "decode failed", ml_analysis: null }),
        track("D:/LibraryA/conflict.mp3", {
          existing_tags: { genre: "Tech House" },
          ml_analysis: ml({
            ml_genre_source: "ml_override",
            ml_genre_conflict: true,
            ml_genre: "Trance",
            ml_subgenre: "Trance",
            ml_genre_audio: "Trance",
          }),
        }),
      ],
      selectedIds: new Set(),
      searchFilter: "",
    });

    const user = userEvent.setup();
    render(<LibraryBrowser />);

    // Engage Review.
    await user.click(await screen.findByRole("button", { name: /1 to review/i }));
    expect(
      await screen.findByRole("button", { name: /to review.*showing/i }),
    ).toBeInTheDocument();

    // Engaging Errors must turn Review off (they're different views).
    await user.click(screen.getByRole("button", { name: /1 error/i }));
    await waitFor(() => {
      expect(screen.getByRole("button", { name: /error.*showing/i })).toBeInTheDocument();
    });
    expect(
      screen.queryByRole("button", { name: /to review.*showing/i }),
    ).not.toBeInTheDocument();
  });

  it("batch-approves selected conflicts and drains the review queue", async () => {
    const conflict = track("D:/LibraryA/conflict.mp3", {
      existing_tags: { genre: "Tech House" },
      ml_analysis: ml({
        ml_genre_source: "ml_override",
        ml_genre_conflict: true,
        ml_genre: "Trance",
        ml_subgenre: "Trance",
        ml_genre_audio: "Trance",
      }),
    });
    // What the sidecar returns post-approve: same track, conflict cleared.
    const resolved = track("D:/LibraryA/conflict.mp3", {
      existing_tags: { genre: "Tech House" },
      ml_analysis: ml({
        ml_genre_source: "approved",
        ml_genre_conflict: false,
        ml_genre: "Trance",
        ml_subgenre: "Trance",
        ml_genre_audio: "Trance",
      }),
    });

    (invoke as ReturnType<typeof vi.fn>).mockImplementation(
      async (_cmd: string, args: { method: string; params?: unknown }) => {
        switch (args.method) {
          case "library_state":
            return { recent: [], active: null };
          case "resolve_genre_conflicts":
            return { ok: true, updated: 1, tracks: [resolved] };
          default:
            return {};
        }
      },
    );

    useLibraryStore.setState({
      libraryPath: "D:/LibraryA",
      tracks: [track("D:/LibraryA/clean.mp3"), conflict],
      selectedIds: new Set(),
      searchFilter: "",
    });

    const user = userEvent.setup();
    render(<LibraryBrowser />);

    // Engage review, then select the (only) visible flagged row.
    await user.click(await screen.findByRole("button", { name: /1 to review/i }));
    await user.click(await screen.findByRole("button", { name: /select all/i }));

    // The review-mode toolbar surfaces Approve/Revert (not Apply ML tags).
    const approve = await screen.findByRole("button", { name: /approve 1/i });
    expect(screen.queryByRole("button", { name: /apply ml tags/i })).not.toBeInTheDocument();
    await user.click(approve);

    // The RPC was called with the flagged path + approve action.
    await waitFor(() => {
      const call = (invoke as ReturnType<typeof vi.fn>).mock.calls.find(
        (c) => (c[1] as { method: string }).method === "resolve_genre_conflicts",
      );
      expect(call).toBeTruthy();
      const params = (call![1] as { params: { items: { path: string; action: string }[] } }).params;
      expect(params.items).toEqual([{ path: "D:/LibraryA/conflict.mp3", action: "approve" }]);
    });

    // The merged record cleared its conflict, draining the queue → "All caught up".
    await waitFor(() => {
      const merged = useLibraryStore
        .getState()
        .tracks.find((t) => t.path === "D:/LibraryA/conflict.mp3");
      expect(merged?.ml_analysis?.ml_genre_conflict).toBe(false);
    });
    expect(await screen.findByText(/all caught up/i)).toBeInTheDocument();
  });

  it("resets the Review filter on a library switch", async () => {
    (invoke as ReturnType<typeof vi.fn>).mockImplementation(
      async (_cmd: string, args: { method: string }) => {
        switch (args.method) {
          case "library_state":
            return { recent: [recordB], active: null };
          case "load_recent_analysis":
            return {
              loaded: true,
              report: { tracks: [track("D:/LibraryB/c1.mp3"), track("D:/LibraryB/c2.mp3")] },
            };
          case "count_new_tracks":
            return { new_count: 0, total_count: 2 };
          default:
            return {};
        }
      },
    );

    useLibraryStore.setState({
      libraryPath: "D:/LibraryA",
      tracks: [
        track("D:/LibraryA/clean.mp3"),
        track("D:/LibraryA/conflict.mp3", {
          existing_tags: { genre: "Tech House" },
          ml_analysis: ml({
            ml_genre_source: "ml_override",
            ml_genre_conflict: true,
            ml_genre: "Trance",
            ml_subgenre: "Trance",
            ml_genre_audio: "Trance",
          }),
        }),
      ],
      selectedIds: new Set(),
      searchFilter: "",
    });

    const user = userEvent.setup();
    render(<LibraryBrowser />);

    await user.click(await screen.findByRole("button", { name: /1 to review/i }));
    await waitFor(() => expect(screen.getByText("1 / 2")).toBeInTheDocument());

    // Switch to the clean LibraryB — the review filter must NOT carry over and
    // strand the new (conflict-free) library behind a "0 / 2" view.
    await user.click(await screen.findByRole("button", { name: /^Library/i }));
    await user.click(await screen.findByRole("option", { name: /LibraryB/ }));

    await waitFor(() => expect(screen.getByText("2 / 2")).toBeInTheDocument());
  });
});

describe("<LibraryBrowser /> — analyze completion toast", () => {
  beforeEach(() => {
    // The toast is a notification; reset both stores so each case is clean.
    useNotificationStore.setState({ items: [] });
    useOperationStore.setState({ active: null, opId: null, progress: null, startedAt: null, error: null });
  });

  // Drive a full re-analyze through the toolbar "Re-analyze all" button:
  // handleAnalyze does a preflight probe (mocked ready) then runAnalyze, which
  // hits analyze_directory (mocked to return `report`). Returns after the toast
  // has been pushed.
  async function reanalyzeWith(report: Record<string, unknown>) {
    (invoke as ReturnType<typeof vi.fn>).mockImplementation(
      async (_cmd: string, args: { method: string }) => {
        switch (args.method) {
          case "library_state":
            return { recent: [], active: null };
          case "preflight":
            return { ready: true };
          case "analyze_directory":
            return report;
          default:
            return {};
        }
      },
    );

    useLibraryStore.setState({
      libraryPath: "D:/LibraryA",
      tracks: [track("D:/LibraryA/a.mp3"), track("D:/LibraryA/b.mp3")],
      selectedIds: new Set(),
      searchFilter: "",
    });

    const user = userEvent.setup();
    render(<LibraryBrowser />);
    await user.click(await screen.findByRole("button", { name: /re-analyze all/i }));
  }

  it("shows a success toast with the analyzed count on a clean run", async () => {
    await reanalyzeWith({
      summary: { total_files: 3, analyzed: 3, errors: 0 },
      tracks: [track("D:/LibraryA/a.mp3")],
    });

    await waitFor(() => {
      const items = useNotificationStore.getState().items;
      expect(items.length).toBe(1);
      expect(items[0].message).toBe("Analyzed 3/3");
      expect(items[0].kind).toBe("success");
    });
  });

  it("shows an error-aware info toast when some tracks failed", async () => {
    await reanalyzeWith({
      summary: { total_files: 3, analyzed: 2, errors: 1 },
      tracks: [track("D:/LibraryA/a.mp3")],
    });

    await waitFor(() => {
      const items = useNotificationStore.getState().items;
      expect(items.length).toBe(1);
      expect(items[0].message).toBe("Analyzed 2/3 — 1 error");
      expect(items[0].kind).toBe("info");
      // The detail points the user at the Errors filter (they'd otherwise have
      // to scroll the table hunting for red icons).
      expect(items[0].detail).toMatch(/Errors/);
    });
  });

  it("shows a persistent WARNING toast when the run could not be saved", async () => {
    await reanalyzeWith({
      summary: { total_files: 3, analyzed: 3, errors: 0 },
      tracks: [track("D:/LibraryA/a.mp3")],
      persist_error: "No space left on device",
    });

    await waitFor(() => {
      const items = useNotificationStore.getState().items;
      expect(items.length).toBe(1);
      expect(items[0].message).toMatch(/could NOT be saved/);
      expect(items[0].kind).toBe("warning");
      expect(items[0].detail).toMatch(/No space left on device/);
    });
  });

  it("folds a dropped Rekordbox priors warning into the completion toast", async () => {
    await reanalyzeWith({
      summary: { total_files: 3, analyzed: 3, errors: 0 },
      tracks: [track("D:/LibraryA/a.mp3")],
      priors_warning: "Your imported Rekordbox priors no longer match this library — re-import the XML.",
    });

    await waitFor(() => {
      const items = useNotificationStore.getState().items;
      expect(items.length).toBe(1);
      expect(items[0].kind).toBe("warning");
      expect(items[0].detail).toMatch(/Rekordbox priors no longer match/);
    });
  });

  it("warns when GPU libraries couldn't be restored during an engine self-heal", async () => {
    await reanalyzeWith({
      summary: { total_files: 3, analyzed: 3, errors: 0 },
      tracks: [track("D:/LibraryA/a.mp3")],
      runtime_heal_warning: "GPU libraries could not be restored automatically — this run used the CPU.",
    });

    await waitFor(() => {
      const items = useNotificationStore.getState().items;
      expect(items.length).toBe(1);
      // A failed GPU restore is a caution, not a clean success.
      expect(items[0].kind).toBe("warning");
      expect(items[0].detail).toMatch(/GPU libraries could not be restored/);
    });
  });

  it("folds an online-genre-lookup outage into the completion toast as a caution", async () => {
    await reanalyzeWith({
      summary: { total_files: 3, analyzed: 3, errors: 0 },
      tracks: [track("D:/LibraryA/a.mp3")],
      genre_web_unavailable_warning:
        "Online genre lookup wasn't available this run — genres used tags and audio only. Restarting Vibechek usually restores it.",
    });

    await waitFor(() => {
      const items = useNotificationStore.getState().items;
      expect(items.length).toBe(1);
      // A silent degradation is a caution, not a clean success — same toast as
      // the sibling warnings, not a parallel one.
      expect(items[0].kind).toBe("warning");
      expect(items[0].detail).toMatch(/Online genre lookup wasn't available/);
    });
  });

  it("notes a successful engine auto-repair without downgrading a clean run", async () => {
    await reanalyzeWith({
      summary: { total_files: 3, analyzed: 3, errors: 0 },
      tracks: [track("D:/LibraryA/a.mp3")],
      runtime_healed: "The analysis engine environment was repaired automatically (ml-stack).",
    });

    await waitFor(() => {
      const items = useNotificationStore.getState().items;
      expect(items.length).toBe(1);
      // A successful repair is informational — the clean run stays a success.
      expect(items[0].kind).toBe("success");
      expect(items[0].detail).toMatch(/repaired automatically/);
    });
  });
});

describe("<LibraryBrowser /> — Import Rekordbox XML (tag priors)", () => {
  it("imports priors: picks a file, calls the RPC, merges the returned tracks", async () => {
    // The file picker resolves to an XML path (mocked plugin-dialog from
    // src/test/setup.ts).
    (openDialog as ReturnType<typeof vi.fn>).mockResolvedValue("D:/rekordbox.xml");

    // What the sidecar returns post-import: the same track with the XML genre
    // merged in at the tag tier and the record re-reconciled.
    const updated = track("D:/LibraryA/one.mp3", {
      existing_tags: { genre: "Melodic Techno", genre_origin: "rekordbox" },
      ml_analysis: ml({
        ml_genre: "Melodic Techno",
        ml_subgenre: "Melodic Techno",
        ml_genre_source: "tag",
        ml_genre_conflict: false,
      }),
    });

    (invoke as ReturnType<typeof vi.fn>).mockImplementation(
      async (_cmd: string, args: { method: string; params?: unknown }) => {
        switch (args.method) {
          case "library_state":
            return { recent: [], active: null };
          case "import_tag_priors":
            return { ok: true, xml_tracks: 12, matched: 1, updated: 1, tracks: [updated] };
          default:
            return {};
        }
      },
    );

    useLibraryStore.setState({
      libraryPath: "D:/LibraryA",
      tracks: [track("D:/LibraryA/one.mp3")], // genre "House" pre-import
      selectedIds: new Set(),
      searchFilter: "",
    });

    const user = userEvent.setup();
    render(<LibraryBrowser />);

    await user.click(
      await screen.findByRole("button", { name: /import rekordbox xml/i }),
    );

    // The RPC was called with the current library + the picked XML path.
    await waitFor(() => {
      const call = (invoke as ReturnType<typeof vi.fn>).mock.calls.find(
        (c) => (c[1] as { method: string }).method === "import_tag_priors",
      );
      expect(call).toBeTruthy();
      const params = (call![1] as {
        params: { library_path: string; xml_path: string };
      }).params;
      expect(params.library_path).toBe("D:/LibraryA");
      expect(params.xml_path).toBe("D:/rekordbox.xml");
    });

    // The returned record was merged into the store — the genre changed from
    // the pre-import "House" to the XML's "Melodic Techno", marked as a
    // Rekordbox-origin tag. (Row DOM isn't assertable here: Virtuoso renders
    // no items under jsdom, so like the batch-approve test we assert the
    // store the rows render from.)
    await waitFor(() => {
      const merged = useLibraryStore
        .getState()
        .tracks.find((t) => t.path === "D:/LibraryA/one.mp3");
      expect(merged?.ml_analysis?.ml_genre).toBe("Melodic Techno");
      expect(merged?.existing_tags.genre_origin).toBe("rekordbox");
    });
  });

  it("does NOT merge into a different library opened while the import was in flight", async () => {
    // Regression: handleImportPriors awaited a slow RPC with the library
    // switcher still enabled; mergeAnalyzedTracks APPENDS unknown paths, so
    // library A's records were spliced into whichever library was open when
    // the RPC resolved (then swept into select-all / Apply-ML-tags / organize
    // fingerprints). The import must detect the switch and drop the merge —
    // the sidecar already persisted it to library A's saved analysis.
    (openDialog as ReturnType<typeof vi.fn>).mockResolvedValue("D:/rekordbox.xml");

    const updated = track("D:/LibraryA/one.mp3", {
      existing_tags: { genre: "Melodic Techno", genre_origin: "rekordbox" },
      ml_analysis: ml({ ml_genre: "Melodic Techno", ml_genre_source: "tag" }),
    });

    // Hold the import RPC open so we can switch libraries mid-flight.
    let resolveImport!: (v: unknown) => void;
    (invoke as ReturnType<typeof vi.fn>).mockImplementation(
      async (_cmd: string, args: { method: string }) => {
        switch (args.method) {
          case "library_state":
            return { recent: [], active: null };
          case "import_tag_priors":
            return new Promise((res) => { resolveImport = res; });
          default:
            return {};
        }
      },
    );

    useLibraryStore.setState({
      libraryPath: "D:/LibraryA",
      tracks: [track("D:/LibraryA/one.mp3")],
      selectedIds: new Set(),
      searchFilter: "",
    });

    const user = userEvent.setup();
    render(<LibraryBrowser />);
    await user.click(
      await screen.findByRole("button", { name: /import rekordbox xml/i }),
    );
    await waitFor(() => expect(resolveImport).toBeTypeOf("function"));

    // The user switches to library B while the import is still running.
    useLibraryStore.setState({
      libraryPath: "D:/LibraryB",
      tracks: [track("D:/LibraryB/other.mp3")],
    });

    resolveImport({ ok: true, xml_tracks: 12, matched: 1, updated: 1, tracks: [updated] });

    // The import button leaves its busy state (the handler finished)…
    await waitFor(() => {
      expect(
        screen.getByRole("button", { name: /import rekordbox xml/i }),
      ).not.toBeDisabled();
    });
    // …and library A's record was NOT appended into library B's list.
    const paths = useLibraryStore.getState().tracks.map((t) => t.path);
    expect(paths).toEqual(["D:/LibraryB/other.mp3"]);
  });
});

describe("<LibraryBrowser /> — retryable load-recent failure", () => {
  beforeEach(() => {
    useLibraryFiltersStore.getState().clearFilters();
    useOperationStore.setState({
      active: null, opId: null, progress: null, startedAt: null,
      error: null, errorInfo: null,
    });
  });

  // Same header-switcher drive as the state-reset suite (that helper is local
  // to its own describe block).
  async function switchToLibraryB(user: ReturnType<typeof userEvent.setup>) {
    const switcher = await screen.findByRole("button", { name: /^Library/i });
    await user.click(switcher);
    await user.click(await screen.findByRole("option", { name: /LibraryB/ }));
  }

  it("does NOT attach a retryAction when the load failure isn't retryable", async () => {
    (invoke as ReturnType<typeof vi.fn>).mockImplementation(
      async (_cmd: string, args: { method: string }) => {
        switch (args.method) {
          case "library_state":
            return { recent: [recordB], active: null };
          case "load_recent_analysis":
            // Terminal failure — no `retryable` flag.
            return { loaded: false, reason: "library not in recents" };
          default:
            return {};
        }
      },
    );

    useLibraryStore.setState({
      libraryPath: "D:/LibraryA",
      tracks: [track("D:/LibraryA/remix.mp3")],
      searchFilter: "",
    });

    const user = userEvent.setup();
    render(<LibraryBrowser />);
    await switchToLibraryB(user);

    await waitFor(() => {
      expect(useOperationStore.getState().error).toBe("library not in recents");
    });
    const info = useOperationStore.getState().errorInfo;
    expect(info?.retryAction).toBeUndefined();
    expect(info?.kind).toBeUndefined();
  });

  it("attaches a retryAction on a retryable failure; invoking it re-issues the load and populates tracks", async () => {
    const recordBTracks = [track("D:/LibraryB/c1.mp3"), track("D:/LibraryB/c2.mp3")];
    let loadCalls = 0;
    (invoke as ReturnType<typeof vi.fn>).mockImplementation(
      async (_cmd: string, args: { method: string }) => {
        switch (args.method) {
          case "library_state":
            return { recent: [recordB], active: null };
          case "load_recent_analysis":
            loadCalls += 1;
            // Fail the first attempt with a locked-file (retryable) failure,
            // succeed on the retry.
            return loadCalls === 1
              ? {
                  loaded: false,
                  retryable: true,
                  reason:
                    "Vibechek couldn't read the saved analysis for this library right now — try again.",
                }
              : { loaded: true, report: { tracks: recordBTracks } };
          case "count_new_tracks":
            return { new_count: 0, total_count: 2 };
          default:
            return {};
        }
      },
    );

    useLibraryStore.setState({
      libraryPath: "D:/LibraryA",
      tracks: [track("D:/LibraryA/remix.mp3")],
      searchFilter: "",
    });

    const user = userEvent.setup();
    render(<LibraryBrowser />);
    await switchToLibraryB(user);

    // First attempt failed retryably → fail() received a component-owned closure.
    await waitFor(() => {
      const info = useOperationStore.getState().errorInfo;
      expect(info?.retryAction).toBeTypeOf("function");
      expect(info?.kind).toBe("retryable");
    });
    expect(loadCalls).toBe(1);

    // Invoke the closure — the same thing ErrorToast's "Try again" does. It must
    // re-enter the full load path and, on the successful second response,
    // populate the library's tracks.
    const retryAction = useOperationStore.getState().errorInfo!.retryAction!;
    await act(async () => {
      await retryAction();
    });

    await waitFor(() => {
      const paths = useLibraryStore.getState().tracks.map((t) => t.path);
      expect(paths).toEqual(["D:/LibraryB/c1.mp3", "D:/LibraryB/c2.mp3"]);
    });
    expect(loadCalls).toBe(2);
    expect(useLibraryStore.getState().libraryPath).toBe("D:/LibraryB");
  });
});
