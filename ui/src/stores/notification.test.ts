/**
 * Tests for the notification store's persistent + action variant (WP-L1).
 *
 * App-breaking conditions (the risky-install-path warning) must not auto-dismiss
 * after a few seconds and should carry an in-view next step.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";

import { useNotificationStore } from "./notification";

describe("notification store — persistent + action", () => {
  it("records persistent + action on notify", () => {
    const onClick = vi.fn();
    useNotificationStore.getState().notify("Risky install path", {
      kind: "warning",
      detail: "Install path contains 'my drive'.",
      persistent: true,
      action: { label: "Open install folder", onClick },
    });
    const item = useNotificationStore.getState().items.at(-1)!;
    expect(item.kind).toBe("warning");
    expect(item.persistent).toBe(true);
    expect(item.action?.label).toBe("Open install folder");
    item.action?.onClick();
    expect(onClick).toHaveBeenCalledOnce();
  });

  it("defaults persistent/action to undefined for a plain toast", () => {
    useNotificationStore.getState().notify("Saved");
    const item = useNotificationStore.getState().items.at(-1)!;
    expect(item.persistent).toBeUndefined();
    expect(item.action).toBeUndefined();
    expect(item.kind).toBe("success");
  });
});

describe("notification store — MAX_STACK eviction", () => {
  beforeEach(() => {
    useNotificationStore.setState({ items: [] });
  });

  it("keeps a persistent toast when a burst of ordinary ones overflows the cap", () => {
    // The risky-install-path warning arrives first and sits at index 0 forever
    // (ordinary toasts self-dismiss, it doesn't). A plain slice(-MAX_STACK)
    // evicted exactly that one — the toast documented to never vanish unclicked.
    useNotificationStore.getState().notify("Risky install path", {
      kind: "warning",
      persistent: true,
      action: { label: "Open install folder", onClick: () => {} },
    });
    // A burst inside the 4-8s auto-dismiss window (e.g. five impatient clicks
    // on Apply Tags while a backup runs).
    for (let i = 0; i < 6; i++) {
      useNotificationStore.getState().notify(`Backup in progress ${i}`, { kind: "info" });
    }
    const items = useNotificationStore.getState().items;
    const kept = items.find((n) => n.persistent);
    expect(kept?.message).toBe("Risky install path");
    expect(kept?.action?.label).toBe("Open install folder");
    // The newest ordinary toast survives; the oldest ordinary ones were evicted.
    expect(items.at(-1)?.message).toBe("Backup in progress 5");
    expect(items.some((n) => n.message === "Backup in progress 0")).toBe(false);
  });

  // The ceiling is absolute — which is exactly why an owner holding a toast id
  // (useConfigPersistence's unreadable-settings warning) must check the id is
  // still in `items` rather than trusting it forever: an evicted warning it
  // still believes is up is a warning it can never put back.
  it("drops even a persistent toast once nothing else is evictable", () => {
    const id = useNotificationStore.getState().notify("Settings could not be read", {
      kind: "warning",
      persistent: true,
      action: { label: "Restore defaults", onClick: () => {} },
    });
    for (let i = 0; i < 12; i++) {
      useNotificationStore.getState().notify(`Other warning ${i}`, { persistent: true });
    }
    expect(useNotificationStore.getState().items.some((n) => n.id === id)).toBe(false);
  });

  it("still caps the stack overall (a runaway persistent producer can't grow it without bound)", () => {
    for (let i = 0; i < 40; i++) {
      useNotificationStore.getState().notify(`Persistent ${i}`, { persistent: true });
    }
    expect(useNotificationStore.getState().items.length).toBeLessThanOrEqual(10);
    // The hard ceiling keeps the NEWEST, so the most recent warning is in view.
    expect(useNotificationStore.getState().items.at(-1)?.message).toBe("Persistent 39");
  });
});
