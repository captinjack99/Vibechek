/**
 * Regression test for the audit finding "Global audio player keeps a stale path
 * after organize moves the loaded track; replay errors out" (LOW, frontend).
 *
 * `updateTrackPaths` already migrates `selectedIds` (library store) and
 * `selectedTrackPath` (UI store) after a move, but it did NOT migrate the
 * global player's `path`. So after an organize relocated the currently-loaded
 * track, hitting Play/Restart loaded the old (now-404) location and errored or
 * timed out. These tests lock in that the player path follows the move.
 */

import { beforeEach, describe, expect, it } from "vitest";

import { useLibraryStore, usePlayerStore, useUIStore } from "../stores";
import type { TrackAnalysis } from "../types";

function track(path: string): TrackAnalysis {
  // Only `path`/`filename` matter for these tests; the rest of TrackAnalysis is
  // optional in practice for updateTrackPaths, so a minimal cast is fine.
  const lastSep = Math.max(path.lastIndexOf("/"), path.lastIndexOf("\\"));
  const filename = lastSep >= 0 ? path.slice(lastSep + 1) : path;
  return { path, filename } as unknown as TrackAnalysis;
}

describe("useLibraryStore.updateTrackPaths — player path migration", () => {
  beforeEach(() => {
    useLibraryStore.setState({
      libraryPath: "D:/Music",
      tracks: [track("D:/Music/a.mp3"), track("D:/Music/b.mp3")],
      selectedIds: new Set(),
      searchFilter: "",
    });
    usePlayerStore.setState({ path: null, title: null, playToken: 0 });
    useUIStore.setState({ selectedTrackPath: null });
  });

  it("rewrites the loaded player path when that track is moved", () => {
    usePlayerStore.setState({ path: "D:/Music/a.mp3", title: "a.mp3", playToken: 1 });

    useLibraryStore.getState().updateTrackPaths({
      "D:/Music/a.mp3": "D:/Music/House/a.mp3",
    });

    const player = usePlayerStore.getState();
    expect(player.path).toBe("D:/Music/House/a.mp3");
    // Identity (title + playToken) must be preserved — only the path moves.
    expect(player.title).toBe("a.mp3");
    expect(player.playToken).toBe(1);
  });

  it("leaves the player path untouched when a different track is moved", () => {
    usePlayerStore.setState({ path: "D:/Music/b.mp3", title: "b.mp3", playToken: 2 });

    useLibraryStore.getState().updateTrackPaths({
      "D:/Music/a.mp3": "D:/Music/House/a.mp3",
    });

    expect(usePlayerStore.getState().path).toBe("D:/Music/b.mp3");
  });

  it("does nothing to the player when nothing is loaded", () => {
    useLibraryStore.getState().updateTrackPaths({
      "D:/Music/a.mp3": "D:/Music/House/a.mp3",
    });
    expect(usePlayerStore.getState().path).toBeNull();
  });
});
