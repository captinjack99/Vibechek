/**
 * Regression test for "Sidecar startup 'risky install path' warning
 * (sidecar:notify) is emitted but never displayed — no frontend listener".
 *
 * The Python sidecar emits a `notify` JSON-RPC notification at startup for
 * known-problematic install locations (Google Drive "My Drive", OneDrive,
 * iCloud, Dropbox, very long / space-heavy paths). The Rust shell re-emits it
 * as the Tauri event `sidecar:notify`. App.tsx had no listener for it, so the
 * warning was silently dropped.
 *
 * This asserts that App registers a `sidecar:notify` listener and that an
 * incoming notify event surfaces a notification via the notification store.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, waitFor } from "@testing-library/react";
import { listen } from "@tauri-apps/api/event";
import { invoke } from "@tauri-apps/api/core";

import App from "./App";
import { useConfigStore, useNotificationStore } from "./stores";

/**
 * Find the callback App registered for a given `sidecar:<name>` event by
 * inspecting the mocked `listen()` calls.
 */
function captureListener(eventName: string): ((evt: { payload: unknown }) => void) | undefined {
  const call = (listen as ReturnType<typeof vi.fn>).mock.calls.find(
    ([name]) => name === eventName,
  );
  return call?.[1] as ((evt: { payload: unknown }) => void) | undefined;
}

describe("<App /> — sidecar:notify install-path warning", () => {
  beforeEach(() => {
    // App mounts useConfigPersistence, which fires get_config on start. The
    // default invoke mock returns {}, which would replace `config` with an
    // object lacking `ui` (App reads config.ui.seen_onboarding). Return a
    // valid config (the store's defaults) so the render is stable.
    const validConfig = useConfigStore.getState().config;
    (invoke as ReturnType<typeof vi.fn>).mockImplementation(
      async (_cmd: string, args: { method?: string }) => {
        if (args?.method === "get_config") return validConfig;
        if (args?.method === "library_state") return { recent: [], active: null };
        return {};
      },
    );
  });

  it("registers a sidecar:notify listener and surfaces the message", async () => {
    render(<App />);

    // App mounts several useSidecarEvent/useSidecarProgress hooks; the listen()
    // registrations are async (inside the effect's promise), so wait until the
    // notify registration appears.
    await waitFor(() => {
      expect(captureListener("sidecar:notify")).toBeTypeOf("function");
    });

    const handler = captureListener("sidecar:notify")!;
    handler({
      payload: {
        level: "warning",
        message: "Vibechek is installed under Google Drive (My Drive).",
        detail: "This can make the app hang on launch — reinstall to a simple path.",
      },
    });

    await waitFor(() => {
      const items = useNotificationStore.getState().items;
      expect(items.map((n) => n.message)).toContain(
        "Vibechek is installed under Google Drive (My Drive).",
      );
    });
    const item = useNotificationStore
      .getState()
      .items.find((n) => n.message.includes("Google Drive"))!;
    // A sidecar warning renders as the amber "warning" kind (distinct from
    // cheerful info AND from the sticky red operation-error toast).
    expect(item.kind).toBe("warning");
    expect(item.detail).toMatch(/hang on launch/);
  });

  it("ignores a notify event with no message", async () => {
    render(<App />);
    await waitFor(() => {
      expect(captureListener("sidecar:notify")).toBeTypeOf("function");
    });
    const before = useNotificationStore.getState().items.length;
    captureListener("sidecar:notify")!({ payload: { level: "warning" } });
    // No message → no notification added.
    expect(useNotificationStore.getState().items.length).toBe(before);
  });

  it("makes the install-path warning persistent with an Open install folder action", async () => {
    render(<App />);
    await waitFor(() => {
      expect(captureListener("sidecar:notify")).toBeTypeOf("function");
    });
    captureListener("sidecar:notify")!({
      payload: {
        level: "warning",
        message: "Vibechek is installed in a location that may cause launch issues.",
        detail: "Install path contains 'my drive'.",
        path: "C:/Users/dj/My Drive/Vibechek/vibechek.exe",
      },
    });

    await waitFor(() => {
      expect(
        useNotificationStore
          .getState()
          .items.some((n) => n.message.includes("launch issues")),
      ).toBe(true);
    });
    const item = useNotificationStore
      .getState()
      .items.find((n) => n.message.includes("launch issues"))!;
    // App-breaking → must not auto-dismiss, and must offer an in-view next step.
    expect(item.persistent).toBe(true);
    expect(item.action?.label).toBe("Open install folder");
  });
});
