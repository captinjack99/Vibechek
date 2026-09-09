/**
 * WP-E2: TagsView backup/restore completion honesty.
 *
 * The backend has always computed `not_fully_backed_up` (backup) and
 * `skipped_unsupported` (restore), plus a per-file `errors` list — but the
 * frontend hard-typed those away and the "Backup complete" panel implied zero
 * loss. These tests pin the honest counts + the expandable per-file list, and
 * assert a clean run stays quiet.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { invoke } from "@tauri-apps/api/core";
import { open, save } from "@tauri-apps/plugin-dialog";

import { TagsView } from "./TagsView";
import { useLibraryStore } from "../stores";

type MockFn = ReturnType<typeof vi.fn>;

/** Route invoke("rpc_call", {method}) to a per-method response map. */
function routeInvoke(map: Record<string, unknown>) {
  (invoke as MockFn).mockImplementation(
    async (_cmd: string, args: { method: string }) => {
      if (args.method in map) return map[args.method];
      return {};
    },
  );
}

describe("<TagsView /> — backup completion honesty", () => {
  beforeEach(() => {
    // No library seeded → refreshLibraryAfterRestore short-circuits to a notify.
    useLibraryStore.setState({ libraryPath: "D:/Music" });
    (save as MockFn).mockResolvedValue("D:/backup.json");
    (open as MockFn).mockResolvedValue(null);
  });

  it("warns about not-fully-backed-up files and lists read errors", async () => {
    const user = userEvent.setup();
    routeInvoke({
      backup_history: { records: [] },
      backup_tags: {
        total: 10,
        backed_up: 10,
        not_fully_backed_up: 2,
        errors: ["broken.wma: [Errno 13] read error"],
      },
    });

    render(<TagsView />);
    await user.click(await screen.findByRole("button", { name: /create backup/i }));

    // Still "complete", but honest about the gaps. (The "2" is inside a
    // <strong>, so match the surrounding text node, not the count.)
    expect(await screen.findByText(/backup complete/i)).toBeInTheDocument();
    expect(
      screen.getByText(/files could not be fully backed up/i),
    ).toBeInTheDocument();

    // The per-file read-error list is expandable and shows the real line.
    const toggle = screen.getByText(/couldn't be read — show details/i);
    await user.click(toggle);
    expect(screen.getByText(/broken\.wma: \[Errno 13\] read error/i)).toBeInTheDocument();
  });

  it("stays quiet on a fully-clean backup", async () => {
    const user = userEvent.setup();
    routeInvoke({
      backup_history: { records: [] },
      backup_tags: { total: 5, backed_up: 5, not_fully_backed_up: 0, errors: [] },
    });

    render(<TagsView />);
    await user.click(await screen.findByRole("button", { name: /create backup/i }));

    expect(await screen.findByText(/backup complete/i)).toBeInTheDocument();
    expect(screen.queryByText(/could not be fully backed up/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/show details/i)).not.toBeInTheDocument();
  });
});

describe("<TagsView /> — restore completion honesty", () => {
  beforeEach(() => {
    // libraryPath null → post-restore refresh just notifies, no scan_only call.
    useLibraryStore.setState({ libraryPath: null });
    (open as MockFn).mockResolvedValue("D:/backup.json");
    (save as MockFn).mockResolvedValue(null);
  });

  it("surfaces skipped + unsupported counts and the per-file write errors", async () => {
    const user = userEvent.setup();
    routeInvoke({
      backup_history: { records: [] },
      restore_tags: {
        total: 10,
        restored: 7,
        skipped_missing: 1,
        skipped_unsupported: 2,
        errors: ["z.flac: write failed — read-only file system"],
      },
    });

    render(<TagsView />);
    await user.click(await screen.findByRole("button", { name: /choose a backup file/i }));
    // Confirm the destructive restore.
    await user.click(await screen.findByRole("button", { name: /yes, restore/i }));

    // Counts sit inside <strong>; match the surrounding text nodes.
    expect(await screen.findByText(/of 10 files restored/i)).toBeInTheDocument();
    expect(screen.getByText(/skipped — no longer on disk/i)).toBeInTheDocument();
    expect(screen.getByText(/skipped — the backup held no tags/i)).toBeInTheDocument();

    const toggle = screen.getByText(/couldn't be written — show details/i);
    await user.click(toggle);
    expect(
      screen.getByText(/z\.flac: write failed — read-only file system/i),
    ).toBeInTheDocument();
  });

  it("shows a plain success result with no warnings on a clean restore", async () => {
    const user = userEvent.setup();
    routeInvoke({
      backup_history: { records: [] },
      restore_tags: {
        total: 8,
        restored: 8,
        skipped_missing: 0,
        skipped_unsupported: 0,
        errors: [],
      },
    });

    render(<TagsView />);
    await user.click(await screen.findByRole("button", { name: /choose a backup file/i }));
    await user.click(await screen.findByRole("button", { name: /yes, restore/i }));

    expect(await screen.findByText(/of 8 files restored/i)).toBeInTheDocument();
    expect(screen.queryByText(/skipped/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/show details/i)).not.toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// F003 — a restore that targets a DIFFERENT folder than the open library must
// adopt that folder as the library, not just push its tracks in. Leaving
// `libraryPath` behind pointed Organize's base_dir at the stale root while
// every source path lived under the new one.
// ---------------------------------------------------------------------------

describe("<TagsView /> — restore adopts the folder it restored into", () => {
  beforeEach(() => {
    useLibraryStore.setState({ libraryPath: "D:/Music", tracks: [] });
    (open as MockFn).mockResolvedValue("D:/backup.json");
    (save as MockFn).mockResolvedValue(null);
  });

  it("repoints libraryPath at the restored library, not just its tracks", async () => {
    const user = userEvent.setup();
    routeInvoke({
      // The backup belongs to a library on a DIFFERENT root than the open one.
      backup_history: {
        records: [
          {
            backup_path: "D:/backup.json",
            library_path: "E:/Music",
            file_count: 2,
            created_at: 0,
            size_bytes: 1,
            missing: false,
          },
        ],
      },
      restore_tags: {
        total: 2,
        restored: 2,
        skipped_missing: 0,
        skipped_unsupported: 0,
        errors: [],
      },
      scan_only: {
        tracks: [
          { path: "E:/Music/a.mp3", filename: "a.mp3", extension: ".mp3", size_mb: 1 },
          { path: "E:/Music/b.mp3", filename: "b.mp3", extension: ".mp3", size_mb: 1 },
        ],
      },
    });

    render(<TagsView />);
    await user.click(await screen.findByRole("button", { name: /choose a backup file/i }));
    await user.click(await screen.findByRole("button", { name: /yes, restore/i }));

    await waitFor(() => {
      expect(useLibraryStore.getState().tracks.map((t) => t.path)).toEqual([
        "E:/Music/a.mp3",
        "E:/Music/b.mp3",
      ]);
    });
    // The invariant: tracks and libraryPath name the SAME library.
    expect(useLibraryStore.getState().libraryPath).toBe("E:/Music");
  });
});
