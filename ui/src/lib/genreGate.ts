/**
 * Frontend mirror of the tagger's two-stage genre gate.
 *
 * The bulk-tag confirm modal (LibraryBrowser) and the per-track diff panel
 * (TrackDetails) both have to tell the user what an *irreversible* tag write
 * will do BEFORE they click. Both used to model a single 0.85 threshold on
 * `ml_genre_confidence`, which is not the rule the backend runs: the parent
 * fallback (~47% of a typical library, per tagger.py) was reported as
 * "will be skipped" while the tagger was in fact rewriting those TCON frames.
 *
 * This module is the ONE place that rule lives on the frontend. It mirrors
 * `vibechek/tagger.py::apply_ml_tags` (the block starting "Two-stage genre
 * confidence") field for field — keep them in step; there is no codegen for
 * tagger.py, so only review catches drift.
 */

import type { MLResult, TaggingConfig } from "../types";

/**
 * Genre sources that came from the AUDIO model. Mirrors
 * `vibechek/tagger.py::_AUDIO_GENRE_SOURCES`.
 *
 * `ml_genre_raw_confidence` used to hold the audio model's single-class score
 * whatever source actually won, which is why the gate below substitutes the
 * family score for non-audio sources. `analyzer._reconcile_record_genre` now
 * re-stamps that field with the reconciled confidence for those sources, so on
 * a report from the current analyzer the substitution changes nothing — it is
 * kept (here and in tagger.py) for reports written before that landed, and so
 * the two copies of the rule stay identical.
 */
const AUDIO_GENRE_SOURCES = ["ml", "ml_override"];

/** Which branch of the tagger's genre gate a track lands in. */
export type GenreOutcome =
  /** Stage 1: the raw subgenre confidence cleared the strict threshold. */
  | "subgenre"
  /** Stage 2: only the parent family cleared its (lower) threshold. */
  | "parent-only"
  /** Cleared a gate, but `write_genre` is off — nothing is written. */
  | "write-disabled"
  /** Neither gate cleared — no genre frame is touched. */
  | "low-confidence";

export interface GenreGateDecision {
  outcome: GenreOutcome;
  /** True when a genre frame will actually be rewritten on disk. */
  willWrite: boolean;
  /** The label that lands in the MAIN genre frame (TCON/GENRE), or "" when
   *  nothing is written. This is what the user must be shown — the preview
   *  used to promise the subgenre even where the parent is written. */
  genreToWrite: string;
  /** The label that lands in the SUBGENRE frame, or "" (stage 2 clears it). */
  subgenreToWrite: string;
}

/** The subset of the tagging config the gate reads. */
export type GenreGateConfig = Pick<
  TaggingConfig,
  | "genre_confidence_threshold"
  | "parent_genre_confidence_threshold"
  | "write_genre"
  | "write_subgenre_as_main_genre"
>;

/**
 * Decide what the tagger will do with one track's genre.
 *
 * `ml` may be null/undefined (an unanalyzed track): the tagger `continue`s on
 * an empty `ml_analysis`, so nothing is written and nothing is counted.
 */
export function decideGenre(
  ml: MLResult | null | undefined,
  cfg: GenreGateConfig,
): GenreGateDecision {
  const none: GenreGateDecision = {
    outcome: "low-confidence",
    willWrite: false,
    genreToWrite: "",
    subgenreToWrite: "",
  };
  if (!ml) return none;

  const familyConf = ml.ml_genre_confidence ?? 0;
  const rawConf = ml.ml_genre_raw_confidence;
  // A report written before `ml_genre_raw_confidence` was plumbed can't
  // distinguish a confident parent from a subgenre prediction, so stage 2 is
  // held back entirely and stage 1 falls back to the family score.
  const isLegacyReport = rawConf == null;
  const genreSource = ml.ml_genre_source ?? "";

  let subgenreConf: number;
  if (isLegacyReport) {
    subgenreConf = familyConf;
  } else if (genreSource && !AUDIO_GENRE_SOURCES.includes(genreSource)) {
    // Tag-, web- or review-sourced genre. On a pre-fix report the raw
    // confidence still describes the (unrelated) audio read, so stage 1 is
    // judged on the family score instead. On a current report the analyzer has
    // already stamped the reconciled confidence into both fields, so this arm
    // picks the same number either way — belt and braces, see
    // AUDIO_GENRE_SOURCES above.
    subgenreConf = familyConf;
  } else {
    subgenreConf = rawConf ?? 0;
  }

  const subgenre = ml.ml_subgenre || "";
  const parentGenre = ml.ml_genre || "";

  const applySubgenre =
    subgenreConf >= cfg.genre_confidence_threshold && !!subgenre;
  const applyParentOnly =
    !applySubgenre &&
    !isLegacyReport &&
    familyConf >= cfg.parent_genre_confidence_threshold &&
    !!parentGenre;

  if (!applySubgenre && !applyParentOnly) return none;

  if (!cfg.write_genre) {
    return {
      outcome: "write-disabled",
      willWrite: false,
      genreToWrite: "",
      subgenreToWrite: "",
    };
  }

  if (applySubgenre) {
    return {
      outcome: "subgenre",
      willWrite: true,
      genreToWrite: cfg.write_subgenre_as_main_genre
        ? subgenre
        : parentGenre || subgenre,
      subgenreToWrite: subgenre,
    };
  }
  return {
    outcome: "parent-only",
    willWrite: true,
    genreToWrite: parentGenre,
    subgenreToWrite: "",
  };
}
