/**
 * Regression test for the product-owner-reported bug: the analyze/dedupe
 * progress overlay was `fixed bottom-6 left-1/2 -translate-x-1/2` with
 * `min-w-[420px] max-w-[640px]` — a center-anchored panel with a
 * content-driven width. Every progress message of a different length
 * re-widthed the panel and BOTH edges moved, so the Cancel button "moves all
 * over the place, impossible to click", and long content could overflow past
 * `max-w` off the right side of the screen.
 *
 * The fix pins the panel to the bottom-right corner with a FIXED width, so
 * geometry (and the Cancel button's position) never depends on message
 * length; long text truncates within the fixed width instead of growing it.
 */

import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";

import { AnalysisProgress } from "./AnalysisProgress";
import { useOperationStore } from "../stores";
import type { ProgressEvent } from "../types";

const LONG_MESSAGE =
  "Analyzing /Volumes/Music Library/Some Really Long Nested Folder Name/" +
  "Artist With A Very Long Name - Track Title That Goes On And On.flac " +
  "(this is a deliberately long progress message to exercise truncation)";

function beginOperation(progress: ProgressEvent = { current: 3, total: 10, message: "" }) {
  useOperationStore.setState({
    active: "analyze",
    opId: "test-op",
    progress,
    startedAt: Date.now(),
    error: null,
  });
}

describe("<AnalysisProgress /> — right-anchored, fixed-width overlay", () => {
  it("anchors to the bottom-right with fixed geometry, not center-anchored", () => {
    beginOperation();
    const { container } = render(<AnalysisProgress />);

    // Outermost motion.div carries the positioning classes.
    const wrapper = container.firstChild as HTMLElement;
    expect(wrapper).toBeTruthy();
    expect(wrapper.className).toContain("bottom-6");
    expect(wrapper.className).toContain("right-6");

    // Must NOT carry the old center-anchoring classes.
    expect(wrapper.className).not.toContain("left-1/2");
    expect(wrapper.className).not.toContain("-translate-x-1/2");
  });

  it("uses a fixed panel width clamped for narrow viewports, not min/max breathing width", () => {
    beginOperation();
    const { container } = render(<AnalysisProgress />);

    const wrapper = container.firstChild as HTMLElement;
    const panel = wrapper.firstChild as HTMLElement;
    expect(panel).toBeTruthy();
    expect(panel.className).toContain("w-[440px]");
    expect(panel.className).toContain("max-w-[calc(100vw-3rem)]");

    // Must NOT carry the old content-driven min/max width classes.
    expect(panel.className).not.toContain("min-w-[420px]");
    expect(panel.className).not.toContain("max-w-[640px]");
  });

  it("renders Cancel with flex-none so it can't be pushed or shrunk by sibling content", () => {
    beginOperation();
    render(<AnalysisProgress />);

    const cancel = screen.getByRole("button", { name: /cancel/i });
    expect(cancel.className).toContain("flex-none");
  });

  it("truncates a long progress message instead of growing the panel", () => {
    beginOperation({ current: 3, total: 10, message: LONG_MESSAGE });
    render(<AnalysisProgress />);

    const message = screen.getByText(LONG_MESSAGE);
    expect(message.className).toContain("truncate");
    // Its flex-parent needs min-w-0 for truncate to actually take effect.
    expect(message.parentElement?.className).toContain("min-w-0");
  });

  it("truncates the label row even when the label text itself is long", () => {
    // "revert" etc. fall back to the raw kind string when unmapped; exercise
    // the mapped label's truncate class directly since KIND_LABELS values
    // are the realistic long strings ("Analyzing library", etc.).
    beginOperation();
    render(<AnalysisProgress />);

    const label = screen.getByText("Analyzing library");
    expect(label.className).toContain("truncate");
  });

  it("truncates the stats row (current/total) with min-w-0 alongside the elapsed time", () => {
    beginOperation({ current: 1234567, total: 9999999, message: "" });
    render(<AnalysisProgress />);

    const stats = screen.getByText("1,234,567 / 9,999,999");
    expect(stats.className).toContain("truncate");
    expect(stats.className).toContain("min-w-0");

    const elapsed = screen.getByText(/elapsed/);
    expect(elapsed.className).toContain("flex-none");
  });

  it("renders nothing when no operation is active", () => {
    useOperationStore.setState({ active: null, progress: null, startedAt: null });
    const { container } = render(<AnalysisProgress />);
    expect(container).toBeEmptyDOMElement();
  });
});
