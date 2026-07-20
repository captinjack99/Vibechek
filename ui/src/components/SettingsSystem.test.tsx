/**
 * WP7: the "Ready to analyze?" banner must not paint a green Essentia row for a
 * build that can't actually serve the engine it's evaluating. The bundled
 * DSP-only Windows wheel imports fine (`installed=true`) but can't run
 * essentia_tf/onnx in-process, so the row is gated on `essentia_usable`.
 */

import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";

import { CrossVendorGpuInventory, PreflightSection } from "./SettingsSystem";
import type { PreflightResult, SystemResources } from "../types";

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
    expect(screen.getByText(/^installed \(2\.1\)$/i)).toBeInTheDocument();
    // The honest "can't run the analysis engine" caption must NOT appear when usable.
    expect(screen.queryByText(/can't run the analysis engine/i)).not.toBeInTheDocument();
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
    expect(screen.getByText(/can't run the analysis engine here/i)).toBeInTheDocument();
    expect(
      screen.queryByText(/^installed \(2\.1-dsp\)$/i),
    ).not.toBeInTheDocument();
  });

  it("shows 'not installed in the Linux analysis environment' when essentia is absent on Windows", () => {
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
      screen.getByText(/not installed in the Linux analysis environment yet/i),
    ).toBeInTheDocument();
  });
});

/**
 * The cross-vendor GPU explainer must describe the GPU story of the SELECTED
 * engine — the pre-0.6 copy hardcoded "essentia-tensorflow / CUDA-only", which
 * is flat wrong for the ONNX engine (NVIDIA today, cross-vendor planned) and the
 * Windows-default native engine (CPU-only for every vendor, NVIDIA included).
 */
function makeSysInfoWithUnsupportedGpu(): SystemResources {
  return {
    platform: "win32",
    cpu_count: 8,
    memory_total_mb: 16384,
    memory_available_mb: 8192,
    gpu_available: false,
    gpu_devices: [
      {
        name: "AMD Radeon RX 7900",
        backend: "amd",
        memory_mb: 20480,
        vendor: "amd",
        device_kind: "discrete",
        accelerated_by_vibechek: false,
        unsupported_reason: "AMD GPUs need ROCm/DirectML.",
      },
    ],
    cuda_runtime: null,
    accelerated_gpu_count: 0,
    unsupported_gpu_count: 1,
    recommended_workers: 7,
  } as unknown as SystemResources;
}

describe("<CrossVendorGpuInventory /> — engine-aware GPU story", () => {
  function expand() {
    fireEvent.click(screen.getByText(/why is my amd\/intel gpu not used/i));
  }

  it("essentia_tf: callout says NVIDIA-only; explainer names essentia-tensorflow", () => {
    const { container } = render(
      <CrossVendorGpuInventory sysInfo={makeSysInfoWithUnsupportedGpu()} engine="essentia_tf" />,
    );
    expect(container.textContent).toContain("can only use NVIDIA GPUs");
    expand();
    expect(container.textContent).toContain("essentia-tensorflow");
    expect(container.textContent).toContain("TensorFlow 2.5");
  });

  it("onnx: callout says NVIDIA accelerated today, cross-vendor planned (not 'only NVIDIA')", () => {
    const { container } = render(
      <CrossVendorGpuInventory sysInfo={makeSysInfoWithUnsupportedGpu()} engine="onnx" />,
    );
    expect(container.textContent).toContain("accelerates NVIDIA GPUs today");
    expect(container.textContent).not.toContain("can only use NVIDIA GPUs");
    expand();
    expect(container.textContent).toContain("execution providers are wired");
    // No longer claims all analysis runs through essentia-tensorflow.
    expect(container.textContent).not.toContain("essentia-tensorflow");
  });

  it("native: callout must NOT imply NVIDIA works — CPU today, GPU planned", () => {
    const { container } = render(
      <CrossVendorGpuInventory sysInfo={makeSysInfoWithUnsupportedGpu()} engine="native" />,
    );
    expect(container.textContent).toContain("runs on CPU today — GPU support is planned");
    expect(container.textContent).not.toContain("can only use NVIDIA GPUs");
    expand();
    // Honest about NVIDIA being unused on the native engine too.
    expect(container.textContent).toContain("NVIDIA included");
  });

  it("worker guidance points at the auto-capped slider, not 'bump the worker count'", () => {
    const { container } = render(
      <CrossVendorGpuInventory sysInfo={makeSysInfoWithUnsupportedGpu()} engine="essentia_tf" />,
    );
    expand();
    expect(container.textContent).toContain("workers slider in the Analysis section above is already capped");
    expect(container.textContent).not.toContain("Bump the worker count");
  });

  it("renders nothing when no GPUs are detected", () => {
    const sys = makeSysInfoWithUnsupportedGpu();
    const { container } = render(
      <CrossVendorGpuInventory
        sysInfo={{ ...sys, gpu_devices: [], unsupported_gpu_count: 0 } as SystemResources}
        engine="native"
      />,
    );
    expect(container.textContent).toBe("");
  });
});
