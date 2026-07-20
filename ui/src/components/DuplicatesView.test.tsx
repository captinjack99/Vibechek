/**
 * Regression test for the audit finding "Chromaprint similarity-threshold
 * setting silently ignored by dedupe scan" (MED, frontend).
 *
 * The Settings slider writes `duplicates.chromaprint_similarity_threshold`, but
 * `handleScan` used to send only `{ path, use_md5, use_chromaprint }` to
 * `find_duplicates` — so the slider was dead. This asserts the configured
 * threshold actually reaches the RPC payload (the backend reads it as
 * `threshold`, see rpc._find_duplicates).
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { invoke } from "@tauri-apps/api/core";

import { DuplicatesView } from "./DuplicatesView";
import {
  useConfigStore,
  useLibraryStore,
  useNotificationStore,
  useOperationStore,
} from "../stores";
import type { DuplicateReport, FileInfo } from "../types";

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
// WP-E1: partial-failure surfacing. `handle_duplicates` returns a per-file
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
