/**
 * The shared memory-refusal recovery buttons (used by BOTH the ErrorToast and
 * Settings' worker-slider refusal). Each button renders only when its capability
 * flag is set; the switch flips the classifier through the config store; the
 * increase-memory action fires `increase_wsl_memory` and — on a real bump —
 * raises a PERSISTENT notice and NEVER restarts anything.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

import { MemoryRefusalActions } from "./MemoryRefusalActions";
import { useConfigStore, useNotificationStore } from "../stores";
import { increaseWslMemory } from "../api/rpc";

// Mock at the api layer so no real RPC is issued.
vi.mock("../api/rpc", () => ({
  increaseWslMemory: vi.fn(),
}));

const mockIncrease = increaseWslMemory as unknown as ReturnType<typeof vi.fn>;

beforeEach(() => {
  useNotificationStore.setState({ items: [] });
  // Start on CLAP so "switch to the standard model" has somewhere to go.
  useConfigStore.getState().updateAnalysis({ genre_classifier: "clap" });
  mockIncrease.mockReset();
});

describe("<MemoryRefusalActions />", () => {
  it("renders nothing when neither capability is offered", () => {
    const { container } = render(<MemoryRefusalActions />);
    expect(container).toBeEmptyDOMElement();
  });

  it("renders each button only when its flag is set", () => {
    render(<MemoryRefusalActions canSwitchClassifier canIncreaseMemory />);
    expect(screen.getByText("Switch to the standard genre model")).toBeInTheDocument();
    expect(screen.getByText("Give Vibechek more memory")).toBeInTheDocument();
  });

  it("omits the increase-memory button when only the classifier switch is offered", () => {
    render(<MemoryRefusalActions canSwitchClassifier />);
    expect(screen.getByText("Switch to the standard genre model")).toBeInTheDocument();
    expect(screen.queryByText("Give Vibechek more memory")).not.toBeInTheDocument();
  });

  it("switches the classifier to discogs through the config store (persisted by autosave)", () => {
    render(<MemoryRefusalActions canSwitchClassifier />);
    fireEvent.click(screen.getByText("Switch to the standard genre model"));
    expect(useConfigStore.getState().config.analysis.genre_classifier).toBe("discogs");
    const items = useNotificationStore.getState().items;
    expect(items.some((n) => /standard genre model/i.test(n.message))).toBe(true);
    // We never invoke the memory RPC for a classifier switch.
    expect(mockIncrease).not.toHaveBeenCalled();
  });

  it("raises a PERSISTENT restart notice on a real memory bump (never auto-restarts)", async () => {
    mockIncrease.mockResolvedValue({
      ok: true, changed: true, old: "8GB", new: "24GB", restart_required: true,
    });
    render(<MemoryRefusalActions canIncreaseMemory />);
    fireEvent.click(screen.getByText("Give Vibechek more memory"));

    await waitFor(() => {
      expect(mockIncrease).toHaveBeenCalledTimes(1);
      const items = useNotificationStore.getState().items;
      expect(items.length).toBe(1);
      expect(items[0].persistent).toBe(true);
      expect(items[0].kind).toBe("warning");
      expect(items[0].message).toMatch(/from 8GB to 24GB/);
      expect(items[0].detail).toMatch(/Restart Windows' Linux environment/);
    });
  });

  it("reports a non-persistent info toast when the limit was already high enough", async () => {
    mockIncrease.mockResolvedValue({
      ok: true, changed: false, restart_required: false,
      message: "The Linux analysis environment already has enough memory.",
    });
    render(<MemoryRefusalActions canIncreaseMemory />);
    fireEvent.click(screen.getByText("Give Vibechek more memory"));

    await waitFor(() => {
      const items = useNotificationStore.getState().items;
      expect(items.length).toBe(1);
      expect(items[0].persistent).toBeFalsy();
      expect(items[0].kind).toBe("info");
      expect(items[0].message).toMatch(/already has enough memory/);
    });
  });

  it("surfaces the backend headline (not a raw error) when the bump fails", async () => {
    mockIncrease.mockResolvedValue({
      ok: false, changed: false, restart_required: false,
      headline: "Couldn't work out a safe memory limit for this PC.",
      detail: "could not measure host RAM",
    });
    render(<MemoryRefusalActions canIncreaseMemory />);
    fireEvent.click(screen.getByText("Give Vibechek more memory"));

    await waitFor(() => {
      const items = useNotificationStore.getState().items;
      expect(items.length).toBe(1);
      expect(items[0].message).toMatch(/safe memory limit/);
      expect(items[0].persistent).toBeFalsy();
    });
  });
});
