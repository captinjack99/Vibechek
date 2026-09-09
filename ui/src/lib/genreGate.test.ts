/**
 * The frontend mirror of the tagger's two-stage genre gate.
 *
 * These cases are lifted from `vibechek/tagger.py::apply_ml_tags` and
 * `tests/test_tagger.py` — if one of them changes there, this file must change
 * too (tagger.py isn't in the TS codegen's MODULES, so nothing else catches it).
 */

import { describe, expect, it } from "vitest";

import { decideGenre, type GenreGateConfig } from "./genreGate";
import type { MLResult } from "../types";

const CFG: GenreGateConfig = {
  genre_confidence_threshold: 0.85,
  parent_genre_confidence_threshold: 0.5,
  write_genre: true,
  write_subgenre_as_main_genre: true,
};

/** Build an MLResult from just the genre fields the gate reads. */
function genre(fields: {
  ml_genre?: string | null;
  ml_subgenre?: string | null;
  ml_genre_confidence?: number | null;
  ml_genre_raw_confidence?: number | null;
  ml_genre_source?: string | null;
}): MLResult {
  return {
    ml_genre: null,
    ml_subgenre: null,
    ml_genre_confidence: null,
    ml_genre_raw_confidence: null,
    ml_genre_source: "ml",
    ...fields,
  } as unknown as MLResult;
}

describe("decideGenre", () => {
  it("writes the subgenre when the RAW confidence clears the strict gate", () => {
    const d = decideGenre(
      genre({
        ml_genre: "House",
        ml_subgenre: "Deep House",
        ml_genre_confidence: 0.95,
        ml_genre_raw_confidence: 0.9,
      }),
      CFG,
    );
    expect(d.outcome).toBe("subgenre");
    expect(d.willWrite).toBe(true);
    expect(d.genreToWrite).toBe("Deep House");
  });

  it("writes the PARENT when only the family confidence clears its gate", () => {
    // The tier the old single-threshold preview reported as "will be skipped".
    // Verified against the real tagger: family 0.72 / raw 0.40 →
    // genre_applied_parent_only = 1, genre_skipped_low_confidence = 0.
    const d = decideGenre(
      genre({
        ml_genre: "House",
        ml_subgenre: "Deep House",
        ml_genre_confidence: 0.72,
        ml_genre_raw_confidence: 0.4,
      }),
      CFG,
    );
    expect(d.outcome).toBe("parent-only");
    expect(d.willWrite).toBe(true);
    expect(d.genreToWrite).toBe("House");
    expect(d.subgenreToWrite).toBe("");
  });

  it("judges stage 1 on the FAMILY confidence, not the raw one, in the family band", () => {
    // tests/test_tagger.py pins family 0.92 / raw 0.30 as a PARENT-only write.
    // The old UI counted it as "will write Deep House".
    const d = decideGenre(
      genre({
        ml_genre: "House",
        ml_subgenre: "Deep House",
        ml_genre_confidence: 0.92,
        ml_genre_raw_confidence: 0.3,
      }),
      CFG,
    );
    expect(d.outcome).toBe("parent-only");
    expect(d.genreToWrite).toBe("House");
  });

  it("holds stage 2 back on a legacy report with no raw confidence", () => {
    const d = decideGenre(
      genre({
        ml_genre: "House",
        ml_subgenre: "Deep House",
        ml_genre_confidence: 0.72,
        ml_genre_raw_confidence: null,
      }),
      CFG,
    );
    expect(d.outcome).toBe("low-confidence");
    expect(d.willWrite).toBe(false);
  });

  it("judges a tag-sourced genre on the family confidence (the raw one is the audio read)", () => {
    const d = decideGenre(
      genre({
        ml_genre: "House",
        ml_subgenre: "Deep House",
        ml_genre_confidence: 0.99,
        ml_genre_raw_confidence: 0.2,
        ml_genre_source: "tag",
      }),
      CFG,
    );
    expect(d.outcome).toBe("subgenre");
    expect(d.genreToWrite).toBe("Deep House");
  });

  it("writes nothing when write_genre is off, however confident", () => {
    const d = decideGenre(
      genre({
        ml_genre: "House",
        ml_subgenre: "Deep House",
        ml_genre_confidence: 0.99,
        ml_genre_raw_confidence: 0.95,
      }),
      { ...CFG, write_genre: false },
    );
    expect(d.outcome).toBe("write-disabled");
    expect(d.willWrite).toBe(false);
    expect(d.genreToWrite).toBe("");
  });

  it("puts the PARENT in the main genre frame when write_subgenre_as_main_genre is off", () => {
    const d = decideGenre(
      genre({
        ml_genre: "House",
        ml_subgenre: "Deep House",
        ml_genre_confidence: 0.95,
        ml_genre_raw_confidence: 0.9,
      }),
      { ...CFG, write_subgenre_as_main_genre: false },
    );
    expect(d.outcome).toBe("subgenre");
    expect(d.genreToWrite).toBe("House");
    expect(d.subgenreToWrite).toBe("Deep House");
  });

  it("takes the parent tier when there is no subgenre at all", () => {
    const d = decideGenre(
      genre({
        ml_genre: "House",
        ml_subgenre: null,
        ml_genre_confidence: 0.72,
        ml_genre_raw_confidence: 0.4,
      }),
      CFG,
    );
    expect(d.outcome).toBe("parent-only");
    expect(d.genreToWrite).toBe("House");
  });

  it("skips a track with no ML analysis", () => {
    expect(decideGenre(null, CFG).willWrite).toBe(false);
    expect(decideGenre(undefined, CFG).outcome).toBe("low-confidence");
  });

  it("skips when neither gate clears", () => {
    const d = decideGenre(
      genre({
        ml_genre: "House",
        ml_subgenre: "Deep House",
        ml_genre_confidence: 0.3,
        ml_genre_raw_confidence: 0.2,
      }),
      CFG,
    );
    expect(d.outcome).toBe("low-confidence");
    expect(d.willWrite).toBe(false);
  });
});
