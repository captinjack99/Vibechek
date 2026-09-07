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

describe("useLibraryStore.mergeAnalyzedTracks — batch merge", () => {
  beforeEach(() => {
    useLibraryStore.setState({
      libraryPath: "D:/Music",
      tracks: [track("D:/Music/a.mp3"), track("D:/Music/b.mp3"), track("D:/Music/c.mp3")],
      selectedIds: new Set(["D:/Music/a.mp3"]),
      searchFilter: "",
    });
  });

  it("replaces matching records in place, preserving order", () => {
    const updatedA = { ...track("D:/Music/a.mp3"), filename_title: "A!" } as TrackAnalysis;
    const updatedC = { ...track("D:/Music/c.mp3"), filename_title: "C!" } as TrackAnalysis;

    useLibraryStore.getState().mergeAnalyzedTracks([updatedA, updatedC]);

    const { tracks } = useLibraryStore.getState();
    expect(tracks.map((t) => t.path)).toEqual([
      "D:/Music/a.mp3",
      "D:/Music/b.mp3",
      "D:/Music/c.mp3",
    ]);
    expect((tracks[0] as TrackAnalysis & { filename_title?: string }).filename_title).toBe("A!");
    expect((tracks[2] as TrackAnalysis & { filename_title?: string }).filename_title).toBe("C!");
    // Untouched record stays as-is.
    expect((tracks[1] as TrackAnalysis & { filename_title?: string }).filename_title).toBeUndefined();
  });

  it("appends records whose path isn't already present", () => {
    const novel = track("D:/Music/d.mp3");
    useLibraryStore.getState().mergeAnalyzedTracks([novel]);
    const { tracks } = useLibraryStore.getState();
    expect(tracks.map((t) => t.path)).toContain("D:/Music/d.mp3");
    expect(tracks).toHaveLength(4);
  });

  it("leaves the selection untouched and is a no-op for an empty list", () => {
    const before = useLibraryStore.getState().tracks;
    useLibraryStore.getState().mergeAnalyzedTracks([]);
    const after = useLibraryStore.getState();
    expect(after.tracks).toBe(before); // identical reference — no state write
    expect([...after.selectedIds]).toEqual(["D:/Music/a.mp3"]);
  });
});

/**
 * Regression test for the audit finding "Trashed/moved duplicates are never
 * removed from the in-memory library" (MEDIUM, frontend).
 *
 * After DuplicatesView trashes files the library table kept listing them:
 * Play loaded a nonexistent file, Apply-tags failed per file, and an organize
 * preview filled with "File not found" — for files Vibechek itself deleted.
 * `removeTracks` is the store half of that fix.
 */
describe("useLibraryStore.removeTracks", () => {
  beforeEach(() => {
    useLibraryStore.setState({
      libraryPath: "D:/Music",
      tracks: [track("D:/Music/a.mp3"), track("D:/Music/b.mp3"), track("D:/Music/c.mp3")],
      selectedIds: new Set(["D:/Music/a.mp3", "D:/Music/c.mp3"]),
      searchFilter: "",
    });
    usePlayerStore.setState({ path: null, title: null, playToken: 0 });
    useUIStore.setState({ selectedTrackPath: null });
  });

  it("drops the removed tracks and leaves the rest untouched", () => {
    useLibraryStore.getState().removeTracks(["D:/Music/a.mp3", "D:/Music/c.mp3"]);
    expect(useLibraryStore.getState().tracks.map((t) => t.path)).toEqual(["D:/Music/b.mp3"]);
  });

  it("prunes the selection so bulk actions can't target deleted files", () => {
    useLibraryStore.getState().removeTracks(["D:/Music/a.mp3"]);
    expect([...useLibraryStore.getState().selectedIds]).toEqual(["D:/Music/c.mp3"]);
  });

  it("closes the inspector and the player when their track is the one removed", () => {
    useUIStore.setState({ selectedTrackPath: "D:/Music/a.mp3" });
    usePlayerStore.setState({ path: "D:/Music/a.mp3", title: "a.mp3", playToken: 1 });

    useLibraryStore.getState().removeTracks(["D:/Music/a.mp3"]);

    expect(useUIStore.getState().selectedTrackPath).toBeNull();
    expect(usePlayerStore.getState().path).toBeNull();
  });

  it("leaves an untouched inspector/player alone", () => {
    useUIStore.setState({ selectedTrackPath: "D:/Music/b.mp3" });
    usePlayerStore.setState({ path: "D:/Music/b.mp3", title: "b.mp3", playToken: 1 });

    useLibraryStore.getState().removeTracks(["D:/Music/a.mp3"]);

    expect(useUIStore.getState().selectedTrackPath).toBe("D:/Music/b.mp3");
    expect(usePlayerStore.getState().path).toBe("D:/Music/b.mp3");
  });

  it("is a no-op for an empty list or for paths that aren't in the library", () => {
    const before = useLibraryStore.getState().tracks;
    useLibraryStore.getState().removeTracks([]);
    expect(useLibraryStore.getState().tracks).toBe(before);
    useLibraryStore.getState().removeTracks(["D:/Elsewhere/z.mp3"]);
    expect(useLibraryStore.getState().tracks).toBe(before);
  });
});

// ---------------------------------------------------------------------------
// `filename` is a STORED field, not derived at render time, and the destination
// basename is not always the source's: both the organizer and the dedupe
// move go through `_unique_path`, which renames on collision ("a.mp3" ->
// "a (2).mp3"). A re-path that moves only `path` leaves the library table
// showing a name no file has any more, and every basename-keyed lookup
// (count_new_tracks matches on basenames) desyncs.
// ---------------------------------------------------------------------------

describe("useLibraryStore.updateTrackPaths — filename follows the path", () => {
  beforeEach(() => {
    useLibraryStore.setState({
      libraryPath: "D:/Music",
      tracks: [track("D:/Music/a.mp3")],
      selectedIds: new Set(),
      searchFilter: "",
    });
  });

  it("re-derives the filename when the move renamed the file", () => {
    useLibraryStore.getState().updateTrackPaths({
      "D:/Music/a.mp3": "D:/Music/Review/a (2).mp3",
    });

    const [t] = useLibraryStore.getState().tracks;
    expect(t.path).toBe("D:/Music/Review/a (2).mp3");
    expect(t.filename).toBe("a (2).mp3");
  });

  it("handles a Windows-separator destination too", () => {
    useLibraryStore.getState().updateTrackPaths({
      "D:/Music/a.mp3": "D:\\Music\\House\\a.mp3",
    });

    expect(useLibraryStore.getState().tracks[0].filename).toBe("a.mp3");
  });
});
