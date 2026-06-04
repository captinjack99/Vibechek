/**
 * Regression test for the audit finding "ONNX setup dialog has no Cancel and
 * cannot be dismissed while running — can wedge the UI" (MED, fe-settings).
 *
 * While phase === "running" the dialog deliberately hides the close (X) button
 * (so a partial setup can't be half-dismissed). Before the fix the running view
 * also had NO Cancel button, so a setup stalled on a slow/flaky mirror trapped
 * the user behind a non-dismissable full-screen modal for up to the 1h sidecar
 * timeout. setup_onnx_engine IS a Cancellable backend op, so the running phase
 * must expose a Cancel that fires the cancel path.
 */

import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

// useSidecarProgress wires a Tauri event listener; in tests it's a no-op so the
// component renders without the sidecar plumbing.
vi.mock("../hooks/useSidecar", () => ({
  useSidecarProgress: () => {},
}));

import { OnnxSetupDialog, type OnnxSetupState } from "./OnnxSetupDialog";

function setup(
  state: OnnxSetupState,
  overrides: { onClose?: () => void; onCancel?: () => void } = {},
) {
  const onClose = overrides.onClose ?? vi.fn();
  const onCancel = overrides.onCancel;
  const utils = render(
    <OnnxSetupDialog state={state} onClose={onClose} onCancel={onCancel} />,
  );
  return { onClose, onCancel, ...utils };
}

describe("<OnnxSetupDialog /> — running phase is escapable", () => {
  it("renders a Cancel button while running when onCancel is provided", () => {
    setup({ phase: "running" }, { onCancel: vi.fn() });
    expect(screen.getByRole("button", { name: /cancel/i })).toBeInTheDocument();
  });

  it("does NOT render the close (X) button while running", () => {
    setup({ phase: "running" }, { onCancel: vi.fn() });
    // The X button uses aria-label="Close"; it must stay hidden mid-run so the
    // setup can only be aborted via the wired Cancel path.
    expect(screen.queryByRole("button", { name: "Close" })).toBeNull();
  });

  it("invokes onCancel when the running Cancel button is clicked", async () => {
    const user = userEvent.setup();
    const onCancel = vi.fn();
    setup({ phase: "running" }, { onCancel });
    await user.click(screen.getByRole("button", { name: /cancel/i }));
    expect(onCancel).toHaveBeenCalledTimes(1);
  });

  it("omits the Cancel button if onCancel is not provided (back-compat)", () => {
    setup({ phase: "running" });
    expect(screen.queryByRole("button", { name: /cancel/i })).toBeNull();
  });

  it("shows the close (X) button once finished (done phase)", () => {
    setup({ phase: "done", staged: 16 }, { onCancel: vi.fn() });
    expect(screen.getByRole("button", { name: "Close" })).toBeInTheDocument();
    // The Done button confirms the success view rendered.
    expect(screen.getByRole("button", { name: "Done" })).toBeInTheDocument();
  });

  it("renders nothing when state is null", () => {
    const { container } = setup(null);
    expect(container).toBeEmptyDOMElement();
  });
});
