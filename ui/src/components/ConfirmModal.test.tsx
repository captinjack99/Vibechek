import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { ConfirmModal } from "./ConfirmModal";

function setup(overrides: Partial<React.ComponentProps<typeof ConfirmModal>> = {}) {
  const onConfirm = vi.fn();
  const onCancel = vi.fn();
  const utils = render(
    <ConfirmModal
      open
      title="Delete files"
      message="This cannot be undone."
      onConfirm={onConfirm}
      onCancel={onCancel}
      {...overrides}
    />,
  );
  return { onConfirm, onCancel, ...utils };
}

describe("<ConfirmModal />", () => {
  it("renders title and message when open", () => {
    setup();
    expect(screen.getByText("Delete files")).toBeInTheDocument();
    expect(screen.getByText("This cannot be undone.")).toBeInTheDocument();
  });

  it("renders nothing when open={false}", () => {
    const { container } = setup({ open: false });
    expect(container).toBeEmptyDOMElement();
  });

  it("invokes onConfirm when the confirm button is clicked", async () => {
    const user = userEvent.setup();
    const { onConfirm } = setup({ confirmLabel: "Yes, delete" });
    await user.click(screen.getByRole("button", { name: "Yes, delete" }));
    expect(onConfirm).toHaveBeenCalledTimes(1);
  });

  it("invokes onCancel when the cancel button is clicked", async () => {
    const user = userEvent.setup();
    const { onCancel } = setup({ cancelLabel: "Nope" });
    await user.click(screen.getByRole("button", { name: "Nope" }));
    expect(onCancel).toHaveBeenCalledTimes(1);
  });

  it("invokes onCancel when the backdrop is clicked", async () => {
    const user = userEvent.setup();
    const { onCancel, container } = setup();
    // The outermost motion.div is the backdrop (fixed inset-0).
    const backdrop = container.firstChild as HTMLElement;
    expect(backdrop).toBeTruthy();
    await user.click(backdrop);
    expect(onCancel).toHaveBeenCalled();
  });
});
