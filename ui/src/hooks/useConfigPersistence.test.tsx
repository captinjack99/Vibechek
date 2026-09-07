/**
 * WP7 #2: get_config attaches `config_warnings` when the loader snapped an
 * invalid/cross-platform saved value back to a default; the load path shows a
 * one-time toast so the reverted default doesn't masquerade as the user's own
 * choice.
 *
 * WP9 #19: a change made inside the 500ms autosave debounce must not be lost on
 * quit — flush the pending save on window teardown.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
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
  useConfigStore.setState({ config: CFG as never, loaded: false, loadUntrusted: false });
  useNotificationStore.setState({ items: [] } as never);
  // Fake timers, but AUTO-ADVANCING. This suite's negative assertions ("no save
  // landed") used to sleep 700ms of real wall clock per test, which makes them
  // pass more readily the slower the machine is — the wrong way round for an
  // assertion that something did NOT happen — and cost 1.4s a run. With the
  // clock faked, `advance()` jumps the 500ms debounce (and the post-refusal
  // re-probe) deterministically instead.
  //
  // `shouldAdvanceTime` is required, not decorative: @testing-library/dom's
  // waitFor only knows how to pump JEST's fake clock (helpers.js literally
  // checks `typeof jest`), so a fully frozen vitest clock hangs every waitFor
  // in this file.
  vi.useFakeTimers({ shouldAdvanceTime: true });
});

afterEach(() => {
  vi.useRealTimers();
});

/** Jump the fake clock `ms` forward and let the promises it releases settle. */
async function advance(ms: number) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

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

/**
 * Regression test for the audit finding "A failed/fallback get_config
 * overwrites the user's on-disk settings with defaults 500ms later".
 *
 * The load `.catch` used to mark the store `loaded`, which is what arms the
 * debounced autosave — so a rejected get_config (a >60s cold sidecar start) or
 * a payload the backend flagged as a defaults-fallback wrote DEFAULT_CONFIG
 * straight over config.json, silently, on the next launch.
 */
describe("useConfigPersistence — a load we can't trust must not write defaults back", () => {
  function mockLoad(
    getConfig: () => Promise<unknown>,
    saved: Array<{ config: unknown }>,
  ) {
    (invoke as ReturnType<typeof vi.fn>).mockImplementation(
      async (cmd: string, args?: { method?: string; params?: unknown }) => {
        if (cmd === "rpc_call" && args?.method === "get_config") return getConfig();
        if (cmd === "rpc_call" && args?.method === "save_config") {
          saved.push(args.params as { config: unknown });
        }
        return {};
      },
    );
  }

  it("saves nothing when get_config rejects — and says so out loud", async () => {
    const saved: Array<{ config: unknown }> = [];
    mockLoad(() => Promise.reject(new Error("sidecar call 'get_config' timed out after 60s")), saved);

    render(<Harness />);

    // Fail loud: the user is told their settings weren't read.
    await waitFor(() => {
      const items = useNotificationStore.getState().items;
      expect(items.some((i) => /couldn't load your saved settings/i.test(i.message))).toBe(true);
    });
    expect(useConfigStore.getState().loaded).toBe(false);

    // Nothing is pending (a teardown flush would fire it), and nothing lands
    // once the 500ms debounce window has gone by either.
    fireEvent(window, new Event("beforeunload"));
    await advance(700); // well past the 500ms autosave debounce
    expect(saved).toEqual([]);
  });

  it("arms the save only once the user deliberately changes a setting", async () => {
    const saved: Array<{ config: { analysis?: { workers?: number } } }> = [];
    mockLoad(() => Promise.reject(new Error("boom")), saved);

    render(<Harness />);
    await waitFor(() => expect(useNotificationStore.getState().items.length).toBeGreaterThan(0));
    expect(saved).toEqual([]);

    // An explicit edit IS intent to write — from here on saving is what the
    // user asked for.
    act(() => {
      useConfigStore.getState().updateAnalysis({ workers: 7 });
    });
    fireEvent(window, new Event("beforeunload")); // flush the pending debounce

    expect(saved.length).toBe(1);
    expect(saved[0].config.analysis?.workers).toBe(7);
  });

  it("treats a get_config payload flagged load_failed as untrusted defaults", async () => {
    const saved: Array<{ config: unknown }> = [];
    mockLoad(async () => ({ ...CFG, load_failed: true }), saved);

    render(<Harness />);

    await waitFor(() => {
      const items = useNotificationStore.getState().items;
      expect(items.some((i) => /couldn't be read/i.test(i.message))).toBe(true);
    });
    // The RPC succeeded, so the app is usable — but the config on screen isn't
    // the user's, so nothing may be written back over the real file.
    expect(useConfigStore.getState().loaded).toBe(true);
    fireEvent(window, new Event("beforeunload"));
    await advance(700); // well past the 500ms autosave debounce
    expect(saved).toEqual([]);
    // The transport-only flag must never be stored as a config field.
    expect(
      (useConfigStore.getState().config as unknown as Record<string, unknown>).load_failed,
    ).toBeUndefined();
  });

  // F029 — the refs above only gate THIS hook's autosave. Other views must not
  // nudge the user into an action that writes the config either, so the verdict
  // has to be readable from the store.
  it("publishes the untrusted verdict to the store on a rejected load", async () => {
    mockLoad(() => Promise.reject(new Error("boom")), []);

    render(<Harness />);

    await waitFor(() => expect(useConfigStore.getState().loadUntrusted).toBe(true));
  });

  it("publishes it for a load_failed payload too", async () => {
    mockLoad(async () => ({ ...CFG, load_failed: true }), []);

    render(<Harness />);

    await waitFor(() => expect(useConfigStore.getState().loadUntrusted).toBe(true));
  });

  it("leaves a good load trusted", async () => {
    mockLoad(async () => ({ ...CFG }), []);

    render(<Harness />);

    await waitFor(() => expect(useConfigStore.getState().loaded).toBe(true));
    expect(useConfigStore.getState().loadUntrusted).toBe(false);
  });
});

/**
 * `save_config` REFUSES (INVALID_PARAMS) rather than overwrite a settings file
 * it could not read — see `vibechek/rpc.py::_save_config`. That refusal used to
 * land in the generic throttled "Settings could not save: <jargon>" toast,
 * which offers no way out: every later autosave is refused identically, and the
 * one path that clears it (restore_default_config, which quarantines the
 * unreadable bytes as `<name>.corrupt-<timestamp>` before writing) was three
 * clicks away in Settings with nothing pointing at it.
 */
describe("useConfigPersistence — a refused save names the file and offers the way out", () => {
  const REFUSAL_MESSAGE =
    "The saved settings file could not be read; refusing to overwrite it. " +
    "Fix or remove C:\\Users\\dj\\AppData\\Roaming\\Vibechek\\Vibechek\\config.json first.";
  // The Rust shell rejects with the stringified envelope; RpcError parses it.
  const REFUSAL = JSON.stringify({ message: REFUSAL_MESSAGE, code: -32602 });

  function mockRefusingSave(opts: { restored?: object; stillUnreadable?: boolean } = {}) {
    const calls: string[] = [];
    let saveFails = true;
    let getConfigCalls = 0;
    (invoke as ReturnType<typeof vi.fn>).mockImplementation(
      async (cmd: string, args?: { method?: string }) => {
        if (cmd !== "rpc_call") return {};
        calls.push(args!.method!);
        if (args?.method === "get_config") {
          getConfigCalls += 1;
          // Call 1 is the mount load (clean, so the autosave arms at all).
          // Every later call is the hook RE-PROBING the file after a refused
          // save: that is where a test stages "still unreadable".
          return getConfigCalls > 1 && opts.stillUnreadable
            ? { ...CFG, load_failed: true }
            : { ...CFG };
        }
        if (args?.method === "save_config") {
          if (saveFails) throw REFUSAL;
          return { saved_to: "C:/cfg.json" };
        }
        if (args?.method === "restore_default_config") {
          saveFails = false; // the file is writable again
          return { saved_to: "C:/cfg.json", config: opts.restored ?? { ...CFG } };
        }
        return {};
      },
    );
    return calls;
  }

  async function armAndSave() {
    render(<Harness />);
    await waitFor(() => expect(useConfigStore.getState().loaded).toBe(true));
    act(() => {
      useConfigStore.getState().updateAnalysis({ workers: 7 });
    });
    fireEvent(window, new Event("beforeunload")); // flush the pending debounce
    // The FIRST refusal is not believed — the hook re-probes the file (and
    // retries the save once) before it puts anything on screen. Get past both.
    await advance(1000);
  }

  it("shows the sidecar's message verbatim with a Restore defaults action", async () => {
    mockRefusingSave();
    await armAndSave();

    await waitFor(() => expect(useNotificationStore.getState().items.length).toBe(1));
    const toast = useNotificationStore.getState().items[0];
    // Verbatim: it is the only thing that names the file standing in the way.
    expect(toast.message).toBe(REFUSAL_MESSAGE);
    expect(toast.kind).toBe("warning");
    // The condition persists until acted on, so the toast must not self-dismiss.
    expect(toast.persistent).toBe(true);
    expect(toast.detail).toMatch(/not being saved/i);
    // ...and it says what the button will DO. It renames the settings file
    // that's there now out of the way; a user who reads only the label reads
    // "Restore defaults" and clicks it on an intact config.
    expect(toast.detail).toMatch(/quarantines/i);
    expect(toast.action?.label).toBe("Restore defaults");
    // NOT the generic wrapper the retryable failures get.
    expect(toast.message).not.toMatch(/Settings could not save/i);
  });

  it("the action runs restore_default_config and adopts what comes back", async () => {
    const restored = { ...CFG, analysis: { workers: 3 } };
    const calls = mockRefusingSave({ restored });
    await armAndSave();

    await waitFor(() => expect(useNotificationStore.getState().items.length).toBe(1));
    await act(async () => {
      useNotificationStore.getState().items[0].action!.onClick();
    });

    await waitFor(() => expect(calls).toContain("restore_default_config"));
    await waitFor(() =>
      expect(
        (useConfigStore.getState().config as unknown as { analysis: { workers?: number } })
          .analysis.workers,
      ).toBe(3),
    );
    await waitFor(() => {
      const msgs = useNotificationStore.getState().items.map((n) => n.message);
      expect(msgs).toContain("Settings restored to defaults");
    });
    // The file on disk is now what's on screen — trustworthy again.
    expect(useConfigStore.getState().loadUntrusted).toBe(false);
  });

  it("shows the refusal once, not on every keystroke", async () => {
    mockRefusingSave();
    await armAndSave();
    await waitFor(() => expect(useNotificationStore.getState().items.length).toBe(1));

    for (const workers of [8, 9, 10]) {
      act(() => {
        useConfigStore.getState().updateAnalysis({ workers });
      });
      fireEvent(window, new Event("beforeunload"));
    }
    await advance(1000);

    expect(useNotificationStore.getState().items.length).toBe(1);
  });

  // -------------------------------------------------------------------------
  // ...but ONE refusal is not proof the file is broken. `_save_config` re-reads
  // config.json on every autosave, so a slider drag while Google Drive holds
  // the file open for a sync read is refused exactly like a corrupt file is.
  // Believing that first refusal put a PERMANENT toast on screen whose only
  // action renames the user's fully intact settings to
  // `config.json.corrupt-<timestamp>` and writes factory defaults — for a
  // fault that lasted a second, and with nothing ever re-evaluating it.
  // -------------------------------------------------------------------------

  it("stays silent for a lock that clears, and re-saves what was refused", async () => {
    const saved: Array<{ config: unknown }> = [];
    let saveFails = true;
    (invoke as ReturnType<typeof vi.fn>).mockImplementation(
      async (cmd: string, args?: { method?: string; params?: unknown }) => {
        if (cmd !== "rpc_call") return {};
        if (args?.method === "get_config") return { ...CFG };
        if (args?.method === "save_config") {
          if (saveFails) {
            saveFails = false; // the sharing violation lasted one autosave
            throw REFUSAL;
          }
          saved.push(args!.params as { config: unknown });
          return { saved_to: "C:/cfg.json" };
        }
        return {};
      },
    );

    await armAndSave();

    // Nothing on screen at all — least of all a destructive button.
    expect(useNotificationStore.getState().items).toEqual([]);
    // ...and the change the refusal swallowed was re-issued, not dropped.
    expect(saved.length).toBe(1);
  });

  it("warns when the re-probe finds the file still unreadable", async () => {
    mockRefusingSave({ stillUnreadable: true });
    await armAndSave();

    await waitFor(() => expect(useNotificationStore.getState().items.length).toBe(1));
    const toast = useNotificationStore.getState().items[0];
    expect(toast.message).toBe(REFUSAL_MESSAGE);
    expect(toast.persistent).toBe(true);
    expect(toast.action?.label).toBe("Restore defaults");
  });

  it("takes the warning back down once a save lands again", async () => {
    let saveFails = true;
    (invoke as ReturnType<typeof vi.fn>).mockImplementation(
      async (cmd: string, args?: { method?: string }) => {
        if (cmd !== "rpc_call") return {};
        if (args?.method === "get_config") return { ...CFG };
        if (args?.method === "save_config") {
          if (saveFails) throw REFUSAL;
          return { saved_to: "C:/cfg.json" };
        }
        return {};
      },
    );

    await armAndSave();
    // The re-probe read the file back clean but the retried save was refused
    // too, so the standing warning is up.
    await waitFor(() => expect(useNotificationStore.getState().items.length).toBe(1));

    // The condition clears; the next autosave proves the file is writable.
    saveFails = false;
    act(() => {
      useConfigStore.getState().updateAnalysis({ workers: 8 });
    });
    fireEvent(window, new Event("beforeunload"));

    // A persistent toast asserting "your settings can't be read", whose button
    // quarantines them, must not outlive the condition.
    await waitFor(() => expect(useNotificationStore.getState().items).toEqual([]));
  });

  // -------------------------------------------------------------------------
  // A lock that OUTLASTS the one-shot re-probe (a long AV sweep, a cloud client
  // re-uploading) then clears on its own. Nothing re-evaluated the condition:
  // the persistent "could not be read" warning stayed up over a file that reads
  // perfectly, still offering the button that quarantines it — and the user has
  // no reason to touch a setting again just to find out.
  // -------------------------------------------------------------------------

  it("keeps re-probing and takes the warning down when the lock clears with no user edit", async () => {
    const saved: Array<{ config: { analysis?: { workers?: number } } }> = [];
    let unreadable = true;
    let getConfigCalls = 0;
    (invoke as ReturnType<typeof vi.fn>).mockImplementation(
      async (cmd: string, args?: { method?: string; params?: unknown }) => {
        if (cmd !== "rpc_call") return {};
        if (args?.method === "get_config") {
          getConfigCalls += 1;
          // Call 1 is the mount load, which must be clean or nothing arms.
          return getConfigCalls > 1 && unreadable ? { ...CFG, load_failed: true } : { ...CFG };
        }
        if (args?.method === "save_config") {
          if (unreadable) throw REFUSAL;
          saved.push(args!.params as { config: { analysis?: { workers?: number } } });
          return { saved_to: "C:/cfg.json" };
        }
        return {};
      },
    );

    await armAndSave();
    // The lock outlived the 750ms re-probe, so the warning is up — correctly.
    await waitFor(() => expect(useNotificationStore.getState().items.length).toBe(1));

    // It stays up for as long as the file really is unreadable.
    await advance(11_000);
    expect(useNotificationStore.getState().items.length).toBe(1);

    // The sync client lets go. NOTHING else happens — no edit, no click.
    unreadable = false;
    await advance(11_000);

    // The warning comes down by itself...
    await waitFor(() => expect(useNotificationStore.getState().items).toEqual([]));
    // ...and the change the refusal swallowed is written, not left dropped.
    await waitFor(() => expect(saved.length).toBeGreaterThan(0));
    expect(saved[saved.length - 1].config.analysis?.workers).toBe(7);
  });

  it("stops probing after unmount", async () => {
    const calls = mockRefusingSave({ stillUnreadable: true });
    const view = render(<Harness />);
    await waitFor(() => expect(useConfigStore.getState().loaded).toBe(true));
    act(() => {
      useConfigStore.getState().updateAnalysis({ workers: 7 });
    });
    fireEvent(window, new Event("beforeunload"));
    await advance(1000);
    await waitFor(() => expect(useNotificationStore.getState().items.length).toBe(1));

    view.unmount();
    const before = calls.length;
    await advance(31_000);
    expect(calls.length).toBe(before);
  });

  // -------------------------------------------------------------------------
  // The toast can be removed by someone who isn't this hook — the user's own X
  // button (Toast.tsx wires it to `dismiss(id)`), or the store's hard stack
  // ceiling. The held id then pointed at nothing, and every "is it standing?"
  // test read that stale id as YES: the warning could never be re-shown and the
  // re-probe was suppressed with it, so a genuinely unreadable config.json
  // dropped every later save in silence.
  // -------------------------------------------------------------------------

  it("warns again after the user dismisses the toast and the file is still unreadable", async () => {
    mockRefusingSave({ stillUnreadable: true });
    await armAndSave();
    await waitFor(() => expect(useNotificationStore.getState().items.length).toBe(1));

    // The X button, exactly as Toast.tsx calls it.
    const dismissed = useNotificationStore.getState().items[0].id;
    act(() => {
      useNotificationStore.getState().dismiss(dismissed);
    });
    expect(useNotificationStore.getState().items).toEqual([]);

    // The condition hasn't gone anywhere: the next refused save must say so.
    act(() => {
      useConfigStore.getState().updateAnalysis({ workers: 8 });
    });
    fireEvent(window, new Event("beforeunload"));
    await advance(1000);

    await waitFor(() => expect(useNotificationStore.getState().items.length).toBe(1));
    const toast = useNotificationStore.getState().items[0];
    expect(toast.id).not.toBe(dismissed); // a fresh toast, not the stale id
    expect(toast.message).toBe(REFUSAL_MESSAGE);
    expect(toast.action?.label).toBe("Restore defaults");
  });

  it("warns again when the stack ceiling evicted the toast", async () => {
    mockRefusingSave({ stillUnreadable: true });
    await armAndSave();
    await waitFor(() => expect(useNotificationStore.getState().items.length).toBe(1));

    // The hard ceiling is absolute — persistent toasts included (see
    // stores/notification.ts). A burst of standing warnings pushes ours out
    // from under the hook with no dismiss() it can observe.
    act(() => {
      for (let i = 0; i < 12; i++) {
        useNotificationStore.getState().notify(`Other warning ${i}`, { persistent: true });
      }
    });
    expect(
      useNotificationStore.getState().items.some((n) => n.message === REFUSAL_MESSAGE),
    ).toBe(false);

    act(() => {
      useConfigStore.getState().updateAnalysis({ workers: 9 });
    });
    fireEvent(window, new Event("beforeunload"));
    await advance(1000);

    await waitFor(() =>
      expect(
        useNotificationStore.getState().items.some((n) => n.message === REFUSAL_MESSAGE),
      ).toBe(true),
    );
  });

  it("still uses the generic toast for an ordinary save failure", async () => {
    (invoke as ReturnType<typeof vi.fn>).mockImplementation(
      async (cmd: string, args?: { method?: string }) => {
        if (cmd === "rpc_call" && args?.method === "get_config") return { ...CFG };
        if (cmd === "rpc_call" && args?.method === "save_config") {
          throw JSON.stringify({ message: "There is not enough space on the disk", code: -32000 });
        }
        return {};
      },
    );
    await armAndSave();

    await waitFor(() => expect(useNotificationStore.getState().items.length).toBe(1));
    const toast = useNotificationStore.getState().items[0];
    expect(toast.message).toMatch(/^Settings could not save: /);
    expect(toast.action).toBeUndefined();
  });
});
