/**
 * WP7 #2: get_config attaches `config_warnings` when the loader snapped an
 * invalid/cross-platform saved value back to a default; the load path shows a
 * one-time toast so the reverted default doesn't masquerade as the user's own
 * choice.
 *
 * WP9 #19: a change made inside the 500ms autosave debounce must not be lost on
 * quit — flush the pending save on window teardown.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, waitFor } from "@testing-library/react";
import { invoke } from "@tauri-apps/api/core";

import { useConfigPersistence } from "./useConfigPersistence";
import { useConfigStore, useNotificationStore } from "../stores";

function Harness() {
  useConfigPersistence();
  return null;
}

const CFG = { analysis: {}, tagging: {}, duplicates: {}, organization: {}, ui: {} };

beforeEach(() => {
  useConfigStore.setState({ config: CFG as never, loaded: false });
  useNotificationStore.setState({ items: [] } as never);
});

describe("useConfigPersistence — config-warnings toast", () => {
  it("shows a one-time toast when get_config returns config_warnings", async () => {
    (invoke as ReturnType<typeof vi.fn>).mockImplementation(
      async (cmd: string, args?: { method?: string }) => {
        if (cmd === "rpc_call" && args?.method === "get_config") {
          return {
            ...CFG,
            config_warnings: ["analysis.inference_engine was 'TF' — reset to the default"],
          };
        }
        return {};
      },
    );

    render(<Harness />);

    await waitFor(() => {
      const items = useNotificationStore.getState().items;
      expect(items.some((i) => /invalid and were reset/i.test(i.message))).toBe(true);
    });
    // The transport-only field must be stripped, never stored/round-tripped.
    expect(
      (useConfigStore.getState().config as unknown as Record<string, unknown>).config_warnings,
    ).toBeUndefined();
  });

  it("shows no toast when get_config returns no config_warnings", async () => {
    (invoke as ReturnType<typeof vi.fn>).mockImplementation(
      async (cmd: string, args?: { method?: string }) => {
        if (cmd === "rpc_call" && args?.method === "get_config") return { ...CFG };
        return {};
      },
    );

    render(<Harness />);

    await waitFor(() => expect(useConfigStore.getState().loaded).toBe(true));
    expect(useNotificationStore.getState().items.length).toBe(0);
  });
});

describe("useConfigPersistence — flush pending save on teardown", () => {
  it("flushes a still-debounced save on beforeunload (last change not lost on quit)", async () => {
    const saved: Array<{ config: { analysis: { workers?: number } } }> = [];
    (invoke as ReturnType<typeof vi.fn>).mockImplementation(
      async (cmd: string, args?: { method?: string; params?: unknown }) => {
        if (cmd === "rpc_call" && args?.method === "get_config") return { ...CFG };
        if (cmd === "rpc_call" && args?.method === "save_config") {
          saved.push(args.params as { config: { analysis: { workers?: number } } });
        }
        return {};
      },
    );

    render(<Harness />);
    await waitFor(() => expect(useConfigStore.getState().loaded).toBe(true));
    saved.length = 0; // ignore the save the initial load schedules

    // Change a setting; the 500ms debounce hasn't fired yet.
    act(() => {
      useConfigStore.getState().updateAnalysis({ workers: 7 });
    });

    // Window teardown must flush the pending save synchronously.
    fireEvent(window, new Event("beforeunload"));

    expect(saved.length).toBeGreaterThan(0);
    expect(saved[saved.length - 1].config.analysis.workers).toBe(7);
  });
});
