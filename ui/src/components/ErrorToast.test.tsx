/**
 * Tests for ErrorToast's kind-driven rendering:
 *   - retryable  → "Try again" (re-issues the failed call through rpc)
 *   - engine_dead → "Restart Vibechek" (+ the mid-analyze analyzed count)
 *   - no kind     → graceful: headline only, no recovery buttons
 *   - detail toggle appears only when a technical detail is present
 */

import { describe, expect, it, vi } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { invoke } from "@tauri-apps/api/core";

import { ErrorToast } from "./ErrorToast";
import { useOperationStore } from "../stores";
import type { OperationError } from "../stores/operation";

function setError(info: OperationError) {
  useOperationStore.setState({ error: info.headline, errorInfo: info });
}

describe("<ErrorToast />", () => {
  it("renders nothing when there is no error", () => {
    const { container } = render(<ErrorToast />);
    expect(container).toBeEmptyDOMElement();
  });

  it("shows a Try again button for a retryable error (and no Restart)", () => {
    setError({
      headline: "The library scan is taking longer than expected.",
      kind: "retryable",
      raw: "{}",
      retry: { method: "find_duplicates", params: {} },
    });
    render(<ErrorToast />);
    expect(screen.getByText("Try again")).toBeInTheDocument();
    expect(screen.queryByText("Restart Vibechek")).not.toBeInTheDocument();
  });

  it("re-issues the failed call when Try again is clicked", async () => {
    (invoke as ReturnType<typeof vi.fn>).mockClear();
    setError({
      headline: "The library scan is taking longer than expected.",
      kind: "retryable",
      raw: "{}",
      retry: { method: "find_duplicates", params: { path: "D:/Music" } },
    });
    render(<ErrorToast />);
    fireEvent.click(screen.getByText("Try again"));
    await waitFor(() => {
      expect(invoke).toHaveBeenCalledWith("rpc_call", {
        method: "find_duplicates",
        params: { path: "D:/Music" },
      });
    });
    // The banner clears itself before re-firing.
    expect(useOperationStore.getState().errorInfo).toBeNull();
  });

  it("shows a Restart Vibechek button for an engine_dead error (and no Retry)", () => {
    setError({
      headline: "The analysis service stopped unexpectedly.",
      kind: "engine_dead",
      raw: "{}",
    });
    render(<ErrorToast />);
    expect(screen.getByText("Restart Vibechek")).toBeInTheDocument();
    expect(screen.queryByText("Try again")).not.toBeInTheDocument();
  });

  it("surfaces the analyzed-track count on a mid-analyze death", () => {
    setError({
      headline: "The analysis service stopped unexpectedly and this action didn't finish.",
      kind: "engine_dead",
      raw: "{}",
      analyzedCount: 12,
      analyzedTotal: 40,
    });
    render(<ErrorToast />);
    expect(screen.getByText(/12 of 40 tracks were analyzed/)).toBeInTheDocument();
  });

  it("degrades gracefully: headline only, no recovery buttons, when kind is absent", () => {
    setError({ headline: "Something specific failed.", raw: "raw text" });
    render(<ErrorToast />);
    expect(screen.getByText("Something specific failed.")).toBeInTheDocument();
    expect(screen.queryByText("Try again")).not.toBeInTheDocument();
    expect(screen.queryByText("Restart Vibechek")).not.toBeInTheDocument();
    // Always-available actions still render.
    expect(screen.getByText("Copy details")).toBeInTheDocument();
  });

  it("shows the technical-details toggle only when a detail is present", () => {
    setError({ headline: "h", raw: "r", detail: "exit code 137; worker OOM" });
    render(<ErrorToast />);
    expect(screen.getByText("Technical details")).toBeInTheDocument();
    expect(screen.getByText("exit code 137; worker OOM")).toBeInTheDocument();
  });

  it("omits the details toggle when there's nothing to demote", () => {
    setError({ headline: "h", raw: "r" });
    render(<ErrorToast />);
    expect(screen.queryByText("Technical details")).not.toBeInTheDocument();
  });
});
