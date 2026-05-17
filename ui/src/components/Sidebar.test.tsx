import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { Sidebar } from "./Sidebar";
import { useUIStore } from "../stores";

describe("<Sidebar />", () => {
  it("renders all five top-level nav items", () => {
    render(<Sidebar />);
    for (const label of ["Library", "Duplicates", "Organize", "Tags", "Settings"]) {
      expect(screen.getByRole("button", { name: new RegExp(label) })).toBeInTheDocument();
    }
  });

  it("clicking a nav item updates the view mode on the UI store", async () => {
    const user = userEvent.setup();
    render(<Sidebar />);
    expect(useUIStore.getState().viewMode).toBe("library");

    await user.click(screen.getByRole("button", { name: /Duplicates/ }));
    expect(useUIStore.getState().viewMode).toBe("duplicates");

    await user.click(screen.getByRole("button", { name: /Settings/ }));
    expect(useUIStore.getState().viewMode).toBe("settings");
  });

  it("applies the active styling class to the current view's nav item", () => {
    useUIStore.setState({ viewMode: "organize" });
    render(<Sidebar />);
    const organize = screen.getByRole("button", { name: /Organize/ });
    // Active items get the accent background class.
    expect(organize.className).toMatch(/bg-accent/);

    // And non-active items don't.
    const library = screen.getByRole("button", { name: /Library/ });
    expect(library.className).not.toMatch(/bg-accent\/15/);
  });
});
