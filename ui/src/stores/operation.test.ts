/**
 * Tests for the operation store's correlation ids (`begin()` → opId) and the
 * `progressMatches` filter that attributes shared-stream progress events to
 * the exact op instance that produced them.
 */
import { beforeEach, describe, expect, it } from "vitest";

import type { ProgressEvent } from "../types";
import { RpcError } from "../hooks/useSidecar";
import { newOpId, progressMatches, useOperationStore } from "./operation";

/** Build an RpcError from a structured envelope, then stamp on the (method,
 *  params) `rpc()` would have captured. */
function rpcError(
  envelope: object,
  call?: { method: string; params: object },
): RpcError {
  const e = new RpcError(JSON.stringify(envelope));
  if (call) {
    e.method = call.method;
    e.params = call.params;
  }
  return e;
}

const evt = (op_id?: string): ProgressEvent => ({
  current: 1,
  total: 2,
  message: "working",
  ...(op_id ? { op_id } : {}),
});

describe("newOpId", () => {
  it("generates non-empty unique ids", () => {
    const a = newOpId();
    const b = newOpId();
    expect(a).toBeTruthy();
    expect(b).toBeTruthy();
    expect(a).not.toEqual(b);
  });
});

describe("progressMatches", () => {
  it("accepts unstamped events regardless of the local id (legacy sidecar)", () => {
    expect(progressMatches(evt(), null)).toBe(true);
    expect(progressMatches(evt(), "mine")).toBe(true);
  });

  it("accepts stamped events when the consumer has no id (legacy consumer)", () => {
    expect(progressMatches(evt("other"), null)).toBe(true);
    expect(progressMatches(evt("other"), undefined)).toBe(true);
  });

  it("drops only a positive mismatch — both sides present and different", () => {
    expect(progressMatches(evt("mine"), "mine")).toBe(true);
    expect(progressMatches(evt("other"), "mine")).toBe(false);
  });
});

describe("useOperationStore correlation ids", () => {
  it("begin() returns the id it stored; finish() clears it", () => {
    const id = useOperationStore.getState().begin("analyze");
    expect(id).toBeTruthy();
    expect(useOperationStore.getState().opId).toBe(id);
    expect(useOperationStore.getState().active).toBe("analyze");

    useOperationStore.getState().finish();
    expect(useOperationStore.getState().opId).toBeNull();
    expect(useOperationStore.getState().active).toBeNull();
  });

  it("each begin() issues a fresh id; fail() clears it", () => {
    const first = useOperationStore.getState().begin("dedupe");
    useOperationStore.getState().finish();
    const second = useOperationStore.getState().begin("dedupe");
    expect(second).not.toBe(first);

    useOperationStore.getState().fail("boom");
    expect(useOperationStore.getState().opId).toBeNull();
    expect(useOperationStore.getState().error).toBe("boom");
    useOperationStore.getState().clearError();
  });

  it("a cancellation-flavored fail clears the id silently", () => {
    useOperationStore.getState().begin("tag");
    useOperationStore.getState().fail({ cancelled: true });
    const s = useOperationStore.getState();
    expect(s.opId).toBeNull();
    expect(s.active).toBeNull();
    expect(s.error).toBeNull();
    expect(s.errorInfo).toBeNull();
  });
});

describe("useOperationStore.fail() error classification", () => {
  beforeEach(() => {
    useOperationStore.setState({
      active: null,
      opId: null,
      progress: null,
      startedAt: null,
      error: null,
      errorInfo: null,
    });
  });

  it("classifies an engine_dead envelope: headline on `error`, kind + detail on errorInfo", () => {
    useOperationStore.getState().begin("dedupe");
    useOperationStore.getState().fail(
      rpcError({
        message: "The analysis service stopped unexpectedly.",
        data: {
          kind: "engine_dead",
          headline: "The analysis service stopped unexpectedly.",
          detail: "sidecar died mid-request on method 'find_duplicates' (binary: X)",
        },
      }),
    );
    const s = useOperationStore.getState();
    // The inline per-view banners read `error` — must be the plain headline.
    expect(s.error).toBe("The analysis service stopped unexpectedly.");
    expect(s.errorInfo?.kind).toBe("engine_dead");
    expect(s.errorInfo?.detail).toContain("find_duplicates");
    // engine_dead is not retryable → no retry payload.
    expect(s.errorInfo?.retry).toBeUndefined();
  });

  it("carries the retry (method, params) for a retryable envelope", () => {
    useOperationStore.getState().begin("dedupe");
    useOperationStore.getState().fail(
      rpcError(
        {
          message: "The library scan is taking longer than expected.",
          data: { kind: "retryable", headline: "The library scan is taking longer than expected." },
        },
        { method: "find_duplicates", params: { path: "D:/Music" } },
      ),
    );
    const info = useOperationStore.getState().errorInfo!;
    expect(info.kind).toBe("retryable");
    expect(info.retry).toEqual({ method: "find_duplicates", params: { path: "D:/Music" } });
  });

  it("attaches the analyzed-track count on a mid-analyze death (from the last progress frame)", () => {
    useOperationStore.getState().begin("analyze");
    useOperationStore.getState().setProgress({ current: 12, total: 40, message: "…" });
    useOperationStore.getState().fail(
      rpcError({
        message: "The analysis service stopped unexpectedly and this action didn't finish.",
        data: { kind: "engine_dead", headline: "The analysis service stopped unexpectedly and this action didn't finish." },
      }),
    );
    const info = useOperationStore.getState().errorInfo!;
    expect(info.analyzedCount).toBe(12);
    expect(info.analyzedTotal).toBe(40);
    // progress itself is cleared.
    expect(useOperationStore.getState().progress).toBeNull();
  });

  it("does NOT attach a count for a non-analyze op", () => {
    useOperationStore.getState().begin("dedupe");
    useOperationStore.getState().setProgress({ current: 5, total: 10, message: "…" });
    useOperationStore.getState().fail(rpcError({ message: "boom", data: { kind: "fatal" } }));
    expect(useOperationStore.getState().errorInfo?.analyzedCount).toBeUndefined();
  });

  it("degrades gracefully for a plain string error (no envelope)", () => {
    useOperationStore.getState().fail("Something specific broke");
    const s = useOperationStore.getState();
    expect(s.error).toBe("Something specific broke");
    expect(s.errorInfo?.headline).toBe("Something specific broke");
    expect(s.errorInfo?.kind).toBeUndefined();
    expect(s.errorInfo?.retry).toBeUndefined();
  });

  it("clearError() clears both error and errorInfo", () => {
    useOperationStore.getState().fail("x");
    useOperationStore.getState().clearError();
    const s = useOperationStore.getState();
    expect(s.error).toBeNull();
    expect(s.errorInfo).toBeNull();
  });

  it("attaches an extras.retryAction and defaults kind to 'retryable' on a plain error", () => {
    const retryAction = async () => {};
    useOperationStore.getState().fail("Couldn't read the saved analysis", { retryAction });
    const info = useOperationStore.getState().errorInfo!;
    expect(info.retryAction).toBe(retryAction);
    // A plain-string reason carries no envelope kind — the retry closure defaults it.
    expect(info.kind).toBe("retryable");
    expect(info.headline).toBe("Couldn't read the saved analysis");
  });

  it("does NOT override an existing envelope kind when attaching a retryAction", () => {
    const retryAction = async () => {};
    useOperationStore.getState().fail(
      rpcError({ message: "boom", data: { kind: "engine_dead", headline: "boom" } }),
      { retryAction },
    );
    const info = useOperationStore.getState().errorInfo!;
    expect(info.retryAction).toBe(retryAction);
    // Only defaults when unset — a real envelope kind wins.
    expect(info.kind).toBe("engine_dead");
  });

  it("cancellation still exits silently even when a retryAction is supplied", () => {
    const retryAction = async () => {};
    useOperationStore.getState().begin("analyze");
    useOperationStore.getState().fail({ cancelled: true }, { retryAction });
    const s = useOperationStore.getState();
    expect(s.error).toBeNull();
    expect(s.errorInfo).toBeNull();
    expect(s.active).toBeNull();
  });
});
