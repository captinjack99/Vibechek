/**
 * Tests for the notification store's persistent + action variant (WP-L1).
 *
 * App-breaking conditions (the risky-install-path warning) must not auto-dismiss
 * after a few seconds and should carry an in-view next step.
 */

import { describe, expect, it, vi } from "vitest";

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
