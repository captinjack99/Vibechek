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
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { invoke } from "@tauri-apps/api/core";

import { LibraryBrowser } from "./LibraryBrowser";
import { useLibraryStore } from "../stores";
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
