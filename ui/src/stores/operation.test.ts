/**
 * Tests for the operation store's correlation ids (`begin()` → opId) and the
 * `progressMatches` filter that attributes shared-stream progress events to
 * the exact op instance that produced them.
 */
import { describe, expect, it } from "vitest";

import type { ProgressEvent } from "../types";
import { newOpId, progressMatches, useOperationStore } from "./operation";

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
  });
});
