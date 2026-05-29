import { describe, expect, it } from "vitest";

import type { FileInfo } from "../types";
import {
  DEFAULT_RULES,
  explainPick,
  pickKeeper,
  type KeeperRule,
} from "./keeperRules";

// Minimal FileInfo factory — only fill in what the rules look at.
function file(overrides: Partial<FileInfo>): FileInfo {
  return {
    path: "/library/track.mp3",
    filename: "track.mp3",
    size_bytes: 5_000_000,
    size_mb: 5,
    file_hash: null,
    audio_fingerprint: null,
    codec: "mp3",
    bitrate_kbps: 320,
    duration_s: 240,
    modified_time: 1_700_000_000,
    ...overrides,
  };
}

describe("pickKeeper", () => {
  it("prefers FLAC over MP3 by codec rule", () => {
    const mp3 = file({ path: "/a.mp3", codec: "mp3" });
    const flac = file({ path: "/a.flac", codec: "flac" });
    const winner = pickKeeper([mp3, flac], DEFAULT_RULES);
    expect(winner).toBe(flac);
  });

  it("falls back to size when codec ties", () => {
    // Both MP3 → codec rule is a tie. Bitrate also tied → size wins.
    const small = file({
      path: "/small.mp3",
      codec: "mp3",
      size_bytes: 1_000_000,
      size_mb: 1,
    });
    const big = file({
      path: "/big.mp3",
      codec: "mp3",
      size_bytes: 8_000_000,
      size_mb: 8,
    });
    // Disable bitrate so size is the decider after codec.
    const rules: KeeperRule[] = [
      { criterion: "codec", enabled: true },
      { criterion: "size", enabled: true },
    ];
    const winner = pickKeeper([small, big], rules);
    expect(winner).toBe(big);
  });

  it("falls back to mtime when codec + size tie", () => {
    const older = file({
      path: "/old.mp3",
      codec: "mp3",
      size_bytes: 5_000_000,
      modified_time: 1_600_000_000,
    });
    const newer = file({
      path: "/new.mp3",
      codec: "mp3",
      size_bytes: 5_000_000,
      modified_time: 1_700_000_000,
    });
    const rules: KeeperRule[] = [
      { criterion: "codec", enabled: true },
      { criterion: "size", enabled: true },
      { criterion: "modified", enabled: true },
    ];
    const winner = pickKeeper([older, newer], rules);
    expect(winner).toBe(newer);
  });

  it("returns the only file when given a single-element list", () => {
    const only = file({ path: "/only.mp3" });
    expect(pickKeeper([only], DEFAULT_RULES)).toBe(only);
  });

  it("throws on an empty list", () => {
    expect(() => pickKeeper([], DEFAULT_RULES)).toThrow();
  });
});

describe("explainPick", () => {
  it("attributes the win to the codec rule when codecs differ", () => {
    const mp3 = file({ path: "/a.mp3", codec: "mp3" });
    const flac = file({ path: "/a.flac", codec: "flac" });
    const reason = explainPick(flac, [mp3], DEFAULT_RULES);
    expect(reason.criterion).toBe("codec");
    expect(reason.detail).toMatch(/FLAC/i);
  });

  it("returns 'tie' when no rule distinguishes the candidates", () => {
    const a = file({ path: "/a.mp3" });
    const b = file({ path: "/b.mp3" });
    const reason = explainPick(a, [b], DEFAULT_RULES);
    expect(reason.criterion).toBe("tie");
  });
});

describe("DEFAULT_RULES", () => {
  it("has all five criteria enabled in priority order", () => {
    expect(DEFAULT_RULES).toHaveLength(5);
    expect(DEFAULT_RULES.every((r) => r.enabled)).toBe(true);
    expect(DEFAULT_RULES.map((r) => r.criterion)).toEqual([
      "codec",
      "bitrate",
      "size",
      "modified",
      "shortest_path",
    ]);
  });
});
