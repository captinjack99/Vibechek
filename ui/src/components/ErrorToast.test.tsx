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

  it("re-issues a long op's retry under a fresh op (progress panel + Cancel come back)", async () => {
    const mockInvoke = invoke as ReturnType<typeof vi.fn>;
    mockInvoke.mockClear();
    // Hold the replay in flight so we can observe the operation state while it
    // runs (finish() clears it the moment it resolves).
    let release: (v: unknown) => void = () => {};
    mockInvoke.mockImplementation((cmd: string, args?: { method?: string }) => {
      if (cmd === "rpc_call" && args?.method === "install_wsl") {
        return new Promise((res) => {
          release = res;
        });
      }
      return Promise.resolve({});
    });
    try {
      setError({
        headline: "Installing Windows' Linux environment is taking longer than expected.",
        kind: "retryable",
        raw: "{}",
        // `kind` is what fail() stamped on: the op that was running when it died.
        retry: { method: "install_wsl", params: { op_id: "dead-op" }, kind: "install-wsl" },
      });
      render(<ErrorToast />);
      fireEvent.click(screen.getByText("Try again"));

      // The replay runs as a REAL operation, so the progress overlay (and its
      // Cancel button) come back — a bare replay left `active` null and ran
      // invisibly for as long as the op took.
      await waitFor(() => expect(useOperationStore.getState().active).toBe("install-wsl"));
      const opId = useOperationStore.getState().opId;
      const call = mockInvoke.mock.calls.find(
        (c) => c[0] === "rpc_call" && c[1]?.method === "install_wsl",
      )!;
      // The captured params carry the DEAD op's correlation id — progress
      // frames stamped with it are dropped, so the replay gets the fresh one.
      expect(call[1].params.op_id).toBe(opId);
      expect(call[1].params.op_id).not.toBe("dead-op");

      release({});
      await waitFor(() => expect(useOperationStore.getState().active).toBeNull());
    } finally {
      mockInvoke.mockImplementation(async () => ({}));
    }
  });

  it("shows a Try again button for a retryAction-only error (no generic retry)", () => {
    setError({
      headline: "Vibechek couldn't read the saved analysis for this library right now.",
      kind: "retryable",
      raw: "{}",
      retryAction: async () => {},
    });
    render(<ErrorToast />);
    expect(screen.getByText("Try again")).toBeInTheDocument();
  });

  it("invokes the retryAction closure when Try again is clicked (not the generic rpc)", async () => {
    (invoke as ReturnType<typeof vi.fn>).mockClear();
    const retryAction = vi.fn(async () => {});
    setError({
      headline: "Vibechek couldn't read the saved analysis for this library right now.",
      kind: "retryable",
      raw: "{}",
      retryAction,
    });
    render(<ErrorToast />);
    fireEvent.click(screen.getByText("Try again"));
    await waitFor(() => {
      expect(retryAction).toHaveBeenCalledTimes(1);
    });
    // The closure owns re-issuing the call — the toast must not also fire the
    // generic rpc replay path.
    expect(invoke).not.toHaveBeenCalled();
    // The banner clears itself before running the closure.
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

  it("renders both memory-refusal buttons when their option flags are set", () => {
    setError({
      headline: "Not enough memory to run the advanced genre model right now.",
      kind: "fatal",
      raw: "{}",
      options: { canSwitchClassifier: true, canIncreaseMemory: true },
    });
    render(<ErrorToast />);
    expect(screen.getByText("Switch to the standard genre model")).toBeInTheDocument();
    expect(screen.getByText("Give Vibechek more memory")).toBeInTheDocument();
    expect(screen.queryByText("Install WSL")).not.toBeInTheDocument();
  });

  it("shows only the classifier switch when increase-memory isn't offered", () => {
    setError({
      headline: "Not enough memory to run analysis right now.",
      kind: "fatal",
      raw: "{}",
      options: { canSwitchClassifier: true },
    });
    render(<ErrorToast />);
    expect(screen.getByText("Switch to the standard genre model")).toBeInTheDocument();
    expect(screen.queryByText("Give Vibechek more memory")).not.toBeInTheDocument();
  });

  it("shows Install WSL for a can_install_wsl error and firing it calls install_wsl", async () => {
    (invoke as ReturnType<typeof vi.fn>).mockClear();
    setError({
      headline: "Windows' Linux environment isn't installed on this PC yet.",
      kind: "fatal",
      raw: "{}",
      options: { canInstallWsl: true },
    });
    render(<ErrorToast />);
    const btn = screen.getByText("Install WSL");
    expect(btn).toBeInTheDocument();
    fireEvent.click(btn);
    await waitFor(() => {
      expect(invoke).toHaveBeenCalledWith(
        "rpc_call",
        expect.objectContaining({ method: "install_wsl" }),
      );
    });
  });

  it("renders no recovery-option buttons when the error carries no options", () => {
    setError({ headline: "Something specific failed.", raw: "{}" });
    render(<ErrorToast />);
    expect(screen.queryByText("Switch to the standard genre model")).not.toBeInTheDocument();
    expect(screen.queryByText("Give Vibechek more memory")).not.toBeInTheDocument();
    expect(screen.queryByText("Install WSL")).not.toBeInTheDocument();
  });
});
