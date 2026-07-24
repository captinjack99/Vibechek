import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";

import type { TrackAnalysis } from "../types";
import {
  FilterChips,
  applyFilters,
  emptyFilters,
  isEmpty,
  type LibraryFilters,
} from "./LibraryFilters";

// Build a minimal TrackAnalysis. ML fields are the only thing the filter
// chips/applyFilters look at, so the rest is filler.
function track(
  filename: string,
  ml: Partial<NonNullable<TrackAnalysis["ml_analysis"]>>,
): TrackAnalysis {
  return {
    path: `/library/${filename}`,
    filename,
    extension: ".mp3",
    size_mb: 5,
    existing_tags: {},
    ml_analysis: {
      ml_genre: null,
      ml_subgenre: null,
      ml_genre_confidence: null,
      ml_genre_raw_confidence: null,
      ml_bpm: null,
      ml_key: null,
      ml_energy: null,
      ml_mood: null,
      ml_timeslot: null,
      ml_direction: null,
      ml_vocal: null,
      ml_vocal_score: null,
      ml_vocal_peak: null,
      ml_vocal_frac: null,
      ml_danceability: null,
      ml_mood_scores: null,
      ml_error: null,
      ml_genre_audio: null,
      ml_subgenre_audio: null,
      ml_genre_web: null,
      ml_genre_web_grounded: null,
      ml_genre_source: null,
      ml_genre_conflict: null,
      ml_vocal_audio: null,
      ml_vocal_source: null,
      ml_key_tag: null,
      ml_key_conflict: null,
      ml_genre_classifier: null,
      ml_degraded_heads: null,
      ...ml,
    },
  };
}

describe("emptyFilters / isEmpty", () => {
  it("emptyFilters() returns an all-empty filter set", () => {
    const f = emptyFilters();
    expect(f.genres.size).toBe(0);
    expect(f.energies.size).toBe(0);
    expect(f.moods.size).toBe(0);
    expect(f.vocals.size).toBe(0);
  });

  it("isEmpty is true for a fresh emptyFilters()", () => {
    expect(isEmpty(emptyFilters())).toBe(true);
  });

  it("isEmpty is false once any criterion has a value", () => {
    const f: LibraryFilters = { ...emptyFilters(), genres: new Set(["House"]) };
    expect(isEmpty(f)).toBe(false);
  });
});

describe("applyFilters", () => {
  const tracks = [
    track("a.mp3", { ml_genre: "House", ml_energy: 3, ml_mood: "Bright" }),
    track("b.mp3", { ml_genre: "Techno", ml_energy: 5, ml_mood: "Dark" }),
    track("c.mp3", { ml_genre: "House", ml_energy: 5, ml_mood: "Dark" }),
  ];

  it("returns all tracks when no filter is set", () => {
    expect(applyFilters(tracks, emptyFilters())).toHaveLength(3);
  });

  it("narrows by a single genre filter", () => {
    const result = applyFilters(tracks, {
      ...emptyFilters(),
      genres: new Set(["House"]),
    });
    expect(result.map((t) => t.filename)).toEqual(["a.mp3", "c.mp3"]);
  });

  it("ANDs across multiple criteria", () => {
    const result = applyFilters(tracks, {
      ...emptyFilters(),
      genres: new Set(["House"]),
      moods: new Set(["Dark"]),
    });
    expect(result.map((t) => t.filename)).toEqual(["c.mp3"]);
  });

  it("filters by direction (Up / Steady / Down)", () => {
    const dirTracks = [
      track("up.mp3", { ml_direction: "Up" }),
      track("steady.mp3", { ml_direction: "Steady" }),
      track("down.mp3", { ml_direction: "Down" }),
    ];
    const result = applyFilters(dirTracks, {
      ...emptyFilters(),
      directions: new Set(["Up"]),
    });
    expect(result.map((t) => t.filename)).toEqual(["up.mp3"]);
  });

  it("excludes tracks without a direction value when direction filter is active", () => {
    const dirTracks = [
      track("up.mp3", { ml_direction: "Up" }),
      track("missing.mp3", { ml_direction: null }),
    ];
    const result = applyFilters(dirTracks, {
      ...emptyFilters(),
      directions: new Set(["Up"]),
    });
    expect(result.map((t) => t.filename)).toEqual(["up.mp3"]);
  });
});

describe("<FilterChips />", () => {
  it("renders only the Filter label when there are no tracks", () => {
    render(
      <FilterChips tracks={[]} filters={emptyFilters()} setFilters={vi.fn()} />,
    );
    expect(screen.getByText(/Filter:/i)).toBeInTheDocument();
    // No dropdown summaries should render when the universe of values is empty.
    expect(screen.queryByText(/Genre/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Mood/)).not.toBeInTheDocument();
  });

  it("shows the Genre chip when tracks contain at least one genre", () => {
    const tracks = [track("a.mp3", { ml_genre: "House" })];
    render(
      <FilterChips
        tracks={tracks}
        filters={emptyFilters()}
        setFilters={vi.fn()}
      />,
    );
    expect(screen.getByText(/Genre/i)).toBeInTheDocument();
  });
});
