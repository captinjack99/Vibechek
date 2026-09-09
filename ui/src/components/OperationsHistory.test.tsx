/**
 * OperationsHistory undo partial-failure surfacing.
 *
 * `revert_journal` returns per-file `error_messages` (path + reason) in its
 * summary; the toast only ever showed counts, so a DJ couldn't tell WHICH
 * tracks didn't move back. These tests pin the durable in-view panel that lists
 * them, and assert a clean undo stays quiet.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { invoke } from "@tauri-apps/api/core";

import { OperationsHistory } from "./OperationsHistory";
import { useLibraryStore, useNotificationStore, useUIStore } from "../stores";
import type { TrackAnalysis } from "../types";

type MockFn = ReturnType<typeof vi.fn>;

const ONE_ORGANIZE_JOURNAL = {
  journals: [
    {
      path: "D:/journals/organize-1.jsonl",
      kind: "organize",
      started_at: Math.floor(Date.now() / 1000) - 120,
      root: "D:/Music",
      move_count: 3,
      trash_count: 0,
    },
  ],
};

function routeInvoke(revertResult: unknown) {
  (invoke as MockFn).mockImplementation(
    async (_cmd: string, args: { method: string }) => {
      if (args.method === "list_journals") return ONE_ORGANIZE_JOURNAL;
      if (args.method === "revert_journal") return revertResult;
      return {};
    },
  );
}

describe("<OperationsHistory /> — undo partial-failure panel", () => {
  beforeEach(() => {
    useUIStore.setState({ historyOpen: true });
  });

  it("lists the files that couldn't be moved back after a partial undo", async () => {
    const user = userEvent.setup();
    routeInvoke({
      reverted: 1,
      skipped: 1,
      errors: 1,
      trashed_not_reverted: 0,
      error_messages: [
        "D:/Music/House/a.mp3 -> D:/Music/a.mp3: [Errno 13] Permission denied",
      ],
      reverted_pairs: [["D:/Music/House/b.mp3", "D:/Music/b.mp3"]],
    });

    render(<OperationsHistory />);
    await user.click(await screen.findByRole("button", { name: /undo/i }));

    expect(
      await screen.findByText(/some files stayed in place/i),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/\[Errno 13\] Permission denied/i),
    ).toBeInTheDocument();
  });

  it("stays quiet after a fully-clean undo", async () => {
    const user = userEvent.setup();
    routeInvoke({
      reverted: 3,
      skipped: 0,
      errors: 0,
      trashed_not_reverted: 0,
      error_messages: [],
      reverted_pairs: [],
    });

    render(<OperationsHistory />);
    await user.click(await screen.findByRole("button", { name: /undo/i }));

    // Wait for the revert to actually complete (its success toast), then assert
    // no partial-failure panel rendered.
    await waitFor(() => {
      const msgs = useNotificationStore.getState().items.map((n) => n.message);
      expect(msgs.some((m) => /undo complete/i.test(m))).toBe(true);
    });
    expect(screen.queryByText(/stayed in place/i)).not.toBeInTheDocument();
  });
});

/**
 * A cancelled revert must not resolve like a success.
 *
 * The sidecar now returns the partial summary with `cancelled: true` instead
 * of rejecting, and its skipped/errors are 0 because the remaining entries
 * were never attempted. Reporting that as "Undo complete — Restored 1" is a
 * confident false claim: most files are still at their organized paths.
 */
describe("<OperationsHistory /> — cancelled undo", () => {
  beforeEach(() => {
    useUIStore.setState({ historyOpen: true });
  });

  const CANCELLED = {
    reverted: 1,
    skipped: 0,
    errors: 0,
    trashed_not_reverted: 0,
    error_messages: [] as string[],
    reverted_pairs: [["D:/Music/House/b.mp3", "D:/Music/b.mp3"]] as [string, string][],
    cancelled: true,
  };

  it("reports it as cancelled, not complete", async () => {
    const user = userEvent.setup();
    routeInvoke(CANCELLED);

    render(<OperationsHistory />);
    await user.click(await screen.findByRole("button", { name: /undo/i }));

    await waitFor(() => {
      const toast = useNotificationStore
        .getState()
        .items.find((n) => /undo cancelled/i.test(n.message));
      expect(toast).toBeTruthy();
      // 3 = the journal's move_count, i.e. what the run had to put back.
      expect(toast!.message).toMatch(/restored 1 of 3 before stopping/i);
      expect(toast!.kind).not.toBe("success");
    });
    const msgs = useNotificationStore.getState().items.map((n) => n.message);
    expect(msgs.some((m) => /undo complete/i.test(m))).toBe(false);
  });

  it("still re-paths the rows it did restore", async () => {
    const user = userEvent.setup();
    routeInvoke(CANCELLED);
    useLibraryStore.setState({
      libraryPath: "D:/Music",
      tracks: [
        { path: "D:/Music/House/a.mp3", filename: "a.mp3" },
        { path: "D:/Music/House/b.mp3", filename: "b.mp3" },
      ] as unknown as TrackAnalysis[],
    });

    render(<OperationsHistory />);
    await user.click(await screen.findByRole("button", { name: /undo/i }));

    await waitFor(() => {
      const paths = useLibraryStore.getState().tracks.map((t) => t.path).sort();
      expect(paths).toEqual(["D:/Music/House/a.mp3", "D:/Music/b.mp3"]);
    });
  });
});
