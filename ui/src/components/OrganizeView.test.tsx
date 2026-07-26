/**
 * WP-E3: OrganizeView undo partial-failure surfacing.
 *
 * After an organize, the result panel offers "Undo this organize". The revert
 * summary carries per-file `error_messages`, but the panel only toasted counts.
 * This drives the full flow (preview → execute → undo) and asserts the durable
 * in-view list of files that couldn't be moved back.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { invoke } from "@tauri-apps/api/core";
import { save } from "@tauri-apps/plugin-dialog";

import { OrganizeView } from "./OrganizeView";
import { useLibraryStore, useNotificationStore } from "../stores";
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
