/**
 * Trust-UX: surfacing genre source conflicts for one-click review.
 *
 * The analyzer reconciles three genre signals — the in-file tag, the pure-audio
 * model, and (optionally) an online web-synthesis lookup — into one effective
 * value, stamping the record with provenance: `ml_genre_source` (which signal
 * won) and `ml_genre_conflict` (whether they disagreed on family). See
 * `vibechek/genres.py:reconcile_genre` + `analyzer._reconcile_record_genre`.
 *
 * A skeptical pro doesn't want a black box that's "mostly right" — they want to
 * know WHERE a value came from and WHEN to second-guess it. These pure helpers
 * turn the raw provenance fields into a review flag + a plain-English reason the
 * library UI can show. Kept logic-only (no JSX) so they're trivially testable.
 */

import type { ExistingTags, MLResult } from "../types";

export type GenreSource = "tag" | "ml" | "ml_override" | "web" | "web_override";

/**
 * Severity of a track's genre provenance:
 *   - "override": Vibechek CHANGED the user's existing tag (a confident audio
 *     read or a grounded web source disagreed). Highest priority — their
 *     hand-curation was altered.
 *   - "conflict": the user's tag was KEPT, but a source disagreed on family.
 *     Informational — worth a glance, nothing was changed.
 *   - null: no disagreement (or no tag to disagree with).
 *
 * `ml_genre_conflict` is true in both the override and tag-kept cases (the
 * override paths always set it), so it's the single gate for "needs review".
 */
export function reviewSeverity(
  ml: MLResult | null | undefined,
): "override" | "conflict" | null {
  if (!ml || ml.ml_genre_conflict !== true) return null;
  // conflict===true only ever arises when a SPECIFIC tag existed — reconcile
  // returns conflict=false for the no-tag / generic-tag paths. So if any source
  // OTHER than the tag won, the user's tag was DISCARDED → "override". Only when
  // the tag itself won (source="tag", e.g. prefer_tag kept it or tag_only) was it
  // preserved despite a disagreeing signal → the calmer "conflict". Keying off
  // whether the tag won — not the "_override" suffix — keeps the prefer_ml /
  // ml-win cases honest (there source is plain "ml"/"web" yet the tag is gone).
  const src = ml.ml_genre_source;
  return src && src !== "tag" ? "override" : "conflict";
}

/**
 * A track "needs review" when its genre sources disagree — the in-file tag and
 * the audio/web read landed on different families. This is the one field with a
 * computed conflict signal today; named generically so other fields can fold in
 * later (key/BPM/energy currently carry no conflict flag).
 */
export function needsReview(ml: MLResult | null | undefined): boolean {
  return reviewSeverity(ml) !== null;
}

/** The audio model's genre (subgenre preferred), pre-reconciliation. */
function audioGenre(ml: MLResult): string | null {
  return ml.ml_subgenre_audio ?? ml.ml_genre_audio ?? null;
}

/** The effective genre shown to the user (subgenre preferred). */
function effectiveGenre(ml: MLResult): string | null {
  return ml.ml_subgenre ?? ml.ml_genre ?? null;
}

/**
 * One-line, human-readable explanation of a genre conflict — suitable for a row
 * tooltip or the detail panel. Returns null when there's nothing to review.
 */
export function reviewReason(
  ml: MLResult | null | undefined,
  existing: ExistingTags | null | undefined,
): string | null {
  const severity = reviewSeverity(ml);
  if (!ml || !severity) return null;

  const tag = existing?.genre ?? null;
  const effective = effectiveGenre(ml);
  const web = ml.ml_genre_web ?? null;
  const audio = audioGenre(ml);

  if (severity === "override") {
    // "web"/"web_override" → online source; "ml"/"ml_override" → audio model.
    const src = ml.ml_genre_source ?? "";
    const via = src.startsWith("web") ? "an online source" : "the audio model";
    return tag
      ? `Changed your tag “${tag}” → “${effective}” — ${via} disagreed.`
      : `Set to “${effective}” by ${via}.`;
  }

  // Tag kept, but a source read it differently.
  const other = web ?? audio;
  const reader = web ? "an online source" : "the audio model";
  if (tag && other) {
    return `Kept your tag “${tag}”, but ${reader} read “${other}”.`;
  }
  return "Sources disagree on the genre.";
}

export interface GenreProvenance {
  /** The in-file tag (existing.genre), if any. */
  tag: string | null;
  /** The pure-audio model's read (subgenre preferred), pre-reconcile. */
  audio: string | null;
  /** The online web-synthesis read, if the lookup ran. */
  web: string | null;
  /** Whether the web read cited an explicit source (vs. an LLM guess). */
  webGrounded: boolean;
  /** The effective value shown to the user. */
  effective: string | null;
  /** Which signal won. */
  source: GenreSource | null;
  /** Confidence of the effective value (0..1), if known. */
  confidence: number | null;
  /** Whether the sources disagreed on family. */
  conflict: boolean;
  severity: "override" | "conflict" | null;
}

/**
 * Structured provenance for the detail panel. Returns null when the record
 * wasn't reconciled (no `ml_genre_source` — e.g. a raw/in-progress record),
 * since there's no provenance to show.
 */
export function genreProvenance(
  ml: MLResult | null | undefined,
  existing: ExistingTags | null | undefined,
): GenreProvenance | null {
  if (!ml || !ml.ml_genre_source) return null;
  return {
    tag: existing?.genre ?? null,
    audio: audioGenre(ml),
    web: ml.ml_genre_web ?? null,
    webGrounded: ml.ml_genre_web_grounded === true,
    effective: effectiveGenre(ml),
    source: ml.ml_genre_source as GenreSource,
    confidence: ml.ml_genre_confidence ?? null,
    conflict: ml.ml_genre_conflict === true,
    severity: reviewSeverity(ml),
  };
}

/**
 * Whether the genre breakdown is worth showing in the detail panel. We hide it
 * for the boring case (tag kept and audio agreed, no web lookup) where the
 * existing diff row already says everything — and show it whenever there's a
 * conflict, an override, a web read, or the audio read differs from the value
 * we surfaced (so the user can see the alternative the model considered).
 */
export function hasInterestingProvenance(p: GenreProvenance | null): boolean {
  if (!p) return false;
  if (p.severity !== null) return true;
  if (p.web) return true;
  return !!p.audio && !!p.effective && p.audio.toLowerCase() !== p.effective.toLowerCase();
}
