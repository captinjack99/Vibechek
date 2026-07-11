/**
 * WP7: the "Ready to analyze?" banner must not paint a green Essentia row for a
 * build that can't actually serve the engine it's evaluating. The bundled
 * DSP-only Windows wheel imports fine (`installed=true`) but can't run
 * essentia_tf/onnx in-process, so the row is gated on `essentia_usable`.
 */

import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";

import { PreflightSection } from "./SettingsSystem";
import type { PreflightResult } from "../types";

function makePreflight(overrides: Partial<PreflightResult> = {}): PreflightResult {
  return {
    ready: false,
    essentia: { installed: false, version: null, error: null },
    models: { models_dir: "/m", found: ["a"], missing: [], total_size_mb: 100, per_model: [] },
    platform: "test",
    wsl: null,
    native_venv: null,
    analyze_via: null,
    engine: "essentia_tf",
    essentia_usable: false,
    reasons_not_ready: [],
    ...overrides,
  } as unknown as PreflightResult;
}

describe("<PreflightSection /> — Essentia row honesty", () => {
  it("shows a plain green 'installed in the sidecar' row when essentia can serve the engine", () => {
    render(
      <PreflightSection
        preflight={makePreflight({
          essentia: { installed: true, version: "2.1", error: null },
          essentia_usable: true,
        })}
        onRefresh={vi.fn()}
        onSetupClick={vi.fn()}
      />,
    );
    expect(screen.getByText(/installed in the sidecar \(2\.1\)/i)).toBeInTheDocument();
    // The honest "can't run this engine" caption must NOT appear when usable.
    expect(screen.queryByText(/can't run this engine/i)).not.toBeInTheDocument();
  });

  it("does NOT show a green row when essentia is installed but can't serve the engine", () => {
    // The exact audited scenario: DSP-only wheel present (installed), but not
    // TF-capable so essentia_usable=false, and no WSL fallback set up.
    render(
      <PreflightSection
        preflight={makePreflight({
          essentia: { installed: true, version: "2.1-dsp", error: null },
          essentia_usable: false,
          wsl: { is_windows: true, can_run_vibechek: false, usable_distro: null } as never,
        })}
        onRefresh={vi.fn()}
        onSetupClick={vi.fn()}
      />,
    );
    // Explicit installed-but-can't-serve wording, not a green "installed" check.
    expect(screen.getByText(/can't run this engine in-process/i)).toBeInTheDocument();
    expect(
      screen.queryByText(/^installed in the sidecar \(2\.1-dsp\)$/i),
    ).not.toBeInTheDocument();
  });

  it("shows 'not installed inside your WSL distro' when essentia is absent on Windows", () => {
    render(
      <PreflightSection
        preflight={makePreflight({
          essentia: { installed: false, version: null, error: null },
          essentia_usable: false,
          wsl: { is_windows: true, can_run_vibechek: false, usable_distro: null } as never,
        })}
        onRefresh={vi.fn()}
        onSetupClick={vi.fn()}
      />,
    );
    expect(
      screen.getByText(/not installed inside your WSL distro yet/i),
    ).toBeInTheDocument();
  });
});
