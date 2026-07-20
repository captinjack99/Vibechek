/**
 * Regression tests for two GlobalAudioPlayer preview bugs:
 *
 *  1. (HIGH) "Rapid preview switch shows the previous track's abort error on the
 *     NEW track, and never recovers." Switching previews faster than a file
 *     decodes aborts the in-flight fetch; WaveSurfer re-emits that as an
 *     AbortError 'error'. Before the fix, the single error handler stamped that
 *     onto the new (good) track and the `ready` handler never cleared it, so the
 *     UI showed "The user aborted a request." with dead controls.
 *
 *  2. (MED) "Slow-to-decode (large) valid files show a false 'Timed out (15s)'
 *     error and lock the play button even after they finish loading." The 15s
 *     watchdog fired, then a late `ready` arrived but never cleared the error or
 *     disarmed the timer.
 *
 * Both are driven here by mocking WaveSurfer and emitting the exact event order.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, render, screen } from "@testing-library/react";

import { GlobalAudioPlayer } from "./GlobalAudioPlayer";
import { usePlayerStore } from "../stores";

// ---------------------------------------------------------------------------
// WaveSurfer mock. One shared "instance" per test captures registered event
// handlers so the test can emit them in a precise order. getDuration is backed
// by a mutable variable so we can simulate a not-yet-decoded (0) vs decoded
// (>0) track at watchdog time.
// ---------------------------------------------------------------------------

type Handler = (...args: unknown[]) => void;

const handlers: Record<string, Handler[]> = {};
let mockDuration = 0;

function emit(event: string, ...args: unknown[]) {
  for (const h of handlers[event] ?? []) h(...args);
}

const mockInstance = {
  on: vi.fn((event: string, cb: Handler) => {
    (handlers[event] ??= []).push(cb);
  }),
  getDuration: vi.fn(() => mockDuration),
  seekTo: vi.fn(),
  play: vi.fn(),
  stop: vi.fn(),
  load: vi.fn(async () => {}),
  destroy: vi.fn(),
  playPause: vi.fn(),
};

vi.mock("wavesurfer.js", () => ({
  default: { create: vi.fn(() => mockInstance) },
}));

beforeEach(() => {
  for (const k of Object.keys(handlers)) delete handlers[k];
  mockDuration = 0;
  vi.clearAllMocks();
  usePlayerStore.setState({ path: null, title: null, playToken: 0 });
});

afterEach(() => {
  vi.useRealTimers();
});

describe("<GlobalAudioPlayer /> — rapid preview switch (AbortError)", () => {
  it("ignores the previous track's abort error and recovers on the new track's ready", () => {
    render(<GlobalAudioPlayer />);

    // Preview track one, then immediately track two (the rapid switch).
    act(() => usePlayerStore.getState().play("one.mp3", "One"));
    act(() => usePlayerStore.getState().play("two.mp3", "Two"));

    // The aborted first load rejects: WaveSurfer re-emits it as an AbortError.
    const abortErr = Object.assign(new Error("The user aborted a request."), {
      name: "AbortError",
    });
    act(() => emit("error", abortErr));

    // The abort must NOT have surfaced as a user-facing error.
    expect(screen.queryByText(/aborted a request/i)).not.toBeInTheDocument();

    // Track two then decodes fine.
    mockDuration = 180;
    act(() => emit("ready"));

    // No error shown, and the play/pause control is enabled (not disabled).
    expect(screen.queryByText(/aborted a request/i)).not.toBeInTheDocument();
    const playBtn = screen.getByRole("button", { name: /^(Play|Pause)$/ });
    expect(playBtn).not.toBeDisabled();
  });

  it("still surfaces a genuine (non-abort) load error", () => {
    render(<GlobalAudioPlayer />);
    act(() => usePlayerStore.getState().play("bad.mp3", "Bad"));

    act(() => emit("error", new Error("Unsupported codec")));

    expect(screen.getByText(/unsupported codec/i)).toBeInTheDocument();
  });
});

describe("<GlobalAudioPlayer /> — slow decode watchdog", () => {
  it("clears the timed-out error when a late ready arrives", () => {
    vi.useFakeTimers();
    render(<GlobalAudioPlayer />);

    act(() => usePlayerStore.getState().play("huge.flac", "Huge"));

    // 15s pass with no ready and duration still 0 → watchdog fires.
    act(() => vi.advanceTimersByTime(15_000));
    expect(screen.getByText(/timed out loading audio/i)).toBeInTheDocument();

    // The file finally decodes a second later.
    mockDuration = 600;
    act(() => emit("ready"));

    // The stale timeout error must be gone and controls re-enabled.
    expect(screen.queryByText(/timed out loading audio/i)).not.toBeInTheDocument();
    const playBtn = screen.getByRole("button", { name: /^(Play|Pause)$/ });
    expect(playBtn).not.toBeDisabled();
  });

  it("does not fire the watchdog once a track is already decoded", () => {
    vi.useFakeTimers();
    render(<GlobalAudioPlayer />);

    act(() => usePlayerStore.getState().play("normal.mp3", "Normal"));

    // Track decodes well before the 15s deadline.
    mockDuration = 200;
    act(() => emit("ready"));

    // Even after the watchdog deadline passes, no timeout error appears
    // (the timer was disarmed by the `ready` handler).
    act(() => vi.advanceTimersByTime(15_000));
    expect(screen.queryByText(/timed out loading audio/i)).not.toBeInTheDocument();
  });
});

describe("<GlobalAudioPlayer /> — failure fallback (WP-F)", () => {
  it("shows a plain headline + Show-in-folder, and demotes the raw decoder error", () => {
    render(<GlobalAudioPlayer />);
    act(() => usePlayerStore.getState().play("D:/Music/bad.mp3", "Bad"));

    act(() => emit("error", new Error("DEMUXER_ERROR_COULD_NOT_OPEN")));

    // Plain headline leads — not the raw decoder string.
    expect(screen.getByText("Couldn't play this track.")).toBeInTheDocument();
    // A next step: reveal the file in the OS file manager.
    expect(screen.getByText("Show in folder")).toBeInTheDocument();
    // The raw error is DEMOTED behind a Details toggle, never deleted.
    expect(screen.getByText("Details")).toBeInTheDocument();
    expect(screen.getByText(/DEMUXER_ERROR_COULD_NOT_OPEN/)).toBeInTheDocument();
  });
});
