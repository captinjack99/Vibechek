/**
 * Regression test for the audit finding "Chromaprint similarity-threshold
 * setting silently ignored by dedupe scan" (MED, frontend).
 *
 * The Settings slider writes `duplicates.chromaprint_similarity_threshold`, but
 * `handleScan` used to send only `{ path, use_md5, use_chromaprint }` to
 * `find_duplicates` — so the slider was dead. This asserts the configured
 * threshold actually reaches the RPC payload (the backend reads it as
 * `threshold`, see rpc._find_duplicates).
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { invoke } from "@tauri-apps/api/core";

import { DuplicatesView } from "./DuplicatesView";
import { useConfigStore, useLibraryStore } from "../stores";
import type { DuplicateReport } from "../types";

const emptyReport: DuplicateReport = {
  summary: {
    total_files: 0,
    exact_duplicate_groups: 0,
    exact_duplicate_files: 0,
    audio_duplicate_groups: 0,
    audio_duplicate_files: 0,
    total_duplicates: 0,
    space_recoverable_mb: 0,
  },
  exact_duplicates: [],
  audio_duplicates: [],
};

describe("<DuplicatesView /> — find_duplicates payload", () => {
  beforeEach(() => {
    (invoke as ReturnType<typeof vi.fn>).mockResolvedValue(emptyReport);
    // A library path seeds the scan path so the Scan button is enabled.
    useLibraryStore.setState({ libraryPath: "D:/Music" });
  });

  it("forwards the configured chromaprint similarity threshold to find_duplicates", async () => {
    const user = userEvent.setup();
    // Set a non-default threshold the user would have moved the slider to.
    useConfigStore.getState().updateDuplicates({
      use_md5: true,
      use_chromaprint: true,
      chromaprint_similarity_threshold: 0.81,
    });

    render(<DuplicatesView />);
    await user.click(screen.getByRole("button", { name: /scan/i }));

    await waitFor(() => {
      expect(invoke).toHaveBeenCalledWith(
        "rpc_call",
        expect.objectContaining({
          method: "find_duplicates",
          params: expect.objectContaining({ threshold: 0.81 }),
        }),
      );
    });
  });
});
