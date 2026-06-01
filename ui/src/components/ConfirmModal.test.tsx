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

  // --- Accessibility / focus behaviour (audit: MED frontend ConfirmModal) ----

  it("exposes dialog semantics (role=dialog + aria-modal)", () => {
    setup();
    const dialog = screen.getByRole("dialog");
    expect(dialog).toHaveAttribute("aria-modal", "true");
  });

  it("autofocuses the CONFIRM button for the default variant", async () => {
    setup({ confirmLabel: "Apply" });
    const confirm = screen.getByRole("button", { name: "Apply" });
    // Focus lands in a requestAnimationFrame; wait for it.
    await vi.waitFor(() => expect(confirm).toHaveFocus());
  });

  it("autofocuses CANCEL (not the destructive action) for variant=danger", async () => {
    setup({ variant: "danger", confirmLabel: "Delete", cancelLabel: "Cancel" });
    const cancel = screen.getByRole("button", { name: "Cancel" });
    const confirm = screen.getByRole("button", { name: "Delete" });
    await vi.waitFor(() => expect(cancel).toHaveFocus());
    expect(confirm).not.toHaveFocus();
  });

  it("calls onCancel when Escape is pressed", async () => {
    const user = userEvent.setup();
    const { onCancel } = setup({ variant: "danger" });
    await user.keyboard("{Escape}");
    expect(onCancel).toHaveBeenCalledTimes(1);
  });

  it("traps Tab focus inside the dialog (wraps from last back to first)", async () => {
    const user = userEvent.setup();
    setup({ variant: "danger", confirmLabel: "Delete", cancelLabel: "Cancel" });
    const confirm = screen.getByRole("button", { name: "Delete" });
    // Cancel is autofocused for danger. The confirm button is the last
    // focusable; Tabbing off it should wrap to the first focusable (the
    // close "X") rather than escaping to document.body.
    confirm.focus();
    expect(confirm).toHaveFocus();
    await user.tab();
    expect(document.body).not.toHaveFocus();
    const dialog = screen.getByRole("dialog");
    expect(dialog.contains(document.activeElement)).toBe(true);
  });
});
