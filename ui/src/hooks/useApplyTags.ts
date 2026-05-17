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

import { useConfigStore, useOperationStore } from "../stores";
import { rpc } from "./useSidecar";
import type { TrackAnalysis } from "../types";

export interface ApplyTagsResult {
  /** Files where the genre was written (confidence >= threshold). */
  applied: number;
  /** Files where genre was skipped because confidence was too low. */
  skipped: number;
  /** Files where non-genre tags (energy/mood/timeslot/etc.) were written. */
  other: number;
  errors: string[];
}

interface RpcStats {
  total: number;
  genre_applied: number;
  genre_skipped_low_confidence: number;
  other_tags_applied: number;
  errors: string[];
}

interface UseApplyTagsReturn {
  apply: (tracks: TrackAnalysis[]) => Promise<ApplyTagsResult | null>;
  isApplying: boolean;
}

export function useApplyTags(): UseApplyTagsReturn {
  const taggingCfg = useConfigStore((s) => s.config.tagging);
  const active = useOperationStore((s) => s.active);
  const begin = useOperationStore((s) => s.begin);
  const finish = useOperationStore((s) => s.finish);
  const fail = useOperationStore((s) => s.fail);

  const apply = useCallback(
    async (tracks: TrackAnalysis[]): Promise<ApplyTagsResult | null> => {
      if (tracks.length === 0) return null;
      begin("tag");
      try {
        const stats = await rpc<RpcStats>("apply_ml_tags", {
          analysis: { tracks },
          confidence: taggingCfg.genre_confidence_threshold,
          skip_bpm_and_key: taggingCfg.skip_bpm_and_key,
          preserve_rekordbox_frames: taggingCfg.preserve_rekordbox_frames,
        });
        finish();
        return {
          applied: stats.genre_applied,
          skipped: stats.genre_skipped_low_confidence,
          other: stats.other_tags_applied,
          errors: stats.errors,
        };
      } catch (e) {
        fail(String(e));
        return null;
      }
    },
    [taggingCfg, begin, finish, fail],
  );

  return { apply, isApplying: active === "tag" };
}
