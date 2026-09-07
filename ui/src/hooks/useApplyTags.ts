/**
 * Shared hook for applying ML tags — bulk or single-track.
 *
 * Both `LibraryBrowser` (bulk) and `TrackDetails` (one) had near-identical
 * code: read tagging config, call `apply_ml_tags`, manage the operation
 * store's loading/error state, surface a result. This hook centralises it.
 *
 * The caller decides what to do with the result — show a toast, refresh,
 * close a dialog, etc.
 */

import { useCallback } from "react";

import { useConfigStore, useNotificationStore, useOperationStore } from "../stores";
import { rpc, RpcError } from "./useSidecar";
import type { TagApplyStats } from "../api/methods";
import type { TrackAnalysis } from "../types";
import { KIND_LABELS } from "../components/AnalysisProgress";

/** Plain-language name for an operation kind (e.g. "download-models" →
 *  "Downloading ML models"), so "X is in progress" never shows a raw op id. */
function opLabel(kind: string): string {
  return KIND_LABELS[kind] ?? kind;
}

export interface ApplyTagsResult {
  /** Files where the genre was written (confidence >= threshold). */
  applied: number;
  /** Files where the SUBgenre was too uncertain but the parent-genre family
   *  cleared its (lower) threshold — the parent genre WAS written. These are
   *  neither `applied` nor `skipped`; reporting only those two hides them. */
  parentOnly: number;
  /** Files where genre was skipped because confidence was too low. */
  skipped: number;
  /** Files whose genre was confident enough but wasn't written because the
   *  `write_genre` toggle is off — neither applied nor low-confidence. */
  skippedWriteDisabled: number;
  /** Files where non-genre tags (energy/mood/timeslot/etc.) were written. */
  other: number;
  errors: string[];
}

interface UseApplyTagsReturn {
  apply: (tracks: TrackAnalysis[]) => Promise<ApplyTagsResult | null>;
  /**
   * True while *any* long-running operation owned by the sidecar is in
   * flight. The original implementation only checked for
   * `active === "tag"`, which let users click Apply while a Backup was
   * still running — the RPC would reject with `{busy: true}`, the local
   * `begin("tag")` had already wiped the backup's progress UI, and the user
   * was left looking at a confusing red toast while the backup silently
   * continued in the background. Now any active op blocks Apply.
   */
  isApplying: boolean;
}

export function useApplyTags(): UseApplyTagsReturn {
  const taggingCfg = useConfigStore((s) => s.config.tagging);
  const active = useOperationStore((s) => s.active);
  const begin = useOperationStore((s) => s.begin);
  const finish = useOperationStore((s) => s.finish);
  const fail = useOperationStore((s) => s.fail);
  const notify = useNotificationStore((s) => s.notify);

  const apply = useCallback(
    async (tracks: TrackAnalysis[]): Promise<ApplyTagsResult | null> => {
      if (tracks.length === 0) return null;

      // Gate against concurrent long-ops *before* we flip operation state.
      // If we begin("tag") first and then the sidecar rejects with busy,
      // we'd have already wiped the in-flight backup's progress UI — exactly
      // the original bug.
      if (active !== null) {
        const label =
          active === "backup"
            ? "Backup in progress, please wait."
            : `${opLabel(active)} is in progress, please wait.`;
        notify(label, { kind: "info" });
        return null;
      }

      const opId = begin("tag");
      try {
        // Trim the per-track payload down to just what the RPC needs:
        // `path` (used as the file identifier) and `ml_analysis` (the
        // source of all written tags). Sending the full TrackAnalysis with
        // existing_tags / filename_* fields is wasteful — on a 12k library
        // the JSON-RPC payload shrinks by ~80%.
        const slim = tracks.map((t) => ({
          path: t.path,
          ml_analysis: t.ml_analysis ?? null,
        }));

        // The generated asdict() shape of tagger.ApplyStats — no local copy, so
        // a renamed or dropped field is a compile error, not a silent zero.
        const stats = await rpc<TagApplyStats>("apply_ml_tags", {
          op_id: opId,
          analysis: { tracks: slim },
          confidence: taggingCfg.genre_confidence_threshold,
          parent_genre_confidence_threshold: taggingCfg.parent_genre_confidence_threshold,
          // Per-field write toggles — each ML field is written independently.
          write_genre: taggingCfg.write_genre,
          write_bpm: taggingCfg.write_bpm,
          write_key: taggingCfg.write_key,
          write_energy: taggingCfg.write_energy,
          write_mood: taggingCfg.write_mood,
          write_timeslot: taggingCfg.write_timeslot,
          write_direction: taggingCfg.write_direction,
          write_vocal: taggingCfg.write_vocal,
          vocal_instrumental_max: taggingCfg.vocal_instrumental_max,
          vocal_full_min: taggingCfg.vocal_full_min,
          preserve_rekordbox_frames: taggingCfg.preserve_rekordbox_frames,
          id3_text_encoding: taggingCfg.id3_text_encoding,
          // These two Settings toggles were previously dead end-to-end: the
          // handler reads them with `params.get(name, True)` (vibechek/rpc.py),
          // but useApplyTags never sent them — so flipping either OFF silently
          // had no effect. Forward them so the persisted choice reaches the RPC.
          write_subgenre_as_main_genre: taggingCfg.write_subgenre_as_main_genre,
          backup_before_write: taggingCfg.backup_before_write,
        });
        finish();
        return {
          applied: stats.genre_applied,
          parentOnly: stats.genre_applied_parent_only,
          skipped: stats.genre_skipped_low_confidence,
          skippedWriteDisabled: stats.genre_skipped_write_disabled,
          other: stats.other_tags_applied,
          errors: stats.errors,
        };
      } catch (e) {
        // If the sidecar rejected because *another* long op started between
        // our active-check and the RPC dispatch (race window — possible if
        // a backup was kicked off in another component a few ms ago), turn
        // the cryptic "Another long-running operation ('backup') is already
        // in progress" into a friendly toast and keep the button alive.
        if (
          e instanceof RpcError &&
          typeof e.data === "object" &&
          e.data !== null &&
          (e.data as { busy?: boolean }).busy === true
        ) {
          const running = (e.data as { running?: string }).running;
          const label =
            running === "backup"
              ? "Backup in progress, please wait."
              : running
                ? `${opLabel(running)} is in progress, please wait.`
                : "Another operation is in progress, please wait.";
          notify(label, { kind: "info" });
          // Reset our local op state so the Apply button re-enables.
          finish();
          return null;
        }
        fail(e);
        return null;
      }
    },
    [taggingCfg, active, begin, finish, fail, notify],
  );

  return { apply, isApplying: active !== null };
}
