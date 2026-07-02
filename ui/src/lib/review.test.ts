import { describe, expect, it } from "vitest";

import type { ExistingTags, MLResult } from "../types";
import {
  genreProvenance,
  hasInterestingProvenance,
  keyProvenance,
  needsReview,
  reviewReason,
  reviewSeverity,
} from "./review";

// Minimal MLResult factory — only the fields the review helpers read. Casting a
// Partial keeps the test focused (the helpers only touch genre/vocal provenance).
function ml(overrides: Partial<MLResult>): MLResult {
  return {
    ml_genre: null,
    ml_subgenre: null,
    ml_genre_confidence: null,
    ml_genre_audio: null,
    ml_subgenre_audio: null,
    ml_genre_web: null,
    ml_genre_web_grounded: null,
    ml_genre_source: null,
    ml_genre_conflict: null,
    ml_key: null,
    ml_key_tag: null,
    ml_key_conflict: null,
    ...overrides,
  } as MLResult;
}

const tags = (genre: string | null): ExistingTags => ({ genre });

describe("reviewSeverity / needsReview", () => {
  it("returns null when not reconciled", () => {
    expect(reviewSeverity(ml({}))).toBeNull();
    expect(needsReview(ml({}))).toBe(false);
    expect(needsReview(null)).toBe(false);
  });

  it("returns null when sources agree (conflict false)", () => {
    const m = ml({ ml_genre_source: "tag", ml_genre_conflict: false });
    expect(reviewSeverity(m)).toBeNull();
    expect(needsReview(m)).toBe(false);
  });

  it("flags a kept tag that the audio model disagreed with as 'conflict'", () => {
    const m = ml({ ml_genre_source: "tag", ml_genre_conflict: true });
    expect(reviewSeverity(m)).toBe("conflict");
    expect(needsReview(m)).toBe(true);
  });

  it("flags an ML override as 'override'", () => {
    const m = ml({ ml_genre_source: "ml_override", ml_genre_conflict: true });
    expect(reviewSeverity(m)).toBe("override");
    expect(needsReview(m)).toBe(true);
  });

  it("flags a web override as 'override'", () => {
    const m = ml({ ml_genre_source: "web_override", ml_genre_conflict: true });
    expect(reviewSeverity(m)).toBe("override");
  });

  it("treats a prefer_ml win (source='ml', no _override suffix) as 'override' — the tag was discarded", () => {
    // Under prefer_ml, reconcile returns source='ml' with conflict=true and the
    // tag is gone. Keying off the '_override' suffix would wrongly call this a
    // calm 'conflict'; key off "tag didn't win" instead.
    const m = ml({ ml_genre_source: "ml", ml_genre_conflict: true });
    expect(reviewSeverity(m)).toBe("override");
  });

  it("treats a prefer_ml WEB win (source='web') as 'override'", () => {
    const m = ml({ ml_genre_source: "web", ml_genre_conflict: true });
    expect(reviewSeverity(m)).toBe("override");
  });

  it("never flags a pure-ML read with no tag", () => {
    // No tag to disagree with → reconcile sets conflict false → not reviewed.
    const m = ml({ ml_genre_source: "ml", ml_genre_conflict: false });
    expect(needsReview(m)).toBe(false);
  });
});

describe("reviewReason", () => {
  it("explains an ML override as a change to the user's tag", () => {
    const m = ml({
      ml_genre_source: "ml_override",
      ml_genre_conflict: true,
      ml_genre: "Trance",
      ml_subgenre: "Trance",
      ml_genre_audio: "Trance",
    });
    const reason = reviewReason(m, tags("Tech House"));
    expect(reason).toContain("Tech House");
    expect(reason).toContain("Trance");
    expect(reason).toContain("audio model");
  });

  it("attributes a web override to an online source", () => {
    const m = ml({
      ml_genre_source: "web_override",
      ml_genre_conflict: true,
      ml_genre: "Pop",
      ml_subgenre: "Pop",
      ml_genre_web: "Pop",
    });
    expect(reviewReason(m, tags("Dance/Pop"))).toContain("online source");
  });

  it("says the tag was KEPT when it wins despite a conflict", () => {
    const m = ml({
      ml_genre_source: "tag",
      ml_genre_conflict: true,
      ml_genre: "House",
      ml_subgenre: "Tech House",
      ml_genre_audio: "Trance",
    });
    const reason = reviewReason(m, tags("Tech House"))!;
    expect(reason).toMatch(/Kept your tag/i);
    expect(reason).toContain("Trance"); // the disagreeing audio read
  });

  it("says CHANGED (not Kept) when a prefer_ml audio read discarded the tag", () => {
    const m = ml({
      ml_genre_source: "ml",
      ml_genre_conflict: true,
      ml_genre: "Techno",
      ml_subgenre: "Techno",
      ml_genre_audio: "Techno",
    });
    const reason = reviewReason(m, tags("Tech House"))!;
    expect(reason).not.toMatch(/Kept/i);
    expect(reason).toMatch(/Changed your tag/i);
    expect(reason).toContain("audio model");
  });

  it("attributes a prefer_ml WEB win to an online source", () => {
    const m = ml({
      ml_genre_source: "web",
      ml_genre_conflict: true,
      ml_genre: "Pop",
      ml_subgenre: "Pop",
      ml_genre_web: "Pop",
    });
    const reason = reviewReason(m, tags("Dance/Pop"))!;
    expect(reason).not.toMatch(/Kept/i);
    expect(reason).toContain("online source");
  });

  it("returns null when there's nothing to review", () => {
    expect(reviewReason(ml({ ml_genre_source: "tag", ml_genre_conflict: false }), tags("House"))).toBeNull();
  });

  it("handles an override with no prior tag (defensive)", () => {
    const m = ml({
      ml_genre_source: "ml_override",
      ml_genre_conflict: true,
      ml_genre: "Techno",
      ml_subgenre: "Techno",
    });
    expect(reviewReason(m, tags(null))).toBe("Set to “Techno” by the audio model.");
  });

  it("falls back to a generic message when a conflict has no comparand (defensive)", () => {
    // source=tag + conflict but neither web nor audio populated — unreachable via
    // the real pipeline, but the pure helper must not produce a broken sentence.
    const m = ml({
      ml_genre_source: "tag",
      ml_genre_conflict: true,
      ml_genre: "House",
      ml_subgenre: "Tech House",
    });
    expect(reviewReason(m, tags("Tech House"))).toBe("Sources disagree on the genre.");
  });
});

describe("keyProvenance", () => {
  it("returns null when there's no ml result", () => {
    expect(keyProvenance(null)).toBeNull();
    expect(keyProvenance(undefined)).toBeNull();
  });

  it("returns null when the track has no parseable key tag", () => {
    // ml_key_tag is OMITTED (undefined) on the wire when unparseable — the
    // helper keys off truthiness, never `=== null`.
    expect(keyProvenance(ml({ ml_key: "9B" }))).toBeNull();
    expect(keyProvenance(ml({ ml_key: "9B", ml_key_tag: null }))).toBeNull();
  });

  it("reports agreement when the tag matches the audio read", () => {
    const p = keyProvenance(
      ml({ ml_key: "9B", ml_key_tag: "9B", ml_key_conflict: false }),
    );
    expect(p).toEqual({ tag: "9B", audio: "9B", conflict: false });
  });

  it("flags a disagreeing tag but keeps it OUT of the genre review queue", () => {
    const m = ml({ ml_key: "9B", ml_key_tag: "4A", ml_key_conflict: true });
    expect(keyProvenance(m)).toEqual({ tag: "4A", audio: "9B", conflict: true });
    // Read-only surfacing by design — a key conflict must NOT flag the track
    // for review (needsReview stays genre-only; there's no approve/revert).
    expect(needsReview(m)).toBe(false);
  });
});

describe("genreProvenance / hasInterestingProvenance", () => {
  it("returns null for an un-reconciled record", () => {
    expect(genreProvenance(ml({}), tags("House"))).toBeNull();
  });

  it("collects the three signals + the winner", () => {
    const p = genreProvenance(
      ml({
        ml_genre_source: "web_override",
        ml_genre_conflict: true,
        ml_genre: "Pop",
        ml_subgenre: "Pop",
        ml_genre_audio: "House",
        ml_subgenre_audio: "Deep House",
        ml_genre_web: "Pop",
        ml_genre_web_grounded: true,
        ml_genre_confidence: 0.9,
      }),
      tags("Dance/Pop"),
    )!;
    expect(p.tag).toBe("Dance/Pop");
    expect(p.audio).toBe("Deep House"); // subgenre preferred
    expect(p.web).toBe("Pop");
    expect(p.webGrounded).toBe(true);
    expect(p.effective).toBe("Pop");
    expect(p.source).toBe("web_override");
    expect(p.severity).toBe("override");
  });

  it("hides the boring case (tag kept, audio agreed, no web)", () => {
    const p = genreProvenance(
      ml({
        ml_genre_source: "tag",
        ml_genre_conflict: false,
        ml_genre: "House",
        ml_subgenre: "Tech House",
        ml_genre_audio: "House",
        ml_subgenre_audio: "Tech House",
      }),
      tags("Tech House"),
    );
    expect(hasInterestingProvenance(p)).toBe(false);
  });

  it("shows when the audio read differs from the surfaced value even without a conflict flag", () => {
    const p = genreProvenance(
      ml({
        ml_genre_source: "tag",
        ml_genre_conflict: false,
        ml_genre: "House",
        ml_subgenre: "Tech House",
        ml_genre_audio: "House",
        ml_subgenre_audio: "Bass House",
      }),
      tags("Tech House"),
    );
    expect(hasInterestingProvenance(p)).toBe(true);
  });

  it("treats a case-only difference between audio and effective as NOT interesting", () => {
    const p = genreProvenance(
      ml({
        ml_genre_source: "tag",
        ml_genre_conflict: false,
        ml_genre: "House",
        ml_subgenre: "Tech House",
        ml_genre_audio: "house",
        ml_subgenre_audio: "tech house",
      }),
      tags("Tech House"),
    );
    expect(hasInterestingProvenance(p)).toBe(false);
  });

  it("keeps a user-approved track interesting even though its conflict cleared", () => {
    // After resolve_genre_conflicts("approve"): source='approved', conflict=false.
    // severity is null, audio matches effective (no other signal) — but we still
    // want to show the breakdown + the "you approved this" confirmation.
    const p = genreProvenance(
      ml({
        ml_genre_source: "approved",
        ml_genre_conflict: false,
        ml_genre: "Techno",
        ml_subgenre: "Techno",
        ml_genre_audio: "Techno",
        ml_subgenre_audio: "Techno",
      }),
      tags("Tech House"),
    );
    expect(p!.source).toBe("approved");
    expect(reviewSeverity(ml({ ml_genre_source: "approved", ml_genre_conflict: false }))).toBeNull();
    expect(needsReview(ml({ ml_genre_source: "approved", ml_genre_conflict: false }))).toBe(false);
    expect(hasInterestingProvenance(p)).toBe(true);
  });
});
