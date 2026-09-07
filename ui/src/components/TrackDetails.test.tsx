/**
 * F028 — the track panel's genre line sits directly above "Apply ML tags to
 * this file", so it is a promise about an irreversible write. It used to model
 * a single threshold (family confidence vs. the strict SUBgenre gate, plus a
 * hard requirement for a subgenre), and so printed "won't be written" for every
 * track the tagger's parent-genre tier actually rewrites.
 */

import { describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { invoke } from "@tauri-apps/api/core";

import { TrackDetails } from "./TrackDetails";
import { useConfigStore, useLibraryStore, useNotificationStore, useUIStore } from "../stores";
import type { MLResult, TrackAnalysis } from "../types";

function track(ml: Partial<MLResult>): TrackAnalysis {
  return {
    path: "D:/Music/a.mp3",
    filename: "a.mp3",
    extension: ".mp3",
    size_mb: 5,
    existing_tags: { genre: "Tech House" },
    error: null,
    ml_analysis: {
      ml_genre: null,
      ml_subgenre: null,
      ml_genre_confidence: null,
      ml_genre_raw_confidence: null,
      ml_genre_source: "ml",
      ...ml,
    },
  } as unknown as TrackAnalysis;
}

function show(t: TrackAnalysis) {
  useLibraryStore.setState({ tracks: [t] });
  useUIStore.setState({ selectedTrackPath: t.path });
  render(<TrackDetails />);
}

describe("<TrackDetails /> — genre write promise", () => {
  it("says the PARENT genre will be written for a parent-only track", () => {
    // Family 0.72 / raw 0.40 — the tagger takes the parent-only branch here.
    show(
      track({
        ml_genre: "House",
        ml_subgenre: "Deep House",
        ml_genre_confidence: 0.72,
        ml_genre_raw_confidence: 0.4,
      }),
    );

    expect(screen.getByText(/will replace the existing genre tag/i)).toBeInTheDocument();
    expect(screen.queryByText(/won't be written/i)).not.toBeInTheDocument();
  });

  it("still says nothing will be written below both thresholds", () => {
    show(
      track({
        ml_genre: "House",
        ml_subgenre: "Deep House",
        ml_genre_confidence: 0.3,
        ml_genre_raw_confidence: 0.2,
      }),
    );

    expect(screen.getByText(/won't be written/i)).toBeInTheDocument();
  });

  it("names the subgenre that lands when the strict gate clears", () => {
    show(
      track({
        ml_genre: "House",
        ml_subgenre: "Deep House",
        ml_genre_confidence: 0.95,
        ml_genre_raw_confidence: 0.9,
      }),
    );

    expect(
      screen.getByText(/"Deep House" will be written to the genre tag/i),
    ).toBeInTheDocument();
  });

  it("reports the genre toggle, not the threshold, when write_genre is off", () => {
    useConfigStore.getState().updateTagging({ write_genre: false });
    try {
      show(
        track({
          ml_genre: "House",
          ml_subgenre: "Deep House",
          ml_genre_confidence: 0.95,
          ml_genre_raw_confidence: 0.9,
        }),
      );
      expect(screen.getByText(/genre writing is off in settings/i)).toBeInTheDocument();
    } finally {
      useConfigStore.getState().updateTagging({ write_genre: true });
    }
  });
});

/**
 * The post-apply toast. `genre_applied_parent_only` is a genre WRITE (the
 * parent family, no subgenre) and roughly half a typical library lands there,
 * so counting it as "nothing written" told users their file was untouched right
 * after Vibechek rewrote its TCON frame.
 */
describe("<TrackDetails /> — apply result toast", () => {
  function applyReturning(stats: Record<string, number | string[]>) {
    (invoke as ReturnType<typeof vi.fn>).mockImplementation(
      async (_cmd: string, args: { method: string }) => {
        if (args.method === "apply_ml_tags") {
          return {
            total: 1,
            genre_applied: 0,
            genre_applied_parent_only: 0,
            genre_skipped_low_confidence: 0,
            genre_skipped_write_disabled: 0,
            other_tags_applied: 0,
            errors: [],
            ...stats,
          };
        }
        return {};
      },
    );
  }

  it("reports a parent-only write as a write, not a skip", async () => {
    applyReturning({ genre_applied_parent_only: 1 });
    show(track({ ml_genre: "House", ml_genre_confidence: 0.72 }));

    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: /apply ml tags to this file/i }));

    await waitFor(() => {
      const items = useNotificationStore.getState().items;
      expect(items.length).toBe(1);
      expect(items[0].message).toMatch(/Tags applied to a\.mp3/);
      expect(items[0].detail).toMatch(/parent family/i);
    });
    expect(
      useNotificationStore.getState().items.map((n) => n.message),
    ).not.toContain("Genre skipped — confidence below threshold");
  });

  it("still reports a genuine low-confidence skip", async () => {
    applyReturning({ genre_skipped_low_confidence: 1 });
    show(track({ ml_genre: "House", ml_genre_confidence: 0.2 }));

    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: /apply ml tags to this file/i }));

    await waitFor(() => {
      const items = useNotificationStore.getState().items;
      expect(items.length).toBe(1);
      expect(items[0].message).toBe("Genre skipped — confidence below threshold");
    });
  });
});
