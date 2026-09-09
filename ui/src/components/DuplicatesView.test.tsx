/**
 * Regression test: the Chromaprint similarity-threshold setting was silently
 * ignored by the dedupe scan.
 *
 * The Settings slider writes `duplicates.chromaprint_similarity_threshold`, but
 * `handleScan` used to send only `{ path, use_md5, use_chromaprint }` to
 * `find_duplicates` — so the slider was dead. This asserts the configured
 * threshold actually reaches the RPC payload (the backend reads it as
 * `threshold`, see rpc._find_duplicates).
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { invoke } from "@tauri-apps/api/core";

import {
  DuplicatesView,
  applyChoices,
  countDuplicates,
  keyGroups,
  plannedDuplicatePaths,
  resolvedDuplicatePaths,
} from "./DuplicatesView";
import { DEFAULT_RULES } from "../lib/keeperRules";
import {
  useConfigStore,
  useLibraryStore,
  useNotificationStore,
  useOperationStore,
} from "../stores";
import type { DuplicateReport, FileInfo, TrackAnalysis } from "../types";

const emptyReport: DuplicateReport = {
  summary: {
    total_files: 0,
    exact_duplicate_groups: 0,
    exact_duplicate_files: 0,
    audio_duplicate_groups: 0,
    audio_duplicate_files: 0,
    total_duplicates: 0,
    space_recoverable_mb: 0,
  },
  exact_duplicates: [],
  audio_duplicates: [],
};

describe("<DuplicatesView /> — find_duplicates payload", () => {
  beforeEach(() => {
    (invoke as ReturnType<typeof vi.fn>).mockResolvedValue(emptyReport);
    // A library path seeds the scan path so the Scan button is enabled.
    useLibraryStore.setState({ libraryPath: "D:/Music" });
  });

  it("forwards the configured chromaprint similarity threshold to find_duplicates", async () => {
    const user = userEvent.setup();
    // Set a non-default threshold the user would have moved the slider to.
    useConfigStore.getState().updateDuplicates({
      use_md5: true,
      use_chromaprint: true,
      chromaprint_similarity_threshold: 0.81,
    });

    render(<DuplicatesView />);
    await user.click(screen.getByRole("button", { name: /scan/i }));

    await waitFor(() => {
      expect(invoke).toHaveBeenCalledWith(
        "rpc_call",
        expect.objectContaining({
          method: "find_duplicates",
          params: expect.objectContaining({ threshold: 0.81 }),
        }),
      );
    });
  });
});

// ---------------------------------------------------------------------------
// Regression for "Move to review folder always reports 'Moved 0 duplicates'".
//
// `handle_duplicates` always returns BOTH `moved` and `deleted` (init 0),
// incrementing only the action's key. The toast used `summary.deleted ??
// summary.moved`, which for a MOVE returns the number 0 (`??` only falls
// through on null/undefined) — so every move read "Moved 0 duplicates". The
// fix selects the count by action: move -> moved, trash -> deleted.
// ---------------------------------------------------------------------------

function fileInfo(path: string, sizeMb = 5): FileInfo {
  return {
    path,
    filename: path.split(/[\\/]/).pop() ?? path,
    size_bytes: Math.round(sizeMb * 1024 * 1024),
    size_mb: sizeMb,
    file_hash: "h",
    audio_fingerprint: null,
    codec: "mp3",
    bitrate_kbps: 320,
    duration_s: 200,
    modified_time: 0,
  };
}

/** A report with one exact-duplicate group (1 keeper + 1 dupe) so an action
 *  has something to act on (filesToAct > 0 enables the buttons). */
function reportWithOneGroup(): DuplicateReport {
  const keep = fileInfo("D:/Music/a.mp3");
  const dup = fileInfo("D:/Music/a (1).mp3");
  return {
    summary: {
      total_files: 2,
      exact_duplicate_groups: 1,
      exact_duplicate_files: 1,
      audio_duplicate_groups: 0,
      audio_duplicate_files: 0,
      total_duplicates: 1,
      space_recoverable_mb: 5,
    },
    exact_duplicates: [
      { method: "md5", key: "g1", keep, duplicates: [dup], recoverable_mb: 5 },
    ],
    audio_duplicates: [],
  };
}

describe("<DuplicatesView /> — resolve result toast", () => {
  beforeEach(() => {
    useLibraryStore.setState({ libraryPath: "D:/Music" });
    useConfigStore.getState().updateDuplicates({ review_folder: "D:/Review" });
  });

  it("reports the real moved count for a move action (not 0)", async () => {
    const user = userEvent.setup();
    // handle_duplicates returns {moved: N, deleted: 0}; the follow-up rescan
    // (find_duplicates) returns an empty report.
    (invoke as ReturnType<typeof vi.fn>).mockImplementation(
      async (_cmd: string, args: { method: string }) => {
        if (args.method === "handle_duplicates") {
          return { moved: 7, deleted: 0, errors: 0, journal_path: "D:/j.jsonl" };
        }
        return emptyReport; // find_duplicates rescan
      },
    );
    // Seed a report so the action buttons render + enable.
    useOperationStore.setState({ duplicateReport: reportWithOneGroup() });

    render(<DuplicatesView />);

    await user.click(screen.getByRole("button", { name: /move to review folder/i }));
    // Confirm the modal.
    await user.click(await screen.findByRole("button", { name: /yes, move files/i }));

    await waitFor(() => {
      const msgs = useNotificationStore.getState().items.map((n) => n.message);
      expect(msgs).toContain("Moved 7 duplicates");
    });
    // And specifically NOT the old buggy "Moved 0 duplicates".
    expect(
      useNotificationStore.getState().items.map((n) => n.message),
    ).not.toContain("Moved 0 duplicates");
  });

  it("reports the real trashed count for a trash action", async () => {
    const user = userEvent.setup();
    (invoke as ReturnType<typeof vi.fn>).mockImplementation(
      async (_cmd: string, args: { method: string }) => {
        if (args.method === "handle_duplicates") {
          return { moved: 0, deleted: 4, errors: 0 };
        }
        return emptyReport;
      },
    );
    useOperationStore.setState({ duplicateReport: reportWithOneGroup() });

    render(<DuplicatesView />);

    await user.click(screen.getByRole("button", { name: /send to trash/i }));
    await user.click(await screen.findByRole("button", { name: /yes, send to trash/i }));

    await waitFor(() => {
      const msgs = useNotificationStore.getState().items.map((n) => n.message);
      expect(msgs).toContain("Trashed 4 duplicates");
    });
  });
});

// ---------------------------------------------------------------------------
// Partial-failure surfacing. `handle_duplicates` returns a per-file
// `error_messages` list; the old toast said "see report" but nothing rendered
// it. The fix promotes that list into a durable in-view expandable panel.
// ---------------------------------------------------------------------------

describe("<DuplicatesView /> — partial-failure report", () => {
  beforeEach(() => {
    useLibraryStore.setState({ libraryPath: "D:/Music" });
    useConfigStore.getState().updateDuplicates({ review_folder: "D:/Review" });
  });

  it("renders the per-file failure list when some files couldn't be moved", async () => {
    const user = userEvent.setup();
    (invoke as ReturnType<typeof vi.fn>).mockImplementation(
      async (_cmd: string, args: { method: string }) => {
        if (args.method === "handle_duplicates") {
          return {
            moved: 5,
            deleted: 0,
            errors: 2,
            error_messages: [
              "D:/Music/x.mp3: move failed — [Errno 13] Permission denied",
              "D:/Music/y.mp3: file not found",
            ],
            journal_path: "D:/j.jsonl",
          };
        }
        return emptyReport; // find_duplicates rescan
      },
    );
    useOperationStore.setState({ duplicateReport: reportWithOneGroup() });

    render(<DuplicatesView />);
    await user.click(screen.getByRole("button", { name: /move to review folder/i }));
    await user.click(await screen.findByRole("button", { name: /yes, move files/i }));

    // Honest headline count + the actual per-file lines (the "report").
    expect(await screen.findByText(/2 files couldn't be moved/i)).toBeInTheDocument();
    expect(
      screen.getByText(/\[Errno 13\] Permission denied/i),
    ).toBeInTheDocument();
    expect(screen.getByText(/y\.mp3: file not found/i)).toBeInTheDocument();
    // And the toast no longer promises a nonexistent "report".
    const msgs = useNotificationStore.getState().items;
    expect(msgs.some((n) => /see report/i.test(n.detail ?? ""))).toBe(false);
  });

  it("shows no failure panel on a clean move", async () => {
    const user = userEvent.setup();
    (invoke as ReturnType<typeof vi.fn>).mockImplementation(
      async (_cmd: string, args: { method: string }) => {
        if (args.method === "handle_duplicates") {
          return { moved: 5, deleted: 0, errors: 0, error_messages: [], journal_path: "D:/j.jsonl" };
        }
        return emptyReport;
      },
    );
    useOperationStore.setState({ duplicateReport: reportWithOneGroup() });

    render(<DuplicatesView />);
    await user.click(screen.getByRole("button", { name: /move to review folder/i }));
    await user.click(await screen.findByRole("button", { name: /yes, move files/i }));

    await waitFor(() => {
      const msgs = useNotificationStore.getState().items.map((n) => n.message);
      expect(msgs).toContain("Moved 5 duplicates");
    });
    expect(screen.queryByText(/couldn't be moved/i)).not.toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Audio-fingerprint-tool (fpcalc) FAILURE banner. The tool is auto-provisioned
// now, so the banner is failure-only: it shows the classified reason + the
// automatic-retry promise, uses plain-user language (never "fpcalc"), and is
// absent when fingerprinting succeeded.
// ---------------------------------------------------------------------------

describe("<DuplicatesView /> — audio fingerprint tool banner", () => {
  beforeEach(() => {
    useLibraryStore.setState({ libraryPath: "D:/Music" });
  });

  it("renders the failure banner with the classified reason and retry note", () => {
    const report = reportWithOneGroup();
    report.summary.fpcalc_available = false;
    report.summary.fpcalc_error = "the download didn't complete (check your connection)";
    useOperationStore.setState({ duplicateReport: report });

    render(<DuplicatesView />);

    // Plain-user headline + the real reason + automatic-retry, and NEVER the
    // bare binary name in what the user reads.
    expect(screen.getByText(/audio fingerprint tool/i)).toBeInTheDocument();
    expect(
      screen.getByText(/the download didn't complete \(check your connection\)/i),
    ).toBeInTheDocument();
    expect(screen.getByText(/retry automatically next time/i)).toBeInTheDocument();
    expect(screen.queryByText(/fpcalc/i)).not.toBeInTheDocument();
  });

  it("shows no banner when fingerprinting succeeded", () => {
    const report = reportWithOneGroup();
    report.summary.fpcalc_available = true;
    report.summary.fpcalc_error = null;
    useOperationStore.setState({ duplicateReport: report });

    render(<DuplicatesView />);

    expect(screen.queryByText(/audio fingerprint tool/i)).not.toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// A destructive resolve must report a cancelled batch as
// cancelled, drop the files it removed from the in-memory library, and run
// under its own operation kind rather than the read-only scan's.
// ---------------------------------------------------------------------------

describe("<DuplicatesView /> — destructive resolve", () => {
  beforeEach(() => {
    useLibraryStore.setState({
      libraryPath: "D:/Music",
      tracks: [
        { path: "D:/Music/a.mp3", filename: "a.mp3" },
        { path: "D:/Music/a (1).mp3", filename: "a (1).mp3" },
      ] as unknown as TrackAnalysis[],
    });
    // Toasts are not reset between tests; start from an empty stack so
    // `items.at(-1)` is unambiguously this test's.
    useNotificationStore.setState({ items: [] });
    useConfigStore.getState().updateDuplicates({ review_folder: "D:/Review" });
  });

  /** Run a trash resolve; `summary` is what handle_duplicates returns. */
  async function trashWith(summary: Record<string, unknown>) {
    const user = userEvent.setup();
    (invoke as ReturnType<typeof vi.fn>).mockImplementation(
      async (_cmd: string, args: { method: string }) => {
        if (args.method === "handle_duplicates") return summary;
        return emptyReport; // find_duplicates rescan
      },
    );
    useOperationStore.setState({ duplicateReport: reportWithOneGroup() });

    render(<DuplicatesView />);
    await user.click(screen.getByRole("button", { name: /send to trash/i }));
    await user.click(await screen.findByRole("button", { name: /yes, send to trash/i }));
    return user;
  }

  it("says a cancelled batch was cancelled instead of reporting plain success", async () => {
    await trashWith({ moved: 0, deleted: 0, errors: 0, cancelled: true });

    await waitFor(() => {
      const note = useNotificationStore.getState().items.at(-1);
      expect(note).toBeTruthy();
      expect(note!.message).toMatch(/cancelled before finishing/i);
      expect(note!.kind).not.toBe("success");
    });
  });

  it("drops the trashed files from the in-memory library", async () => {
    await trashWith({ moved: 0, deleted: 1, errors: 0 });

    await waitFor(() => {
      expect(useLibraryStore.getState().tracks.map((t) => t.path)).toEqual([
        "D:/Music/a.mp3",
      ]);
    });
  });

  it("keeps a file the sidecar reported as a failure", async () => {
    await trashWith({
      moved: 0,
      deleted: 0,
      errors: 1,
      error_messages: ["D:/Music/a (1).mp3: trash failed — [Errno 13] denied"],
    });

    // Nothing succeeded, so the library is untouched.
    await waitFor(() => {
      expect(useLibraryStore.getState().tracks).toHaveLength(2);
    });
  });

  it("registers the destructive batch under its own operation kind", async () => {
    const kinds: (string | null)[] = [];
    const user = userEvent.setup();
    (invoke as ReturnType<typeof vi.fn>).mockImplementation(
      async (_cmd: string, args: { method: string }) => {
        if (args.method === "handle_duplicates") {
          kinds.push(useOperationStore.getState().active);
          return { moved: 0, deleted: 1, errors: 0 };
        }
        return emptyReport;
      },
    );
    useOperationStore.setState({ duplicateReport: reportWithOneGroup() });

    render(<DuplicatesView />);
    await user.click(screen.getByRole("button", { name: /send to trash/i }));
    await user.click(await screen.findByRole("button", { name: /yes, send to trash/i }));

    await waitFor(() => expect(kinds).toHaveLength(1));
    // "dedupe" is the read-only scan; the sidecar's own kind for this call is
    // "dedupe-handle" (vibechek/rpc.py::_CANCELLABLE_METHODS).
    expect(kinds[0]).toBe("dedupe-handle");
  });
});

// ---------------------------------------------------------------------------
// The helpers that map a report onto the files the sidecar acts
// on, and that give each group a UNIQUE id.
// ---------------------------------------------------------------------------

describe("duplicate group bookkeeping", () => {
  /** Two audio groups that share a backend key — what `keep_all_formats` emits
   *  for one acoustic cluster present as both WAV and FLAC. */
  function collidingReport(): DuplicateReport {
    const wavKeep = fileInfo("D:/Music/T_tagged.wav");
    const wavDup = fileInfo("D:/Music/T.wav");
    const flacKeep = fileInfo("D:/Music/T_tagged.flac");
    const flacDup = fileInfo("D:/Music/T.flac");
    return {
      ...emptyReport,
      audio_duplicates: [
        { method: "chromaprint", key: "same-fp", keep: wavKeep, duplicates: [wavDup], recoverable_mb: 5 },
        { method: "chromaprint", key: "same-fp", keep: flacKeep, duplicates: [flacDup], recoverable_mb: 5 },
      ],
    };
  }

  it("plans every group's duplicates even when two groups share a key", () => {
    expect(plannedDuplicatePaths(collidingReport())).toEqual([
      "D:/Music/T.wav",
      "D:/Music/T.flac",
    ]);
    expect(countDuplicates(collidingReport())).toBe(2);
  });

  it("never acts on a file that is the keeper of another group", () => {
    // Mirrors handle_duplicates' defence-in-depth: a path that is ANY group's
    // keeper is never acted on, even when another (stale/overlapping) group
    // lists it as a duplicate. Both paths here are keepers, so nothing is
    // planned — the same answer duplicates.py gives.
    const keep = fileInfo("D:/Music/a.mp3");
    const dup = fileInfo("D:/Music/b.mp3");
    const report: DuplicateReport = {
      ...emptyReport,
      exact_duplicates: [
        { method: "md5", key: "g1", keep, duplicates: [dup], recoverable_mb: 5 },
        { method: "md5", key: "g2", keep: dup, duplicates: [keep], recoverable_mb: 5 },
      ],
    };
    expect(plannedDuplicatePaths(report)).toEqual([]);

    // With a third, non-keeper file the surviving duplicate IS planned.
    const other = fileInfo("D:/Music/c.mp3");
    expect(
      plannedDuplicatePaths({
        ...emptyReport,
        exact_duplicates: [
          { method: "md5", key: "g1", keep, duplicates: [dup, other], recoverable_mb: 5 },
          { method: "md5", key: "g2", keep: dup, duplicates: [keep], recoverable_mb: 5 },
        ],
      }),
    ).toEqual(["D:/Music/c.mp3"]);
  });

  it("maps a partial run onto the files it got through, minus the failures", () => {
    const report = collidingReport();
    // Processed 2, one failed → 1 succeeded.
    expect(
      resolvedDuplicatePaths(report, 1, 1, ["D:/Music/T.wav: trash failed — denied"]),
    ).toEqual(["D:/Music/T.flac"]);
    // Cancelled after the first file → only that one.
    expect(resolvedDuplicatePaths(report, 1, 0, [])).toEqual(["D:/Music/T.wav"]);
  });

  it("returns nothing rather than guessing when the counts don't line up", () => {
    const report = collidingReport();
    // The sidecar claims more successes than the report has files.
    expect(resolvedDuplicatePaths(report, 9, 0, [])).toEqual([]);
    expect(resolvedDuplicatePaths(report, 0, 0, [])).toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// Skipping one group must not silently drop its key-twin.
//
// Driven through `applyChoices` (the function that builds the payload the
// destructive RPC receives) rather than the rendered cards: the group list is
// virtualized and react-virtuoso renders no rows in jsdom.
// ---------------------------------------------------------------------------

describe("applyChoices with two groups that share a backend key", () => {
  function collidingReport(): DuplicateReport {
    const wavKeep = fileInfo("D:/Music/T_tagged.wav");
    const wavDup = fileInfo("D:/Music/T.wav");
    const flacKeep = fileInfo("D:/Music/T_tagged.flac");
    const flacDup = fileInfo("D:/Music/T.flac");
    return {
      ...emptyReport,
      audio_duplicates: [
        { method: "chromaprint", key: "same-fp", keep: wavKeep, duplicates: [wavDup], recoverable_mb: 5 },
        { method: "chromaprint", key: "same-fp", keep: flacKeep, duplicates: [flacDup], recoverable_mb: 5 },
      ],
    };
  }

  it("gives the two groups distinct ids", () => {
    const ids = keyGroups(collidingReport()).map((kg) => kg.id);
    expect(new Set(ids).size).toBe(2);
  });

  it("drops only the skipped group, not its key-twin", () => {
    const report = collidingReport();
    const ids = keyGroups(report).map((kg) => kg.id);
    const filtered = applyChoices(report, DEFAULT_RULES, {}, new Set([ids[0]]));

    expect(filtered.audio_duplicates).toHaveLength(1);
    // The survivor is the FLAC group (which file it keeps is the rules' call);
    // nothing from the skipped WAV group is planned.
    const survivor = filtered.audio_duplicates[0];
    expect([survivor.keep.path, ...survivor.duplicates.map((d) => d.path)].sort()).toEqual([
      "D:/Music/T.flac",
      "D:/Music/T_tagged.flac",
    ]);
    expect(plannedDuplicatePaths(filtered)).toHaveLength(1);
    expect(plannedDuplicatePaths(filtered)[0]).toMatch(/\.flac$/);
  });

  it("applies a keeper override to one group only", () => {
    const report = collidingReport();
    const ids = keyGroups(report).map((kg) => kg.id);
    const filtered = applyChoices(
      report,
      DEFAULT_RULES,
      { [ids[0]]: "D:/Music/T.wav" },
      new Set(),
    );

    expect(filtered.audio_duplicates[0].keep.path).toBe("D:/Music/T.wav");
    // The twin is untouched by the other group's override.
    expect(filtered.audio_duplicates[1].keep.path).toMatch(/\.flac$/);
    expect(plannedDuplicatePaths(filtered)).toContain("D:/Music/T_tagged.wav");
  });
});

// ---------------------------------------------------------------------------
// The review folder must be an ABSOLUTE path. `rpc.py::_validated_review_folder`
// rejects a relative one with INVALID_PARAMS (it would resolve against the
// sidecar's working directory and scatter the duplicates somewhere the user
// can't find), so the client guard has to reject it too — before the round trip.
// ---------------------------------------------------------------------------

describe("<DuplicatesView /> — review folder must be absolute", () => {
  beforeEach(() => {
    useLibraryStore.setState({ libraryPath: "D:/Music" });
    (invoke as ReturnType<typeof vi.fn>).mockImplementation(
      async (_cmd: string, args: { method: string }) => {
        if (args.method === "handle_duplicates") {
          return { moved: 1, deleted: 0, errors: 0 };
        }
        return emptyReport;
      },
    );
    useOperationStore.setState({ duplicateReport: reportWithOneGroup() });
  });

  it("refuses a relative review folder without calling the sidecar", async () => {
    const user = userEvent.setup();
    useConfigStore.getState().updateDuplicates({ review_folder: "dupes/review" });

    render(<DuplicatesView />);
    await user.click(screen.getByRole("button", { name: /move to review folder/i }));

    // The precondition banner names the actual rule…
    expect(await screen.findByText(/must be an absolute path/i)).toBeInTheDocument();
    // …no confirm modal, and nothing reached the sidecar.
    expect(
      screen.queryByRole("button", { name: /yes, move files/i }),
    ).not.toBeInTheDocument();
    expect(
      (invoke as ReturnType<typeof vi.fn>).mock.calls.some(
        (c) => (c[1] as { method: string }).method === "handle_duplicates",
      ),
    ).toBe(false);
  });

  it("accepts a Windows drive path and a POSIX path", async () => {
    const user = userEvent.setup();
    useConfigStore.getState().updateDuplicates({ review_folder: "D:/Review" });

    render(<DuplicatesView />);
    await user.click(screen.getByRole("button", { name: /move to review folder/i }));

    expect(await screen.findByRole("button", { name: /yes, move files/i })).toBeInTheDocument();
    expect(screen.queryByText(/must be an absolute path/i)).not.toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Wire additions on the dedupe summary: `journal_incomplete` (the undo
// record only covers part of the run) plus the exact `deleted_paths` /
// `moved_pairs` the sidecar acted on, which beat reconstructing the list from
// counts.
// ---------------------------------------------------------------------------

describe("<DuplicatesView /> — dedupe undo record and resolved paths", () => {
  beforeEach(() => {
    useLibraryStore.setState({
      libraryPath: "D:/Music",
      tracks: [
        { path: "D:/Music/a.mp3", filename: "a.mp3", extension: ".mp3", size_mb: 5,
          existing_tags: {}, ml_analysis: null, error: null },
        { path: "D:/Music/a (1).mp3", filename: "a (1).mp3", extension: ".mp3", size_mb: 5,
          existing_tags: {}, ml_analysis: null, error: null },
      ] as TrackAnalysis[],
    });
    useConfigStore.getState().updateDuplicates({ review_folder: "D:/Review" });
  });

  function mockHandle(summary: Record<string, unknown>) {
    (invoke as ReturnType<typeof vi.fn>).mockImplementation(
      async (_cmd: string, args: { method: string }) => {
        if (args.method === "handle_duplicates") return summary;
        return emptyReport;
      },
    );
    useOperationStore.setState({ duplicateReport: reportWithOneGroup() });
  }

  it("warns that undo will only restore part of the run", async () => {
    const user = userEvent.setup();
    mockHandle({
      moved: 1, deleted: 0, errors: 0,
      journal_path: "D:/j.jsonl",
      journal_incomplete: true,
    });

    render(<DuplicatesView />);
    await user.click(screen.getByRole("button", { name: /move to review folder/i }));
    await user.click(await screen.findByRole("button", { name: /yes, move files/i }));

    await waitFor(() => {
      const toast = useNotificationStore
        .getState()
        .items.find((n) => n.message === "Moved 1 duplicate");
      expect(toast).toBeTruthy();
      expect(toast!.detail).toMatch(/undo record is incomplete/i);
      expect(toast!.detail).toMatch(/only part of this run/i);
      expect(toast!.kind).toBe("warning");
    });
  });

  it("keeps the plain undo hint when the journal is complete", async () => {
    const user = userEvent.setup();
    mockHandle({ moved: 1, deleted: 0, errors: 0, journal_path: "D:/j.jsonl" });

    render(<DuplicatesView />);
    await user.click(screen.getByRole("button", { name: /move to review folder/i }));
    await user.click(await screen.findByRole("button", { name: /yes, move files/i }));

    await waitFor(() => {
      const toast = useNotificationStore
        .getState()
        .items.find((n) => n.message === "Moved 1 duplicate");
      expect(toast).toBeTruthy();
      expect(toast!.detail).toMatch(/Undo available in "Recent operations"/);
      expect(toast!.detail).not.toMatch(/incomplete/i);
      expect(toast!.kind).toBe("success");
    });
  });

  it("removes exactly the paths the sidecar says it trashed", async () => {
    const user = userEvent.setup();
    // The counts alone would have the inference remove the FIRST planned entry;
    // the sidecar names a different one, and its list must win.
    mockHandle({
      moved: 0, deleted: 1, errors: 0,
      deleted_paths: ["D:/Music/a.mp3"],
    });

    render(<DuplicatesView />);
    await user.click(screen.getByRole("button", { name: /send to trash/i }));
    await user.click(await screen.findByRole("button", { name: /yes, send to trash/i }));

    await waitFor(() => {
      const paths = useLibraryStore.getState().tracks.map((t) => t.path);
      expect(paths).toEqual(["D:/Music/a (1).mp3"]);
    });
  });

  // A MOVE is not a removal. `moved_pairs` carries the real
  // destination, so a row whose file stayed INSIDE the library must FOLLOW it
  // the way organize's does; dropping it hides the track outright when the
  // review folder sits inside the library.
  it("re-points the rows a move INSIDE the library relocated, rather than dropping them", async () => {
    const user = userEvent.setup();
    useConfigStore.getState().updateDuplicates({ review_folder: "D:/Music/Review" });
    mockHandle({
      moved: 1, deleted: 0, errors: 0,
      journal_path: "D:/j.jsonl",
      // `_unique_path` renames on collision, so the destination basename is
      // NOT necessarily the source's.
      moved_pairs: [["D:/Music/a.mp3", "D:/Music/Review/a (2).mp3"]],
    });

    render(<DuplicatesView />);
    await user.click(screen.getByRole("button", { name: /move to review folder/i }));
    await user.click(await screen.findByRole("button", { name: /yes, move files/i }));

    await waitFor(() => {
      const paths = useLibraryStore.getState().tracks.map((t) => t.path).sort();
      expect(paths).toEqual(["D:/Music/Review/a (2).mp3", "D:/Music/a (1).mp3"]);
    });
    // ...and the moved row's filename follows it too — `filename` is a stored
    // field, and a stale one renders the OLD name in the library table for a
    // file the collision rename gave a new one.
    const moved = useLibraryStore
      .getState()
      .tracks.find((t) => t.path === "D:/Music/Review/a (2).mp3");
    expect(moved).toBeTruthy();
    expect(moved!.filename).toBe("a (2).mp3");
  });

  // ...but a review folder is normally OUTSIDE the library (it is required to
  // be absolute, and D:/Dupes is the shape the Settings field invites). A row
  // that keeps pointing at a quarantined copy is a LIVE row: organize plans
  // every track it can resolve into `<target>/<Genre>/`, so the next Organize
  // files the duplicates the user just quarantined straight back into the
  // library — under genre folders where they no longer even collide with their
  // keepers. Leaving the library is a removal.
  it("drops the rows a move to a review folder OUTSIDE the library took away", async () => {
    const user = userEvent.setup();
    // The beforeEach library is D:/Music; the review folder is not under it.
    mockHandle({
      moved: 1, deleted: 0, errors: 0,
      journal_path: "D:/j.jsonl",
      moved_pairs: [["D:/Music/a.mp3", "D:/Review/a.mp3"]],
    });

    render(<DuplicatesView />);
    await user.click(screen.getByRole("button", { name: /move to review folder/i }));
    await user.click(await screen.findByRole("button", { name: /yes, move files/i }));

    await waitFor(() => {
      const paths = useLibraryStore.getState().tracks.map((t) => t.path);
      expect(paths).toEqual(["D:/Music/a (1).mp3"]);
    });
    // Nothing in the library points into the review folder.
    expect(
      useLibraryStore.getState().tracks.some((t) => t.path.startsWith("D:/Review/")),
    ).toBe(false);
  });

  it("splits a mixed batch: inside re-pathed, outside dropped", async () => {
    const user = userEvent.setup();
    useLibraryStore.setState({
      tracks: [
        { path: "D:/Music/a.mp3", filename: "a.mp3", extension: ".mp3", size_mb: 5,
          existing_tags: {}, ml_analysis: null, error: null },
        { path: "D:/Music/a (1).mp3", filename: "a (1).mp3", extension: ".mp3", size_mb: 5,
          existing_tags: {}, ml_analysis: null, error: null },
      ] as TrackAnalysis[],
    });
    mockHandle({
      moved: 2, deleted: 0, errors: 0,
      journal_path: "D:/j.jsonl",
      moved_pairs: [
        ["D:/Music/a.mp3", "D:/Music/Review/a.mp3"],
        ["D:/Music/a (1).mp3", "D:/Dupes/a (1).mp3"],
      ],
    });

    render(<DuplicatesView />);
    await user.click(screen.getByRole("button", { name: /move to review folder/i }));
    await user.click(await screen.findByRole("button", { name: /yes, move files/i }));

    await waitFor(() => {
      const paths = useLibraryStore.getState().tracks.map((t) => t.path);
      expect(paths).toEqual(["D:/Music/Review/a.mp3"]);
    });
  });

  it("still drops the rows for a move a sidecar reports without destinations", async () => {
    const user = userEvent.setup();
    // Old sidecar: counts only. We know a file left D:/Music but not where it
    // landed, so the row can only go.
    mockHandle({ moved: 1, deleted: 0, errors: 0, journal_path: "D:/j.jsonl" });

    render(<DuplicatesView />);
    await user.click(screen.getByRole("button", { name: /move to review folder/i }));
    await user.click(await screen.findByRole("button", { name: /yes, move files/i }));

    await waitFor(() => {
      // plannedDuplicatePaths order: the group's duplicate, never the keeper.
      const paths = useLibraryStore.getState().tracks.map((t) => t.path);
      expect(paths).toEqual(["D:/Music/a.mp3"]);
    });
  });

  it("still falls back to the inferred list for a sidecar that sends neither", async () => {
    const user = userEvent.setup();
    mockHandle({ moved: 0, deleted: 1, errors: 0 });

    render(<DuplicatesView />);
    await user.click(screen.getByRole("button", { name: /send to trash/i }));
    await user.click(await screen.findByRole("button", { name: /yes, send to trash/i }));

    // plannedDuplicatePaths order: the group's duplicate, never the keeper.
    await waitFor(() => {
      const paths = useLibraryStore.getState().tracks.map((t) => t.path);
      expect(paths).toEqual(["D:/Music/a.mp3"]);
    });
  });
});

// ---------------------------------------------------------------------------
// `find_duplicates` is on the store's NON_REPLAYABLE_METHODS list, so a failed
// scan gets no Try-again button unless the view hands fail() its own retry
// closure. It used to call plain `fail(e)` — a dead end after a 10-minute scan.
// ---------------------------------------------------------------------------

describe("<DuplicatesView /> — a failed scan stays retryable", () => {
  it("attaches a retryAction that re-runs the scan", async () => {
    const user = userEvent.setup();
    let calls = 0;
    (invoke as ReturnType<typeof vi.fn>).mockImplementation(
      async (_cmd: string, args: { method: string }) => {
        if (args.method === "find_duplicates") {
          calls += 1;
          if (calls === 1) throw new Error("fingerprint tool went away");
          return emptyReport;
        }
        return {};
      },
    );
    useLibraryStore.setState({ libraryPath: "D:/Music" });

    render(<DuplicatesView />);
    await user.click(screen.getByRole("button", { name: /scan/i }));

    await waitFor(() => {
      const info = useOperationStore.getState().errorInfo;
      expect(info).toBeTruthy();
      expect(info!.retryAction).toBeTypeOf("function");
    });

    // The closure re-enters the real path, so the report lands in the store.
    await act(async () => {
      await useOperationStore.getState().errorInfo!.retryAction!();
    });
    expect(calls).toBe(2);
    expect(useOperationStore.getState().duplicateReport).toEqual(emptyReport);
  });
});

// ---------------------------------------------------------------------------
// The sidecar syncs the SAVED analysis after a dedupe
// (rpc.py::_prune_analysis_after_dedupe). Without a `library_path` in the
// request it has to INFER which library that is by testing recents entries for
// path-ancestry over the trashed/moved files — a guess that misses a library
// recorded under a different spelling of the same folder, and can touch a
// second, nested library's analysis. Send the loaded path.
// ---------------------------------------------------------------------------

describe("<DuplicatesView /> — library_path on handle_duplicates", () => {
  beforeEach(() => {
    useConfigStore.getState().updateDuplicates({ review_folder: "D:/Review" });
  });

  function mockHandle(): { params: () => Record<string, unknown> | undefined } {
    let seen: Record<string, unknown> | undefined;
    (invoke as ReturnType<typeof vi.fn>).mockImplementation(
      async (_cmd: string, args: { method: string; params?: Record<string, unknown> }) => {
        if (args.method === "handle_duplicates") {
          seen = args.params;
          return { moved: 0, deleted: 1, errors: 0 };
        }
        return emptyReport; // the follow-up rescan
      },
    );
    return { params: () => seen };
  }

  it("sends the loaded library path so the sidecar need not infer it", async () => {
    const user = userEvent.setup();
    useLibraryStore.setState({ libraryPath: "D:/Music" });
    const handle = mockHandle();
    useOperationStore.setState({ duplicateReport: reportWithOneGroup() });

    render(<DuplicatesView />);
    await user.click(screen.getByRole("button", { name: /send to trash/i }));
    await user.click(await screen.findByRole("button", { name: /yes, send to trash/i }));

    await waitFor(() => expect(handle.params()).toBeTruthy());
    expect(handle.params()!.library_path).toBe("D:/Music");
  });

  it("omits the key entirely when no library is loaded", async () => {
    const user = userEvent.setup();
    // An empty string would match no recents row AND suppress the sidecar's
    // ancestry fallback — leaving the saved analysis full of ghost rows.
    useLibraryStore.setState({ libraryPath: null });
    const handle = mockHandle();
    useOperationStore.setState({ duplicateReport: reportWithOneGroup() });

    render(<DuplicatesView />);
    await user.click(screen.getByRole("button", { name: /send to trash/i }));
    await user.click(await screen.findByRole("button", { name: /yes, send to trash/i }));

    await waitFor(() => expect(handle.params()).toBeTruthy());
    expect("library_path" in handle.params()!).toBe(false);
  });
});
