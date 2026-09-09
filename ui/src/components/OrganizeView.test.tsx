/**
 * OrganizeView undo partial-failure surfacing.
 *
 * After an organize, the result panel offers "Undo this organize". The revert
 * summary carries per-file `error_messages`, but the panel only toasted counts.
 * This drives the full flow (preview → execute → undo) and asserts the durable
 * in-view list of files that couldn't be moved back.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { invoke } from "@tauri-apps/api/core";
import { save } from "@tauri-apps/plugin-dialog";

import { OrganizeView } from "./OrganizeView";
import { useLibraryStore, useNotificationStore, useOperationStore } from "../stores";
import type { OrganizePlan, TrackAnalysis } from "../types";

type MockFn = ReturnType<typeof vi.fn>;

/** Minimal in-memory track so the in-memory organize source is available. */
function track(path: string, genre: string | null): TrackAnalysis {
  return {
    path,
    filename: path.split(/[/\\]/).pop() ?? path,
    extension: ".mp3",
    size_mb: 5,
    filename_artist: null,
    filename_title: null,
    filename_bpm: null,
    filename_key: null,
    filename_mix: null,
    existing_tags: {},
    ml_analysis: genre ? { ml_genre: genre } : null,
    error: null,
  } as unknown as TrackAnalysis;
}

const PLAN: OrganizePlan = {
  base_dir: "D:/Music",
  moves: [
    {
      source: "D:/Music/a.mp3",
      destination: "D:/Music/House/a.mp3",
      genre: "House",
      subgenre: "",
      reason: "ml_genre",
      relative_destination: "House/a.mp3",
      original_source: "D:/Music/a.mp3",
    },
  ],
  small_genres: [],
  genre_counts: { House: 1 },
  existing_genre_counts: { House: 0 },
  errors: [],
};

describe("<OrganizeView /> — undo partial-failure list", () => {
  beforeEach(() => {
    useLibraryStore.setState({
      libraryPath: "D:/Music",
      tracks: [track("D:/Music/a.mp3", "House")],
    });
    // "Back up all tags first" is on by default → a save location is prompted.
    (save as MockFn).mockResolvedValue("D:/backup.json");
  });

  /** Mount, run organize to completion, and click Undo. `revert` is what the
   *  revert_journal RPC returns. */
  async function organizeThenUndo(revert: unknown) {
    const user = userEvent.setup();
    (invoke as MockFn).mockImplementation(
      async (_cmd: string, args: { method: string }) => {
        switch (args.method) {
          case "plan_organization":
            return PLAN;
          case "backup_tags":
            return { total: 1, backed_up: 1, not_fully_backed_up: 0, errors: [] };
          case "organize":
            return {
              planned: 1,
              moved: 1,
              errors: [],
              journal_path: "D:/journals/organize-1.jsonl",
              moved_pairs: [["D:/Music/a.mp3", "D:/Music/House/a.mp3"]],
            };
          case "revert_journal":
            return revert;
          default:
            return {};
        }
      },
    );

    render(<OrganizeView />);
    await user.click(await screen.findByRole("button", { name: /preview plan/i }));
    await user.click(await screen.findByRole("button", { name: /execute \(1 moves\)/i }));
    await user.click(await screen.findByRole("button", { name: /yes, move files/i }));
    await user.click(await screen.findByRole("button", { name: /undo this organize/i }));
    return user;
  }

  it("lists the files an undo couldn't move back", async () => {
    await organizeThenUndo({
      reverted: 0,
      skipped: 0,
      errors: 1,
      trashed_not_reverted: 0,
      error_messages: [
        "D:/Music/House/a.mp3 -> D:/Music/a.mp3: [Errno 13] Permission denied",
      ],
      reverted_pairs: [],
    });

    expect(await screen.findByText(/undo left 1 file in place/i)).toBeInTheDocument();
    expect(
      screen.getByText(/\[Errno 13\] Permission denied/i),
    ).toBeInTheDocument();
  });

  it("stays quiet after a fully-clean undo", async () => {
    await organizeThenUndo({
      reverted: 1,
      skipped: 0,
      errors: 0,
      trashed_not_reverted: 0,
      error_messages: [],
      reverted_pairs: [["D:/Music/House/a.mp3", "D:/Music/a.mp3"]],
    });

    // Wait for the undo to complete (success toast), then assert no panel.
    await waitFor(() => {
      const msgs = useNotificationStore.getState().items.map((n) => n.message);
      expect(msgs.some((m) => /undo complete/i.test(m))).toBe(true);
    });
    expect(screen.queryByText(/in place/i)).not.toBeInTheDocument();
  });
});

/**
 * Empty-folder pruning after an in-place re-organize.
 *
 * Removing a directory is the most destructive thing this view does, so the
 * contract under test is the GATE: organize alone must never remove anything,
 * and the RPC must not fire until the user confirms.
 */
describe("<OrganizeView /> — pruning the folders an organize emptied", () => {
  beforeEach(() => {
    useLibraryStore.setState({
      libraryPath: "D:/Music",
      tracks: [track("D:/Music/a.mp3", "House")],
    });
    (save as MockFn).mockResolvedValue("D:/backup.json");
  });

  /** Mount and organize to completion. `emptied` is the sidecar's emptied_dirs. */
  async function organizeWithEmptied(emptied: string[]) {
    const user = userEvent.setup();
    const prune = vi.fn().mockResolvedValue({
      removed: emptied,
      skipped: [],
      errors: [],
    });
    (invoke as MockFn).mockImplementation(
      async (_cmd: string, args: { method: string; params?: unknown }) => {
        switch (args.method) {
          case "plan_organization":
            return PLAN;
          case "backup_tags":
            return { total: 1, backed_up: 1, not_fully_backed_up: 0, errors: [] };
          case "organize":
            return {
              planned: 1,
              moved: 1,
              errors: [],
              journal_path: "D:/journals/organize-1.jsonl",
              moved_pairs: [["D:/Music/a.mp3", "D:/Music/House/a.mp3"]],
              emptied_dirs: emptied,
            };
          case "prune_empty_folders":
            return prune(args.params);
          default:
            return {};
        }
      },
    );

    render(<OrganizeView />);
    await user.click(await screen.findByRole("button", { name: /preview plan/i }));
    await user.click(await screen.findByRole("button", { name: /execute \(1 moves\)/i }));
    await user.click(await screen.findByRole("button", { name: /yes, move files/i }));
    return { user, prune };
  }

  it("offers to remove emptied folders but does not touch them on its own", async () => {
    const { prune } = await organizeWithEmptied(["D:/Music/Techno"]);

    expect(await screen.findByText(/1 folder is now empty/i)).toBeInTheDocument();
    // The organize itself must not have removed anything.
    expect(prune).not.toHaveBeenCalled();
  });

  it("does not prune until the user confirms", async () => {
    const { user, prune } = await organizeWithEmptied(["D:/Music/Techno"]);

    await user.click(await screen.findByRole("button", { name: /remove empty folders/i }));
    // Confirm dialog is up — still nothing removed.
    expect(prune).not.toHaveBeenCalled();

    await user.click(await screen.findByRole("button", { name: /^remove folders$/i }));

    await waitFor(() => expect(prune).toHaveBeenCalledTimes(1));
    expect(prune).toHaveBeenCalledWith(
      expect.objectContaining({ root: "D:/Music", dirs: ["D:/Music/Techno"] }),
    );
    expect(await screen.findByText(/removed 1 empty folder/i)).toBeInTheDocument();
  });

  it("cancelling the confirm removes nothing", async () => {
    const { user, prune } = await organizeWithEmptied(["D:/Music/Techno"]);

    await user.click(await screen.findByRole("button", { name: /remove empty folders/i }));
    await user.click(await screen.findByRole("button", { name: /cancel/i }));

    expect(prune).not.toHaveBeenCalled();
    // The offer is still there — cancelling declines, it doesn't dismiss.
    expect(screen.getByText(/1 folder is now empty/i)).toBeInTheDocument();
  });

  it("says nothing when no folder was emptied", async () => {
    await organizeWithEmptied([]);

    await screen.findByText(/library organized/i);
    expect(screen.queryByText(/now empty/i)).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /remove empty folders/i }),
    ).not.toBeInTheDocument();
  });
});

/**
 * The confirmed preview MUST match what executes.
 *
 * The plan lives in the global operation store but its staleness key used to be
 * component-local `useState`, so every unmount (Library tab and back) reset the
 * key to null and permanently disarmed the guard for a plan that survived.
 */
describe("<OrganizeView /> — plan staleness survives a remount", () => {
  beforeEach(() => {
    useLibraryStore.setState({
      libraryPath: "D:/Music",
      tracks: [track("D:/Music/a.mp3", "House")],
    });
    useOperationStore.setState({ organizePlan: null, organizePlanKey: null });
    (invoke as MockFn).mockImplementation(
      async (_cmd: string, args: { method: string }) => {
        switch (args.method) {
          case "plan_organization":
            return PLAN;
          default:
            return {};
        }
      },
    );
  });

  it("drops a plan whose library changed while the view was unmounted", async () => {
    const user = userEvent.setup();
    const view = render(<OrganizeView />);
    await user.click(await screen.findByRole("button", { name: /preview plan/i }));
    await screen.findByRole("button", { name: /execute \(1 moves\)/i });

    // Leave the Organize tab (App.tsx renders it conditionally) …
    view.unmount();
    // … open a different library in the Library tab …
    act(() => {
      useLibraryStore.setState({
        libraryPath: "E:/OtherMusic",
        tracks: [track("E:/OtherMusic/z.mp3", "Techno")],
      });
    });
    // … and come back.
    render(<OrganizeView />);

    // The plan the user confirmed described D:/Music. It must not be executable
    // against E:/OtherMusic's freshly-built params.
    await waitFor(() => {
      expect(
        screen.queryByRole("button", { name: /execute \(1 moves\)/i }),
      ).not.toBeInTheDocument();
    });
    expect(useOperationStore.getState().organizePlan).toBeNull();
  });

  it("keeps a plan across a remount when nothing changed", async () => {
    const user = userEvent.setup();
    const view = render(<OrganizeView />);
    await user.click(await screen.findByRole("button", { name: /preview plan/i }));
    await screen.findByRole("button", { name: /execute \(1 moves\)/i });

    view.unmount();
    render(<OrganizeView />);

    expect(
      await screen.findByRole("button", { name: /execute \(1 moves\)/i }),
    ).toBeEnabled();
  });
});

/**
 * The post-organize folder breakdown is the screen the user reads before
 * deciding whether to undo, so it must report what MOVED, not what was planned.
 */
describe("<OrganizeView /> — where files landed", () => {
  const BIG_PLAN: OrganizePlan = {
    ...PLAN,
    moves: [
      { ...PLAN.moves[0] },
      {
        source: "D:/Music/b.mp3",
        destination: "D:/Music/Techno/b.mp3",
        genre: "Techno",
        subgenre: "",
        reason: "ml_genre",
        relative_destination: "Techno/b.mp3",
        original_source: "D:/Music/b.mp3",
      },
    ],
  };

  beforeEach(() => {
    useLibraryStore.setState({
      libraryPath: "D:/Music",
      tracks: [track("D:/Music/a.mp3", "House"), track("D:/Music/b.mp3", "Techno")],
    });
    useOperationStore.setState({ organizePlan: null, organizePlanKey: null });
    (save as MockFn).mockResolvedValue("D:/backup.json");
  });

  it("counts only the folders files actually landed in on a cancelled run", async () => {
    (invoke as MockFn).mockImplementation(
      async (_cmd: string, args: { method: string }) => {
        switch (args.method) {
          case "plan_organization":
            return BIG_PLAN;
          case "backup_tags":
            return { total: 2, backed_up: 2, not_fully_backed_up: 0, errors: [] };
          case "organize":
            // Cancelled after the first move: 1 of 2, House only.
            return {
              planned: 2,
              moved: 1,
              errors: [],
              cancelled: true,
              journal_path: "D:/journals/organize-1.jsonl",
              moved_pairs: [["D:/Music/a.mp3", "D:/Music/House/a.mp3"]],
            };
          default:
            return {};
        }
      },
    );

    const user = userEvent.setup();
    render(<OrganizeView />);
    await user.click(await screen.findByRole("button", { name: /preview plan/i }));
    await user.click(await screen.findByRole("button", { name: /execute \(2 moves\)/i }));
    await user.click(await screen.findByRole("button", { name: /yes, move files/i }));

    // One destination, not two — Techno received nothing.
    await screen.findByText(/where files landed/i);
    const destinations = screen.getByText(/^destinations$/i).parentElement!;
    expect(destinations).toHaveTextContent(/^Destinations1$/);
    expect(screen.queryByText("Techno")).not.toBeInTheDocument();
    expect(screen.getByText("House")).toBeInTheDocument();
  });
});

/**
 * `relative_destination` is optional on the wire. The destructive
 * confirm modal's "First few moves" list must stay readable without it.
 */
describe("<OrganizeView /> — confirm modal destinations", () => {
  beforeEach(() => {
    useLibraryStore.setState({
      libraryPath: "D:/Music",
      tracks: [track("D:/Music/a.mp3", "House")],
    });
    useOperationStore.setState({ organizePlan: null, organizePlanKey: null });
    (save as MockFn).mockResolvedValue("D:/backup.json");
  });

  it("shows a base-dir-relative destination even when the sidecar omits it", async () => {
    const planNoRel = {
      ...PLAN,
      moves: [{ ...PLAN.moves[0], relative_destination: undefined }],
    };
    (invoke as MockFn).mockImplementation(
      async (_cmd: string, args: { method: string }) => {
        switch (args.method) {
          case "plan_organization":
            return planNoRel;
          default:
            return {};
        }
      },
    );

    const user = userEvent.setup();
    render(<OrganizeView />);
    await user.click(await screen.findByRole("button", { name: /preview plan/i }));
    await user.click(await screen.findByRole("button", { name: /execute \(1 moves\)/i }));

    expect(await screen.findByText(/a\.mp3 → House\/a\.mp3/)).toBeInTheDocument();
  });
});

/**
 * An undo that restored nothing is not "Undone".
 */
describe("<OrganizeView /> — undo latch", () => {
  beforeEach(() => {
    useLibraryStore.setState({
      libraryPath: "D:/Music",
      tracks: [track("D:/Music/a.mp3", "House")],
    });
    useOperationStore.setState({ organizePlan: null, organizePlanKey: null });
    (save as MockFn).mockResolvedValue("D:/backup.json");
  });

  /** Mount, organize to completion, click Undo; `revert` is revert_journal's
   *  return. (A local copy — the first describe's helper isn't in scope.) */
  async function runUndo(revert: unknown) {
    const user = userEvent.setup();
    (invoke as MockFn).mockImplementation(
      async (_cmd: string, args: { method: string }) => {
        switch (args.method) {
          case "plan_organization":
            return PLAN;
          case "backup_tags":
            return { total: 1, backed_up: 1, not_fully_backed_up: 0, errors: [] };
          case "organize":
            return {
              planned: 1,
              moved: 1,
              errors: [],
              journal_path: "D:/journals/organize-1.jsonl",
              moved_pairs: [["D:/Music/a.mp3", "D:/Music/House/a.mp3"]],
            };
          case "revert_journal":
            return revert;
          default:
            return {};
        }
      },
    );
    render(<OrganizeView />);
    await user.click(await screen.findByRole("button", { name: /preview plan/i }));
    await user.click(await screen.findByRole("button", { name: /execute \(1 moves\)/i }));
    await user.click(await screen.findByRole("button", { name: /yes, move files/i }));
    await user.click(await screen.findByRole("button", { name: /undo this organize/i }));
  }

  it("leaves the undo retryable when every entry was skipped", async () => {
    await runUndo({
      // The library's drive was unplugged: nothing to move back, no errors.
      reverted: 0,
      skipped: 1,
      errors: 0,
      trashed_not_reverted: 0,
      error_messages: [],
      reverted_pairs: [],
    });

    const retry = await screen.findByRole("button", { name: /retry undo/i });
    expect(retry).toBeEnabled();
    expect(screen.queryByRole("button", { name: /^undone$/i })).not.toBeInTheDocument();
  });

  it("latches Undone only after a clean revert", async () => {
    await runUndo({
      reverted: 1,
      skipped: 0,
      errors: 0,
      trashed_not_reverted: 0,
      error_messages: [],
      reverted_pairs: [["D:/Music/House/a.mp3", "D:/Music/a.mp3"]],
    });

    const undone = await screen.findByRole("button", { name: /undone/i });
    expect(undone).toBeDisabled();
  });
});

// ---------------------------------------------------------------------------
// `OrganizeStats.journal_incomplete` — the sidecar could not record every move
// in the undo journal, so "Undo this organize" restores only a subset. The undo
// journal is the ONLY safety net for a bulk file move, so a half-written one
// has to be stated next to the button, not inferred from a shorter restore.
// ---------------------------------------------------------------------------

describe("<OrganizeView /> — incomplete undo journal", () => {
  beforeEach(() => {
    useLibraryStore.setState({
      libraryPath: "D:/Music",
      tracks: [track("D:/Music/a.mp3", "House")],
    });
    (save as MockFn).mockResolvedValue("D:/backup.json");
  });

  /** Mount and run organize to completion; `organizeExtra` is merged into the
   *  organize RPC result. Returns the userEvent handle. */
  async function organizeWith(organizeExtra: Record<string, unknown>) {
    const user = userEvent.setup();
    (invoke as MockFn).mockImplementation(
      async (_cmd: string, args: { method: string }) => {
        switch (args.method) {
          case "plan_organization":
            return PLAN;
          case "backup_tags":
            return { total: 1, backed_up: 1, not_fully_backed_up: 0, errors: [] };
          case "organize":
            return {
              planned: 1,
              moved: 1,
              errors: [],
              journal_path: "D:/journals/organize-1.jsonl",
              moved_pairs: [["D:/Music/a.mp3", "D:/Music/House/a.mp3"]],
              emptied_dirs: [],
              ...organizeExtra,
            };
          case "revert_journal":
            return {
              reverted: 1, skipped: 0, errors: 0, trashed_not_reverted: 0,
              error_messages: [], reverted_pairs: [],
            };
          default:
            return {};
        }
      },
    );

    render(<OrganizeView />);
    await user.click(await screen.findByRole("button", { name: /preview plan/i }));
    await user.click(await screen.findByRole("button", { name: /execute \(1 moves\)/i }));
    await user.click(await screen.findByRole("button", { name: /yes, move files/i }));
    return user;
  }

  it("warns beside the Undo button when the journal is incomplete", async () => {
    await organizeWith({ journal_incomplete: true });

    // The Undo affordance is still offered — a partial restore beats none.
    expect(
      await screen.findByRole("button", { name: /undo this organize/i }),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/undo record is incomplete/i),
    ).toBeInTheDocument();
    expect(screen.getByText(/only part of this run/i)).toBeInTheDocument();
  });

  it("says so in the completion toast too", async () => {
    await organizeWith({ journal_incomplete: true });

    await waitFor(() => {
      const toast = useNotificationStore
        .getState()
        .items.find((n) => n.message === "Moved 1 of 1 files");
      expect(toast).toBeTruthy();
      expect(toast!.kind).toBe("warning");
      expect(toast!.detail).toMatch(/undo record is incomplete/i);
    });
  });

  it("stays silent about the journal when it is complete", async () => {
    await organizeWith({ journal_incomplete: false });

    expect(
      await screen.findByRole("button", { name: /undo this organize/i }),
    ).toBeInTheDocument();
    expect(screen.queryByText(/undo record is incomplete/i)).not.toBeInTheDocument();

    const toast = useNotificationStore
      .getState()
      .items.find((n) => n.message === "Moved 1 of 1 files");
    expect(toast!.kind).toBe("success");
  });

  it("keeps the warning visible after the undo has run", async () => {
    const user = await organizeWith({ journal_incomplete: true });
    await user.click(await screen.findByRole("button", { name: /undo this organize/i }));

    // Undo reported a clean revert of what the journal DID contain; the warning
    // is what tells the user that isn't the whole run.
    await waitFor(() => {
      expect(screen.getByRole("button", { name: /undone/i })).toBeInTheDocument();
    });
    expect(screen.getByText(/undo record is incomplete/i)).toBeInTheDocument();
  });

  it("treats a sidecar that omits the flag as complete", async () => {
    await organizeWith({});

    expect(
      await screen.findByRole("button", { name: /undo this organize/i }),
    ).toBeInTheDocument();
    expect(screen.queryByText(/undo record is incomplete/i)).not.toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// A CANCELLED undo must not resolve like a success.
//
// `revert_journal` no longer rejects on cancellation: it returns the partial
// summary with `cancelled: true`. Its `skipped`/`errors` are 0 simply because
// the remaining entries were never attempted, so the "clean revert" test above
// would latch "Undone" and grey out the retry with most files still sitting in
// their genre folders — and toast it as a green success.
// ---------------------------------------------------------------------------

const PLAN_2: OrganizePlan = {
  base_dir: "D:/Music",
  moves: [
    {
      source: "D:/Music/a.mp3",
      destination: "D:/Music/House/a.mp3",
      genre: "House",
      subgenre: "",
      reason: "ml_genre",
      relative_destination: "House/a.mp3",
      original_source: "D:/Music/a.mp3",
    },
    {
      source: "D:/Music/b.mp3",
      destination: "D:/Music/House/b.mp3",
      genre: "House",
      subgenre: "",
      reason: "ml_genre",
      relative_destination: "House/b.mp3",
      original_source: "D:/Music/b.mp3",
    },
  ],
  small_genres: [],
  genre_counts: { House: 2 },
  existing_genre_counts: { House: 0 },
  errors: [],
};

describe("<OrganizeView /> — cancelled undo", () => {
  beforeEach(() => {
    useLibraryStore.setState({
      libraryPath: "D:/Music",
      tracks: [track("D:/Music/a.mp3", "House"), track("D:/Music/b.mp3", "House")],
    });
    useOperationStore.setState({ organizePlan: null, organizePlanKey: null });
    (save as MockFn).mockResolvedValue("D:/backup.json");
  });

  /** Organize 2 files, then click Undo once per entry of `reverts`. */
  async function organizeThenUndo(reverts: unknown[]) {
    const user = userEvent.setup();
    let call = 0;
    (invoke as MockFn).mockImplementation(
      async (_cmd: string, args: { method: string }) => {
        switch (args.method) {
          case "plan_organization":
            return PLAN_2;
          case "backup_tags":
            return { total: 2, backed_up: 2, not_fully_backed_up: 0, errors: [] };
          case "organize":
            return {
              planned: 2,
              moved: 2,
              errors: [],
              journal_path: "D:/journals/organize-1.jsonl",
              moved_pairs: [
                ["D:/Music/a.mp3", "D:/Music/House/a.mp3"],
                ["D:/Music/b.mp3", "D:/Music/House/b.mp3"],
              ],
            };
          case "revert_journal":
            return reverts[Math.min(call++, reverts.length - 1)];
          default:
            return {};
        }
      },
    );
    render(<OrganizeView />);
    await user.click(await screen.findByRole("button", { name: /preview plan/i }));
    await user.click(await screen.findByRole("button", { name: /execute \(2 moves\)/i }));
    await user.click(await screen.findByRole("button", { name: /yes, move files/i }));
    for (let i = 0; i < reverts.length; i++) {
      await user.click(
        await screen.findByRole("button", { name: /undo this organize|retry undo/i }),
      );
    }
    return user;
  }

  const CANCELLED_HALFWAY = {
    reverted: 1,
    skipped: 0,
    errors: 0,
    trashed_not_reverted: 0,
    error_messages: [] as string[],
    reverted_pairs: [["D:/Music/House/b.mp3", "D:/Music/b.mp3"]] as [string, string][],
    cancelled: true,
  };

  it("does not latch Undone — the rest of the files are still moved", async () => {
    await organizeThenUndo([CANCELLED_HALFWAY]);

    await waitFor(() => {
      const msgs = useNotificationStore.getState().items.map((n) => n.message);
      expect(msgs.some((m) => /undo cancelled/i.test(m))).toBe(true);
    });
    expect(screen.queryByRole("button", { name: /^undone$/i })).not.toBeInTheDocument();
    expect(
      await screen.findByRole("button", { name: /undo this organize/i }),
    ).toBeEnabled();
  });

  it("says what is still out of place instead of 'Undo complete'", async () => {
    await organizeThenUndo([CANCELLED_HALFWAY]);

    await waitFor(() => {
      const toast = useNotificationStore
        .getState()
        .items.find((n) => /undo cancelled/i.test(n.message));
      expect(toast).toBeTruthy();
      expect(toast!.message).toMatch(/restored 1 of 2 before stopping/i);
      expect(toast!.kind).not.toBe("success");
      expect(toast!.detail).toMatch(/still in their organized folders/i);
    });
    const msgs = useNotificationStore.getState().items.map((n) => n.message);
    expect(msgs.some((m) => /undo complete/i.test(m))).toBe(false);
  });

  it("still re-paths the library rows it did restore", async () => {
    await organizeThenUndo([CANCELLED_HALFWAY]);

    await waitFor(() => {
      const paths = useLibraryStore.getState().tracks.map((t) => t.path).sort();
      // a.mp3 never got undone, b.mp3 did.
      expect(paths).toEqual(["D:/Music/House/a.mp3", "D:/Music/b.mp3"]);
    });
  });

  // The retry's own result must replace the previous attempt's panel.
  it("clears the stale 'left N files in place' panel after a clean retry", async () => {
    await organizeThenUndo([
      {
        reverted: 0,
        skipped: 2,
        errors: 0,
        trashed_not_reverted: 0,
        error_messages: [],
        reverted_pairs: [],
      },
      {
        reverted: 2,
        skipped: 0,
        errors: 0,
        trashed_not_reverted: 0,
        error_messages: [],
        reverted_pairs: [
          ["D:/Music/House/a.mp3", "D:/Music/a.mp3"],
          ["D:/Music/House/b.mp3", "D:/Music/b.mp3"],
        ],
      },
    ]);

    await waitFor(() => {
      expect(screen.getByRole("button", { name: /^undone$/i })).toBeDisabled();
    });
    expect(screen.queryByText(/in place/i)).not.toBeInTheDocument();
  });
});
