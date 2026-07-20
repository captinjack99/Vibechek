/**
 * Auto-selection rules for which file to keep in a duplicate group.
 *
 * The user orders the rules in a priority list. To pick a keeper we sort
 * the files by the first rule; ties are broken by the second; and so on.
 *
 * This is the only place where we encode an opinion about "best version" —
 * everything else (duplicate detection, file operations) is policy-free.
 *
 * Tie-break / missing-value policy
 * ---------------------------------
 * Some fields can be null/undefined on disk (e.g. mutagen couldn't probe a
 * corrupt FLAC and the bitrate came back null). Earlier versions of this
 * module coerced nulls to 0, which silently meant "loses to anything" — a
 * rule-order-dependent footgun. The current policy:
 *
 *   - If EITHER file is missing the data needed for a rule, the rule
 *     returns 0 (skip this rule for this pair). The next enabled rule
 *     decides.
 *   - If a single file is missing data and the comparison can still be
 *     made meaningfully (e.g. one file has a codec, the other doesn't),
 *     the file *with* the data wins — equivalent to "missing = last
 *     place / worst keeper candidate" for that single rule.
 *
 * Net effect: a file with rich metadata never loses a rule it would
 * normally win, and the user doesn't see surprise reorderings just by
 * dragging a rule up the priority list.
 */

import type { FileInfo } from "../types";
import { KEEPER_FORMAT_PRIORITY } from "./keeperConstants";

export type RuleCriterion =
  | "codec"          // FLAC > WAV > AIFF > M4A > MP3 > AAC > OGG > ...
  | "bitrate"        // higher kbps wins
  | "size"           // larger file wins (more data = usually higher quality)
  | "modified"       // newer wins (latest version of the track)
  | "shortest_path"; // shorter path wins (top-level libraries > deep subfolders)

export interface KeeperRule {
  criterion: RuleCriterion;
  enabled: boolean;
}

/** Default ordering — what most people mean by "keep the best version". */
export const DEFAULT_RULES: KeeperRule[] = [
  { criterion: "codec", enabled: true },
  { criterion: "bitrate", enabled: true },
  { criterion: "size", enabled: true },
  { criterion: "modified", enabled: true },
  { criterion: "shortest_path", enabled: true },
];

/**
 * Codec quality order. Index 0 = best. Files with unknown codecs go last.
 *
 * Derived from the Python KEEPER_FORMAT_PRIORITY map (the single source of
 * truth, generated from vibechek/duplicates.py). We strip the leading dot
 * from each extension so the TS side can compare against `FileInfo.codec`
 * (which holds bare codec names like "mp3", not ".mp3").
 *
 * `alac` and `opus` aren't currently in the Python map; they're appended
 * here as a TS-only hint slotted in alongside their lossless / lossy peers.
 * If/when the auto-keeper logic in Python grows to recognize them, add them
 * to `_KEEPER_FORMAT_PRIORITY` and they'll appear here automatically.
 */
export const CODEC_PREFERENCE: string[] = (() => {
  // Sort the shared priority map by ascending priority (lower = better),
  // strip dots. Ties are kept in insertion order (which JS preserves on
  // Object.entries for string keys after we sort by the numeric priority).
  const fromShared = Object.entries(KEEPER_FORMAT_PRIORITY)
    .sort(([, a], [, b]) => a - b)
    .map(([ext]) => ext.replace(/^\./, ""));

  // TS-only additions: codecs Python doesn't yet recognize as duplicates,
  // but which the UI still wants to rank.
  const extras = ["alac", "opus"];
  const seen = new Set(fromShared);
  const ordered: string[] = [];
  // Insert lossless extras (alac) right after the last lossless format,
  // and lossy extras (opus) right after the last "good lossy" (m4a).
  for (const codec of fromShared) {
    ordered.push(codec);
    if (codec === "aif" && !seen.has("alac")) ordered.push("alac");
    if (codec === "m4a" && !seen.has("opus")) ordered.push("opus");
  }
  // Anything not slotted in (defensive) appended at the end before wma.
  for (const e of extras) if (!ordered.includes(e)) ordered.push(e);
  return ordered;
})();

const RULE_LABELS: Record<RuleCriterion, string> = {
  codec:         "File format (FLAC > WAV > MP3 > ...)",
  bitrate:       "Audio bitrate (highest first)",
  size:          "File size (largest first)",
  modified:      "Modified date (newest first)",
  shortest_path: "Folder depth (shortest path first)",
};

/** Terse, plain-language names for the inline "picked by ..." hint on each
 *  group. The full RULE_LABELS are too long to sit inline, and the raw
 *  criterion enum ("shortest_path") is engineer-speak — this is the middle
 *  ground the user actually reads. */
const RULE_SHORT_LABELS: Record<RuleCriterion, string> = {
  codec:         "file format",
  bitrate:       "bitrate",
  size:          "file size",
  modified:      "modified date",
  shortest_path: "folder depth",
};

const RULE_HELP: Record<RuleCriterion, string> = {
  codec:         "Prefers lossless formats (FLAC, WAV, AIFF) over compressed ones; this internal ranking isn't user-editable yet.",
  bitrate:       "Higher bitrate usually = better quality. Treats lossless as effectively infinite.",
  size:          "Larger files usually contain more data. Reliable fallback when bitrate is unknown.",
  modified:      "Picks the most recently saved copy — useful if you re-downloaded a track.",
  shortest_path: "Files at the top of your library win over copies buried in subfolders.",
};

export function ruleLabel(c: RuleCriterion): string {
  return RULE_LABELS[c];
}

/** Terse plain-language name for the inline group hint (e.g. "folder depth"). */
export function ruleShortLabel(c: RuleCriterion): string {
  return RULE_SHORT_LABELS[c];
}

export function ruleHelp(c: RuleCriterion): string {
  return RULE_HELP[c];
}

// ---------------------------------------------------------------------------
// Comparison
// ---------------------------------------------------------------------------

/** True if `v` is a real, comparable number (not null/undefined/NaN). */
function hasNum(v: number | null | undefined): v is number {
  return typeof v === "number" && Number.isFinite(v);
}

/** True if `v` is a non-empty string (not null/undefined/""). */
function hasStr(v: string | null | undefined): v is string {
  return typeof v === "string" && v.length > 0;
}

/**
 * Single-rule tie-break for missing data: if exactly one side has the value
 * it wins; if neither has it, it's a tie. Returns null if both have it
 * (caller should compare normally).
 */
function missingTieBreak(
  aHas: boolean,
  bHas: boolean,
): number | null {
  if (aHas && bHas) return null;        // both present, compare normally
  if (aHas && !bHas) return -1;         // a wins (has data)
  if (!aHas && bHas) return 1;          // b wins (has data)
  return 0;                              // neither — tie, defer to next rule
}

/**
 * Compare two files for a single rule. Returns a negative number if `a` should
 * come first (i.e., be kept), positive if `b` should, 0 for ties.
 *
 * Per the policy at the top of this file: missing data is treated as
 * "last-place for this rule, defer to the next rule when both sides are
 * missing." Crucially, no rule returns NaN; the comparator chain is total.
 */
function compareForRule(a: FileInfo, b: FileInfo, criterion: RuleCriterion): number {
  switch (criterion) {
    case "codec": {
      const aMiss = missingTieBreak(hasStr(a.codec), hasStr(b.codec));
      if (aMiss !== null) return aMiss;
      const ai = codecRank(a.codec);
      const bi = codecRank(b.codec);
      return ai - bi;  // lower index = better
    }
    case "bitrate": {
      // Lossless files often have huge derived "bitrate" values (e.g. 1411
      // for CD-quality WAV), so this naturally prefers lossless.
      const aMiss = missingTieBreak(hasNum(a.bitrate_kbps), hasNum(b.bitrate_kbps));
      if (aMiss !== null) return aMiss;
      return (b.bitrate_kbps as number) - (a.bitrate_kbps as number);
    }
    case "size": {
      // size_mb is always populated server-side, but be defensive in case a
      // synthesized FileInfo (tests, future code paths) leaves it null.
      const aBytes = hasNum(a.size_bytes) ? a.size_bytes : (hasNum(a.size_mb) ? a.size_mb * 1024 * 1024 : null);
      const bBytes = hasNum(b.size_bytes) ? b.size_bytes : (hasNum(b.size_mb) ? b.size_mb * 1024 * 1024 : null);
      const aMiss = missingTieBreak(hasNum(aBytes), hasNum(bBytes));
      if (aMiss !== null) return aMiss;
      return (bBytes as number) - (aBytes as number);
    }
    case "modified": {
      const aMiss = missingTieBreak(hasNum(a.modified_time), hasNum(b.modified_time));
      if (aMiss !== null) return aMiss;
      return (b.modified_time as number) - (a.modified_time as number);
    }
    case "shortest_path": {
      // path is required; if for some reason it isn't a string, the
      // comparator should still be a total order.
      const aLen = typeof a.path === "string" ? a.path.length : Number.MAX_SAFE_INTEGER;
      const bLen = typeof b.path === "string" ? b.path.length : Number.MAX_SAFE_INTEGER;
      return aLen - bLen;
    }
  }
}

function codecRank(codec: string | null | undefined): number {
  if (!codec) return 999;
  const idx = CODEC_PREFERENCE.indexOf(codec.toLowerCase());
  return idx >= 0 ? idx : 999;
}

/**
 * Pick the keeper from a duplicate group by applying enabled rules in order.
 * The first file after sorting wins.
 */
export function pickKeeper(files: FileInfo[], rules: KeeperRule[]): FileInfo {
  if (files.length === 0) throw new Error("pickKeeper: empty file list");
  if (files.length === 1) return files[0];

  const active = rules.filter((r) => r.enabled);
  const sorted = [...files].sort((a, b) => {
    for (const rule of active) {
      const cmp = compareForRule(a, b, rule.criterion);
      if (cmp !== 0) return cmp;
    }
    return 0;
  });
  return sorted[0];
}

/**
 * Explain why a file was chosen — surfaces the first rule that decided it.
 * Returns "tie" if no rule distinguished it.
 */
export function explainPick(
  keeper: FileInfo,
  rest: FileInfo[],
  rules: KeeperRule[],
): { criterion: RuleCriterion | "tie"; detail: string } {
  for (const rule of rules.filter((r) => r.enabled)) {
    const winner = rest.find((f) => compareForRule(keeper, f, rule.criterion) < 0);
    if (winner) {
      return {
        criterion: rule.criterion,
        detail: explainOne(keeper, rule.criterion),
      };
    }
  }
  return { criterion: "tie", detail: "all candidates equal" };
}

function explainOne(f: FileInfo, criterion: RuleCriterion): string {
  switch (criterion) {
    case "codec":         return `${(f.codec || "?").toUpperCase()}`;
    case "bitrate":       return f.bitrate_kbps ? `${f.bitrate_kbps} kbps` : "unknown bitrate";
    case "size":          return `${f.size_mb.toFixed(1)} MB`;
    case "modified":      return f.modified_time ? new Date(f.modified_time * 1000).toLocaleDateString() : "?";
    case "shortest_path": return `${f.path.split(/[/\\]/).length} levels`;
  }
}
