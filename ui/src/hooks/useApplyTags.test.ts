/**
 * Regression test for "Two tag-write Settings toggles are dead: useApplyTags
 * never sends write_subgenre_as_main_genre or backup_before_write".
 *
 * `useApplyTags.apply()` is the single shared path that calls the
 * `apply_ml_tags` RPC. It enumerated every tagging field EXCEPT these two, so
 * the Settings switches (which persist fine) never reached the RPC — flipping
 * either OFF silently had no effect. The handler reads both with
 * `params.get(name, True)` (vibechek/rpc.py), so omitting them defaulted to
 * True regardless of the user's choice.
 *
 * This locks the payload to include EVERY Settings tagging toggle, and asserts
 * the user's OFF choices are forwarded as `false`.
 *
 * NOTE: making the toggles actually CHANGE behaviour additionally requires the
 * backend to consult the two flags in the apply path (tagger.py) — that is
 * outside this hook's ownership and is called out in the fix notes. This test
 * covers the frontend param-drift half of the fix.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import { renderHook } from "@testing-library/react";
import { invoke } from "@tauri-apps/api/core";

import { useApplyTags } from "./useApplyTags";
import { useConfigStore } from "../stores";
import type { TrackAnalysis } from "../types";

const okStats = {
  total: 1,
  genre_applied: 1,
  genre_skipped_low_confidence: 0,
  other_tags_applied: 1,
  errors: [],
};

const oneTrack: TrackAnalysis[] = [
  {
    path: "D:/Music/x.mp3",
    filename: "x.mp3",
    extension: ".mp3",
    size_mb: 5,
    existing_tags: {},
    ml_analysis: {
      ml_genre: "House",
      ml_subgenre: "Deep House",
      ml_genre_confidence: 0.95,
      ml_genre_raw_confidence: 0.95,
      ml_bpm: 124,
      ml_key: "8A",
      ml_energy: 3,
      ml_mood: "Happy",
      ml_timeslot: "Peak",
      ml_direction: "Up",
      ml_vocal: "Instrumental",
      ml_vocal_score: 0.1,
      ml_vocal_peak: null,
      ml_vocal_frac: null,
      ml_danceability: 0.8,
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
    },
    error: null,
  },
];

describe("useApplyTags — apply_ml_tags payload", () => {
  beforeEach(() => {
    (invoke as ReturnType<typeof vi.fn>).mockResolvedValue(okStats);
  });

  it("forwards write_subgenre_as_main_genre and backup_before_write", async () => {
    // User turned BOTH toggles OFF in Settings.
    useConfigStore.getState().updateTagging({
      write_subgenre_as_main_genre: false,
      backup_before_write: false,
    });

    const { result } = renderHook(() => useApplyTags());
    await result.current.apply(oneTrack);

    expect(invoke).toHaveBeenCalledWith(
      "rpc_call",
      expect.objectContaining({
        method: "apply_ml_tags",
        params: expect.objectContaining({
          write_subgenre_as_main_genre: false,
          backup_before_write: false,
        }),
      }),
    );
  });

  it("forwards the ON state too (defaults)", async () => {
    useConfigStore.getState().updateTagging({
      write_subgenre_as_main_genre: true,
      backup_before_write: true,
    });

    const { result } = renderHook(() => useApplyTags());
    await result.current.apply(oneTrack);

    const call = (invoke as ReturnType<typeof vi.fn>).mock.calls.find(
      ([, args]) => (args as { method: string }).method === "apply_ml_tags",
    );
    expect(call).toBeTruthy();
    const params = (call![1] as { params: Record<string, unknown> }).params;
    expect(params.write_subgenre_as_main_genre).toBe(true);
    expect(params.backup_before_write).toBe(true);
  });
});
