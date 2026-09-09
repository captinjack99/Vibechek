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
import { act, render, screen, waitFor } from "@testing-library/react";
import { listen } from "@tauri-apps/api/event";
import { invoke } from "@tauri-apps/api/core";
import { open as openPath } from "@tauri-apps/plugin-shell";

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

/**
 * "Open install folder" used to swallow every rejection from the shell opener
 * (`.catch(() => {})`), so a click that couldn't open anything — no registered
 * file manager, a path that has since moved, a sandbox refusal — looked exactly
 * like a click that worked. The user's only remaining next step silently did
 * nothing, twice.
 */
describe("<App /> — the install-folder action reports its own failures", () => {
  beforeEach(() => {
    const validConfig = useConfigStore.getState().config;
    (invoke as ReturnType<typeof vi.fn>).mockImplementation(
      async (_cmd: string, args: { method?: string }) => {
        if (args?.method === "get_config") return validConfig;
        if (args?.method === "library_state") return { recent: [], active: null };
        return {};
      },
    );
  });

  /** Emit the risky-install-path warning and return its action button. */
  async function installWarningAction() {
    render(<App />);
    await waitFor(() => {
      expect(captureListener("sidecar:notify")).toBeTypeOf("function");
    });
    await act(async () => {
      captureListener("sidecar:notify")!({
        payload: {
          level: "warning",
          message: "Vibechek is installed in a location that may cause launch issues.",
          path: "C:/Users/dj/My Drive/Vibechek/vibechek.exe",
        },
      });
    });
    await waitFor(() => {
      expect(
        useNotificationStore.getState().items.some((n) => n.action),
      ).toBe(true);
    });
    return useNotificationStore.getState().items.find((n) => n.action)!.action!;
  }

  it("surfaces a toast carrying the opener's rejection message", async () => {
    (openPath as ReturnType<typeof vi.fn>).mockRejectedValue(
      new Error("no application is registered to open this path"),
    );

    const action = await installWarningAction();
    await act(async () => {
      action.onClick();
    });

    await waitFor(() => {
      const failure = useNotificationStore
        .getState()
        .items.find((n) => n.message === "Couldn't open the folder");
      expect(failure).toBeTruthy();
      expect(failure!.kind).toBe("warning");
      // Both the path it tried and the reason it failed — a bare "couldn't
      // open" gives the user nothing to act on.
      expect(failure!.detail).toMatch(/no application is registered/);
      expect(failure!.detail).toMatch(/My Drive\/Vibechek/);
    });
  });

  it("stays quiet when the folder opens", async () => {
    (openPath as ReturnType<typeof vi.fn>).mockResolvedValue(undefined);

    const action = await installWarningAction();
    const before = useNotificationStore.getState().items.length;
    await act(async () => {
      action.onClick();
    });

    expect(useNotificationStore.getState().items.length).toBe(before);
  });
});

// ---------------------------------------------------------------------------
// The first-launch tour must not run on an UNTRUSTED config load.
//
// When config.json exists but couldn't be read, the sidecar answers with
// defaults and `load_failed: true`. Those defaults carry seen_onboarding:false,
// so the full-screen, unavoidable tour appears for an already-onboarded user —
// and both its exits (Skip / "Start using Vibechek") write seen_onboarding,
// arming the autosave that then replaces the intact config.json with defaults.
// The user changed no setting; they dismissed an overlay they could not avoid.
// ---------------------------------------------------------------------------

describe("<App /> — first-launch tour needs a config load we trust", () => {
  function mountWith(extra: Record<string, unknown>) {
    useConfigStore.setState({ loaded: false, loadUntrusted: false });
    const base = useConfigStore.getState().config;
    const cfg = { ...base, ui: { ...base.ui, seen_onboarding: false } };
    (invoke as ReturnType<typeof vi.fn>).mockImplementation(
      async (_cmd: string, args: { method?: string }) => {
        if (args?.method === "get_config") return { ...cfg, ...extra };
        if (args?.method === "library_state") return { recent: [], active: null };
        return {};
      },
    );
    return render(<App />);
  }

  it("shows the tour after a trusted load", async () => {
    mountWith({});

    expect(await screen.findByRole("button", { name: /^skip$/i })).toBeInTheDocument();
  });

  it("stays out of the way when the load came back flagged load_failed", async () => {
    mountWith({ load_failed: true });

    await waitFor(() => expect(useConfigStore.getState().loadUntrusted).toBe(true));
    expect(screen.queryByRole("button", { name: /^skip$/i })).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /start using vibechek/i }),
    ).not.toBeInTheDocument();
  });
});
