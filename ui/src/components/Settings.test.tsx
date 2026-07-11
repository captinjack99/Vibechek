/**
 * WP7: two Settings-page honesty fixes.
 *
 *  #3  The "Set up CLAP genre engine" button must be hidden on the native
 *      (no-WSL, in-process) engine — CLAP has no venv there and the setup RPC
 *      rejects it with an error that blames native. We show an inline hint
 *      instead, mirroring the engine-gated "Set up ONNX engine" button.
 *
 *  #1b refreshPreflight must send the SELECTED engine so the readiness banner
 *      judges the engine the user is actually on, not the RPC's essentia_tf
 *      default.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { invoke } from "@tauri-apps/api/core";

import { Settings } from "./Settings";
import { useConfigStore } from "../stores";
import type { PreflightResult } from "../types";

// A not-ready preflight (analyze_via null) so the mount flow never fires the
// engine-GPU probe — keeps the mock surface small.
function preflightResult(): PreflightResult {
  return {
    ready: false,
    essentia: { installed: false, version: null, error: null },
    models: { models_dir: "/m", found: [], missing: ["a"], total_size_mb: 0, per_model: [] },
    platform: "test",
    wsl: { is_windows: false, can_run_vibechek: false, usable_distro: null, distros: [] },
    native_venv: null,
    analyze_via: null,
    engine: "essentia_tf",
    essentia_usable: false,
    reasons_not_ready: ["not ready"],
  } as unknown as PreflightResult;
}

function mockSidecar() {
  (invoke as ReturnType<typeof vi.fn>).mockImplementation(
    async (cmd: string, args?: { method?: string }) => {
      if (cmd === "sidecar_status") return { binary: "vibechek" };
      if (cmd !== "rpc_call") return {};
      switch (args?.method) {
        case "system_info":
          return {
            cpu_count: 8,
            memory_total_mb: 16000,
            memory_available_mb: 8000,
            gpu_available: false,
            gpu_devices: [],
            unsupported_gpu_count: 0,
            recommended_workers: 7,
            platform: "test",
          };
        case "preflight":
          return preflightResult();
        case "worker_budget":
          return null; // slider falls back to its static ceiling
        case "list_profiles":
          return { profiles: [] };
        default:
          return {};
      }
    },
  );
}

function setEngine(inference_engine: string, genre_classifier = "clap") {
  const base = useConfigStore.getState().config;
  useConfigStore.setState({
    config: {
      ...base,
      analysis: { ...base.analysis, inference_engine, genre_classifier },
    },
    loaded: true,
  });
}

beforeEach(() => {
  mockSidecar();
});

describe("<Settings /> — CLAP setup button engine gating", () => {
  it("hides the CLAP setup button and explains why on the native engine", async () => {
    setEngine("native", "clap");
    render(<Settings />);

    // The inline hint appears in place of the button.
    expect(
      await screen.findByText(/CLAP requires the ONNX or Essentia·TF engine/i),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /Set up CLAP genre engine/i }),
    ).not.toBeInTheDocument();
  });

  it("shows the CLAP setup button on the essentia_tf engine", async () => {
    setEngine("essentia_tf", "clap");
    render(<Settings />);

    expect(
      await screen.findByRole("button", { name: /Set up CLAP genre engine/i }),
    ).toBeInTheDocument();
    expect(
      screen.queryByText(/CLAP requires the ONNX or Essentia·TF engine/i),
    ).not.toBeInTheDocument();
  });
});

describe("<Settings /> — preflight is evaluated for the selected engine", () => {
  it("passes the configured engine to the preflight RPC", async () => {
    setEngine("native", "discogs");
    render(<Settings />);

    await waitFor(() => {
      const calls = (invoke as ReturnType<typeof vi.fn>).mock.calls as Array<
        [string, { method?: string; params?: { engine?: string } }?]
      >;
      const preflightCalls = calls.filter(
        ([cmd, a]) => cmd === "rpc_call" && a?.method === "preflight",
      );
      expect(preflightCalls.length).toBeGreaterThan(0);
      // Every preflight call must carry the user's engine, not the RPC default.
      expect(preflightCalls.every(([, a]) => a?.params?.engine === "native")).toBe(true);
    });
  });
});
