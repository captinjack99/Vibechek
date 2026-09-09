/**
 * Regression test for the wrong progress-overlay label bug: the preflight
 * install flows all called `begin("download-models")`, so the global overlay
 * read "Downloading ML models" while it was actually installing WSL, a distro,
 * or the analysis engine. `actionProgressKind` maps each preflight action to
 * the correct operation kind (and therefore the correct KIND_LABELS label).
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { invoke } from "@tauri-apps/api/core";

import { PreflightDialog, actionProgressKind } from "./PreflightDialog";
import { KIND_LABELS } from "./AnalysisProgress";
import { useOperationStore } from "../stores";
import type { PreflightResult } from "../types";

describe("actionProgressKind — preflight action → progress-overlay kind", () => {
  it("maps the WSL and distro installs to install-wsl", () => {
    expect(actionProgressKind("wsl")).toBe("install-wsl");
    expect(actionProgressKind("distro")).toBe("install-wsl");
  });

  it("maps the shared vibechek/essentia install to install-essentia", () => {
    // One action drives both install_vibechek_in_wsl and install_essentia_native.
    expect(actionProgressKind("vibechek")).toBe("install-essentia");
  });

  it("keeps the non-install actions on the generic download-models kind", () => {
    expect(actionProgressKind("models")).toBe("download-models");
    expect(actionProgressKind(null)).toBe("download-models");
  });

  it("never routes an install back to the generic download-models label", () => {
    for (const action of ["wsl", "distro", "vibechek"] as const) {
      expect(actionProgressKind(action)).not.toBe("download-models");
    }
  });

  it("resolves to a label that actually exists in KIND_LABELS", () => {
    for (const action of ["wsl", "distro", "vibechek", "models", null] as const) {
      expect(KIND_LABELS[actionProgressKind(action)]).toBeTruthy();
    }
  });

  it("labels the analysis-engine install with the app's one term", () => {
    // Vocabulary: "analysis engine", not "analyzer".
    expect(KIND_LABELS[actionProgressKind("vibechek")]).toBe(
      "Setting up the analysis engine",
    );
  });
});

// ---------------------------------------------------------------------------
// "Download models" bypassed the active-op guard every other step in
// this dialog carries. begin() would overwrite the running install's global op
// state and the busy-rejection's fail() would then clear `active` while the
// install was still going, orphaning its progress overlay and Cancel button.
// ---------------------------------------------------------------------------

function notReady(): PreflightResult {
  return {
    ready: false,
    essentia: { installed: false, version: null, error: null },
    models: {
      models_dir: "/m",
      found: [],
      missing: ["genre_discogs"],
      total_size_mb: 0,
      per_model: [],
    },
    platform: "test",
    wsl: { is_windows: false, can_run_vibechek: false, usable_distro: null, distros: [] },
    native_venv: null,
    analyze_via: null,
    engine: "essentia_tf",
    essentia_usable: false,
    reasons_not_ready: ["not ready"],
  } as unknown as PreflightResult;
}

function renderDialog() {
  return render(
    <PreflightDialog
      preflight={notReady()}
      onRefresh={() => {}}
      onClose={() => {}}
      onReady={() => {}}
    />,
  );
}

describe("<PreflightDialog /> — download models does not clobber a running op", () => {
  beforeEach(() => {
    useOperationStore.setState({ active: null, opId: null, progress: null });
    (invoke as ReturnType<typeof vi.fn>).mockResolvedValue({});
  });

  it("cannot be started while another op is running, and leaves it untouched", async () => {
    // A 3-5 minute WSL/engine install is already running elsewhere in the app.
    const runningOpId = useOperationStore.getState().begin("install-essentia");

    renderDialog();
    const btn = screen.getByRole("button", { name: /download models/i });
    expect(btn).toBeDisabled();
    fireEvent.click(btn);

    // No download was issued...
    await waitFor(() => {
      const calls = (invoke as ReturnType<typeof vi.fn>).mock.calls.filter(
        (c) => (c[1] as { method?: string })?.method === "download_models",
      );
      expect(calls).toHaveLength(0);
    });
    // ...and the REAL op still owns the global state. (Before the fix, begin()
    // replaced opId and the busy-rejection's fail() cleared `active` outright,
    // orphaning the install's progress overlay and its only Cancel button.)
    expect(useOperationStore.getState().active).toBe("install-essentia");
    expect(useOperationStore.getState().opId).toBe(runningOpId);
  });

  it("greys the button out while another op is running", () => {
    useOperationStore.getState().begin("install-essentia");
    renderDialog();
    expect(screen.getByRole("button", { name: /download models/i })).toBeDisabled();
  });

  it("runs normally when nothing else is active", async () => {
    renderDialog();
    fireEvent.click(screen.getByRole("button", { name: /download models/i }));

    await waitFor(() => {
      const calls = (invoke as ReturnType<typeof vi.fn>).mock.calls.filter(
        (c) => (c[1] as { method?: string })?.method === "download_models",
      );
      expect(calls).toHaveLength(1);
    });
  });

  // The dialog can be opened from Settings while an
  // unrelated op runs. Every install button is then greyed with no reason on
  // screen: the click-time "another operation is running" message can never
  // fire, because the click is suppressed. Dead buttons read as a broken
  // dialog, so the explanation has to be rendered, not waited for.
  it("explains on screen why every button is greyed out", () => {
    useOperationStore.getState().begin("install-essentia");
    renderDialog();

    expect(screen.getByText(/another operation is running/i)).toBeInTheDocument();
  });

  it("says nothing when nothing else is running", () => {
    renderDialog();

    expect(screen.queryByText(/another operation is running/i)).not.toBeInTheDocument();
  });
});
