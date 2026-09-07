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
import { act, render, screen, waitFor, fireEvent } from "@testing-library/react";
import { invoke } from "@tauri-apps/api/core";

import { Settings, orderedVocalBands } from "./Settings";
import {
  useConfigStore, useLibraryStore, useNotificationStore, useOperationStore,
} from "../stores";
import type { PreflightResult, WorkerBudget } from "../types";

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
  // The worker budget is now library-dependent; don't let one test's root leak
  // into the next.
  useLibraryStore.setState({ libraryPath: null });
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

/** A worker-budget that refuses even one worker (the CLAP/WSL out-of-memory
 *  case) — max_workers 0, a plain refusal reason, WSL VM pool. */
function refusalBudget(): WorkerBudget {
  return {
    max_workers: 0,
    effective_workers: 0,
    per_worker_mb: 4608,
    ram_seen_mb: 6000,
    reserve_mb: 4096,
    gpu_workers: 0,
    cpu_workers: 0,
    refusal_reason: "Not enough memory to run the advanced genre model right now.",
    ram_pool: "wsl_vm",
    requested_workers: 4,
    ram_measured: true,
  } as unknown as WorkerBudget;
}

/** mockSidecar variant that lets a test override individual RPC results. */
function mockSidecarWith(overrides: Record<string, unknown>) {
  (invoke as ReturnType<typeof vi.fn>).mockImplementation(
    async (cmd: string, args?: { method?: string }) => {
      if (cmd === "sidecar_status") return { binary: "vibechek" };
      if (cmd !== "rpc_call") return {};
      const m = args?.method ?? "";
      if (m in overrides) return overrides[m];
      switch (m) {
        case "system_info":
          return {
            cpu_count: 8, memory_total_mb: 16000, memory_available_mb: 8000,
            gpu_available: false, gpu_devices: [], unsupported_gpu_count: 0,
            recommended_workers: 7, platform: "test",
          };
        case "preflight":
          return preflightResult();
        case "list_profiles":
          return { profiles: [] };
        default:
          return {};
      }
    },
  );
}

describe("<Settings /> — worker-slider memory refusal (WP-D)", () => {
  it("renders the two shared memory-refusal buttons when the budget refuses a run", async () => {
    setEngine("essentia_tf", "clap");
    mockSidecarWith({ worker_budget: refusalBudget() });
    render(<Settings />);

    expect(
      await screen.findByText(/Not enough memory to run the advanced genre model/i),
    ).toBeInTheDocument();
    // Same two buttons the ErrorToast shows — from the shared component.
    expect(screen.getByText("Switch to the standard genre model")).toBeInTheDocument();
    expect(screen.getByText("Give Vibechek more memory")).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// P01 — the run sizes each worker for the longest track in the loaded library,
// so a worker_budget computed without the library path reports the flat-budget
// maximum while the run plans fewer workers. Every worker_budget call from this
// screen must carry the loaded root.
// ---------------------------------------------------------------------------

function workerBudgetParams(): Array<Record<string, unknown>> {
  const calls = (invoke as ReturnType<typeof vi.fn>).mock.calls as Array<
    [string, { method?: string; params?: Record<string, unknown> }?]
  >;
  return calls
    .filter(([cmd, a]) => cmd === "rpc_call" && a?.method === "worker_budget")
    .map(([, a]) => a?.params ?? {});
}

describe("<Settings /> — the worker budget is computed for the loaded library", () => {
  it("sends the loaded library path with every worker_budget call", async () => {
    setEngine("essentia_tf", "discogs");
    useLibraryStore.setState({ libraryPath: "D:/Music/Sets" });
    render(<Settings />);

    await waitFor(() => {
      const params = workerBudgetParams();
      expect(params.length).toBeGreaterThan(0);
      expect(params.every((p) => p.library_path === "D:/Music/Sets")).toBe(true);
    });
  });

  it("omits the key entirely when no library is loaded", async () => {
    setEngine("essentia_tf", "discogs");
    useLibraryStore.setState({ libraryPath: null });
    render(<Settings />);

    await waitFor(() => {
      const params = workerBudgetParams();
      expect(params.length).toBeGreaterThan(0);
      // Absent, not null — the backend reads it with `params.get(...)` and a
      // null would have to be special-cased on the far side.
      expect(params.every((p) => !("library_path" in p))).toBe(true);
    });
  });
});

describe("<Settings /> — verify-models failure points at Download models (WP-C1)", () => {
  it("links the failure toast to the Download models button", async () => {
    setEngine("essentia_tf", "discogs");
    useNotificationStore.setState({ items: [] });
    mockSidecarWith({
      worker_budget: null,
      verify_models: {
        results: [
          { name: "genre_discogs", suffix: "pb", ok: false, reason: "sha256 mismatch" },
        ],
      },
    });
    render(<Settings />);

    const btn = await screen.findByRole("button", { name: /Verify model integrity/i });
    fireEvent.click(btn);

    await waitFor(() => {
      const items = useNotificationStore.getState().items;
      const t = items.find((n) => /failed verification/i.test(n.message));
      expect(t).toBeTruthy();
      // The detail names the section, and an inline action re-fetches directly.
      expect(t?.detail).toMatch(/Download models now/i);
      expect(t?.detail).toMatch(/Analysis section/i);
      expect(t?.action?.label).toBe("Download models");
    });
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

// ---------------------------------------------------------------------------
// F027 — the two vocal-band sliders overlap on [0.5, 0.95] and the Hint tells
// the user to raise the first one. An inverted pair is persisted to disk and
// then HARD-REJECTS every subsequent tag write (genre, energy, mood, BPM, key),
// with a message naming two identifiers this screen never shows.
// ---------------------------------------------------------------------------

describe("orderedVocalBands", () => {
  it("pushes the vocal cutoff up when the instrumental one is dragged past it", () => {
    // The exact drag the Hint invites: "Instrumental ≤" to 90%, default 88%.
    expect(orderedVocalBands(0.9, 0.88, "instrumental")).toEqual({
      vocal_instrumental_max: 0.9,
      vocal_full_min: 0.91,
    });
  });

  it("pulls the instrumental cutoff down when the vocal one is dragged under it", () => {
    expect(orderedVocalBands(0.72, 0.7, "vocal")).toEqual({
      vocal_instrumental_max: 0.69,
      vocal_full_min: 0.7,
    });
  });

  it("separates an exactly-equal pair (the backend rejects >= too)", () => {
    const r = orderedVocalBands(0.8, 0.8, "instrumental");
    expect(r.vocal_instrumental_max).toBeLessThan(r.vocal_full_min);
  });

  it("leaves an already-ordered pair alone", () => {
    expect(orderedVocalBands(0.72, 0.88, "instrumental")).toEqual({
      vocal_instrumental_max: 0.72,
      vocal_full_min: 0.88,
    });
  });

  it("stays ordered at the sliders' ceiling", () => {
    const r = orderedVocalBands(0.95, 0.95, "instrumental");
    expect(r.vocal_full_min).toBeLessThanOrEqual(1);
    expect(r.vocal_instrumental_max).toBeLessThan(r.vocal_full_min);
  });
});

describe("<Settings /> — download-models toast action respects the busy state", () => {
  it("refuses to start a download while another long op is running", async () => {
    setEngine("essentia_tf", "discogs");
    useNotificationStore.setState({ items: [] });
    useOperationStore.setState({ active: null, opId: null, progress: null });
    mockSidecarWith({
      worker_budget: null,
      verify_models: {
        results: [
          { name: "genre_discogs", suffix: "pb", ok: false, reason: "sha256 mismatch" },
        ],
      },
    });
    render(<Settings />);
    fireEvent.click(await screen.findByRole("button", { name: /Verify model integrity/i }));

    const action = await waitFor(() => {
      const t = useNotificationStore
        .getState()
        .items.find((n) => /failed verification/i.test(n.message));
      expect(t?.action).toBeTruthy();
      return t!.action!;
    });

    // The in-page button is gated on , but this toast action is not —
    // and a toast outlives the click that raised it.
    // Both of these re-render the MOUNTED Settings page (it subscribes to the
    // operation store), so they belong inside act() — unwrapped they printed a
    // React act() warning on every run, which is how a later, real one gets
    // missed.
    let runningOpId = "";
    await act(async () => {
      runningOpId = useOperationStore.getState().begin("analyze");
      action.onClick();
    });

    await waitFor(() => {
      const msgs = useNotificationStore.getState().items.map((n) => n.message);
      expect(msgs.some((m) => /another operation is running/i.test(m))).toBe(true);
    });
    // The running analyze still owns the global op state.
    expect(useOperationStore.getState().active).toBe("analyze");
    expect(useOperationStore.getState().opId).toBe(runningOpId);
    const calls = (invoke as ReturnType<typeof vi.fn>).mock.calls.filter(
      (c) => (c[1] as { method?: string })?.method === "download_models",
    );
    expect(calls).toHaveLength(0);
  });
});

describe("<Settings /> — vocal sensitivity sliders", () => {
  beforeEach(() => {
    mockSidecar();
    useConfigStore.getState().updateTagging({
      vocal_instrumental_max: 0.72,
      vocal_full_min: 0.88,
    });
  });

  it("never persists an inverted pair when the user raises 'Instrumental ≤'", async () => {
    render(<Settings />);
    // The Tagging section lives behind the advanced disclosure.
    fireEvent.click(screen.getByRole("button", { name: /advanced settings/i }));

    const sliders = screen
      .getAllByRole("slider")
      .filter((el) => (el as HTMLInputElement).max === "0.95");
    // The instrumental cutoff is the only 0.3–0.95 range on the page.
    expect(sliders).toHaveLength(1);
    fireEvent.change(sliders[0], { target: { value: "0.9" } });

    await waitFor(() => {
      const t = useConfigStore.getState().config.tagging;
      expect(t.vocal_instrumental_max).toBe(0.9);
      // The invariant vibechek/rpc.py::_apply_ml_tags enforces.
      expect(t.vocal_instrumental_max).toBeLessThan(t.vocal_full_min);
    });
  });
});

// ---------------------------------------------------------------------------
// F029 — merely OPENING Settings must not write the config while the load is
// untrusted. The worker slider seeds itself from system_info on mount, and the
// persistence hook arms its autosave on the first config change it sees: it
// cannot tell that automatic seed apart from a deliberate edit, so seeding
// here wrote DEFAULT_CONFIG over a config.json that is very likely intact.
// ---------------------------------------------------------------------------

describe("<Settings /> — worker auto-seed respects an untrusted config load", () => {
  function mount(loadUntrusted: boolean) {
    const base = useConfigStore.getState().config;
    useConfigStore.setState({
      config: { ...base, analysis: { ...base.analysis, workers: 0 } },
      loaded: true,
      loadUntrusted,
    });
    return render(<Settings />);
  }

  it("seeds the recommended worker count after a trusted load", async () => {
    mount(false);

    await waitFor(() => {
      expect(useConfigStore.getState().config.analysis.workers).toBe(7);
    });
  });

  it("leaves the config untouched while the load is untrusted", async () => {
    mount(true);

    // Give the system_info promise (and the debounce it would have armed)
    // room to land.
    await waitFor(() => {
      expect(screen.getByRole("button", { name: /advanced settings/i })).toBeInTheDocument();
    });
    await new Promise((r) => setTimeout(r, 50));
    expect(useConfigStore.getState().config.analysis.workers).toBe(0);
  });
});

// ---------------------------------------------------------------------------
// F029 fallout — `loadUntrusted` gates the first-launch tour (App.tsx) and the
// worker auto-seed above. `restore_default_config` quarantines the unreadable
// file and writes a fresh one, so once it returns the config on screen IS the
// file on disk. useConfigPersistence's toast action clears the flag; this page
// calls the identical RPC and used to leave it set for the rest of the session.
// ---------------------------------------------------------------------------

describe("<Settings /> — Restore all defaults clears the untrusted verdict", () => {
  it("marks the config trustworthy again after the restore lands", async () => {
    const base = useConfigStore.getState().config;
    const restored = { ...base, analysis: { ...base.analysis, workers: 0 } };
    (invoke as ReturnType<typeof vi.fn>).mockImplementation(
      async (cmd: string, args?: { method?: string }) => {
        if (cmd === "sidecar_status") return { binary: "vibechek" };
        if (cmd !== "rpc_call") return {};
        switch (args?.method) {
          case "restore_default_config":
            return { saved_to: "C:/cfg.json", config: restored };
          case "preflight":
            return preflightResult();
          case "list_profiles":
            return { profiles: [] };
          default:
            return {};
        }
      },
    );
    // config.json was unreadable at launch: defaults on screen, flag set.
    useConfigStore.setState({ config: restored, loaded: true, loadUntrusted: true });

    render(<Settings />);
    fireEvent.click(
      await screen.findByRole("button", { name: /restore all settings to defaults/i }),
    );
    fireEvent.click(await screen.findByRole("button", { name: /^restore defaults$/i }));

    await waitFor(() => expect(useConfigStore.getState().loadUntrusted).toBe(false));
  });
});
