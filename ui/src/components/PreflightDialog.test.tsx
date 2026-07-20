/**
 * Regression test for the wrong progress-overlay label bug: the preflight
 * install flows all called `begin("download-models")`, so the global overlay
 * read "Downloading ML models" while it was actually installing WSL, a distro,
 * or the analysis engine. `actionProgressKind` maps each preflight action to
 * the correct operation kind (and therefore the correct KIND_LABELS label).
 */

import { describe, expect, it } from "vitest";

import { actionProgressKind } from "./PreflightDialog";
import { KIND_LABELS } from "./AnalysisProgress";

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
