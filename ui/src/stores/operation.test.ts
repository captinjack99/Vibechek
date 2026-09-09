/**
 * Tests for the operation store's correlation ids (`begin()` → opId) and the
 * `progressMatches` filter that attributes shared-stream progress events to
 * the exact op instance that produced them.
 */
import { beforeEach, describe, expect, it } from "vitest";

import type { OrganizePlan, ProgressEvent } from "../types";
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
    useOperationStore.getState().fail(
      rpcError(
        {
          message: "Checking your setup is taking longer than expected.",
          data: { kind: "retryable", headline: "Checking your setup is taking longer than expected." },
        },
        { method: "preflight", params: { deep: true } },
      ),
    );
    const info = useOperationStore.getState().errorInfo!;
    expect(info.kind).toBe("retryable");
    expect(info.retry).toEqual({ method: "preflight", params: { deep: true } });
  });

  it("stamps the running op's kind on the retry handle (so the replay gets a progress panel)", () => {
    useOperationStore.getState().begin("install-wsl");
    useOperationStore.getState().fail(
      rpcError(
        {
          message: "Installing Windows' Linux environment is taking longer than expected.",
          data: { kind: "retryable", headline: "Installing is taking longer than expected." },
        },
        { method: "install_wsl", params: { op_id: "dead-op" } },
      ),
    );
    const info = useOperationStore.getState().errorInfo!;
    expect(info.retry).toEqual({
      method: "install_wsl",
      params: { op_id: "dead-op" },
      kind: "install-wsl",
    });
  });

  it("refuses a generic replay handle for a result-bearing long op", () => {
    // A bare re-issue of analyze_directory would run for hours with no
    // begin() (no progress panel, no Cancel) and then throw the report away.
    // Those sites must pass an explicit retryAction instead.
    useOperationStore.getState().begin("analyze");
    useOperationStore.getState().fail(
      rpcError(
        {
          message: "Analysis stopped unexpectedly while processing your library.",
          data: { kind: "retryable", headline: "Analysis stopped unexpectedly." },
        },
        { method: "analyze_directory", params: { path: "D:/Music", op_id: "dead-op" } },
      ),
    );
    const info = useOperationStore.getState().errorInfo!;
    expect(info.kind).toBe("retryable");
    expect(info.retry).toBeUndefined();
    expect(info.retryAction).toBeUndefined();
  });

  it("refuses a generic replay for the other result-bearing / mutating long ops", () => {
    for (const method of [
      "find_duplicates",
      "handle_duplicates",
      "organize",
      "apply_ml_tags",
      "backup_tags",
    ]) {
      useOperationStore.getState().fail(
        rpcError(
          { message: "x", data: { kind: "retryable", headline: "x" } },
          { method, params: {} },
        ),
      );
      expect(useOperationStore.getState().errorInfo?.retry, method).toBeUndefined();
    }
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

describe("useOperationStore.setOrganizePlan — staleness key", () => {
  const plan = { moves: [], base_dir: "D:/Music", errors: [] } as unknown as OrganizePlan;

  beforeEach(() => {
    useOperationStore.setState({ organizePlan: null, organizePlanKey: null });
  });

  it("stores the params fingerprint alongside the plan", () => {
    useOperationStore.getState().setOrganizePlan(plan, "D:/Music|sub:true|min:10");
    const s = useOperationStore.getState();
    expect(s.organizePlan).toBe(plan);
    expect(s.organizePlanKey).toBe("D:/Music|sub:true|min:10");
  });

  it("clears the key with the plan (a cleared plan can't stay 'fresh')", () => {
    useOperationStore.getState().setOrganizePlan(plan, "key-a");
    useOperationStore.getState().setOrganizePlan(null);
    const s = useOperationStore.getState();
    expect(s.organizePlan).toBeNull();
    expect(s.organizePlanKey).toBeNull();
  });

  it("never lets a new plan inherit the previous plan's key", () => {
    useOperationStore.getState().setOrganizePlan(plan, "key-a");
    // Old call shape (no key) — the plan is explicitly unkeyed, so a consumer
    // gating Execute on the key treats it as stale rather than as key-a.
    useOperationStore.getState().setOrganizePlan(plan);
    expect(useOperationStore.getState().organizePlanKey).toBeNull();
  });
});

/**
 * The global vitest setup (src/test/setup.ts) resets the operation store before
 * every test. It used to omit `opId` and `organizePlanKey`, so a test that ran
 * an op leaked both into the next one — and `organizePlanKey` is what
 * OrganizeView gates Execute on, so a stale key silently armed or disarmed the
 * plan-staleness check depending on test ORDER.
 *
 * These two cases run in declaration order: the first dirties every field, the
 * second asserts it started clean.
 */
describe("operation store — per-test reset covers every field", () => {
  it("(1) dirties the whole store", () => {
    const store = useOperationStore.getState();
    store.begin("organize");
    store.setOrganizePlan(
      { base_dir: "D:/Music", moves: [], small_genres: [], genre_counts: {},
        existing_genre_counts: {}, errors: [] } as OrganizePlan,
      "min=10|sub=true|root=D:/Music",
    );
    store.setDuplicateReport(null);
    store.fail(new Error("boom"));

    const dirty = useOperationStore.getState();
    expect(dirty.organizePlanKey).toBe("min=10|sub=true|root=D:/Music");
    expect(dirty.error).toBeTruthy();
  });

  it("(2) starts the next test from a clean store", () => {
    const s = useOperationStore.getState();
    expect(s.active).toBeNull();
    expect(s.opId).toBeNull();
    expect(s.progress).toBeNull();
    expect(s.startedAt).toBeNull();
    expect(s.error).toBeNull();
    expect(s.errorInfo).toBeNull();
    expect(s.duplicateReport).toBeNull();
    expect(s.organizePlan).toBeNull();
    // The two that used to leak.
    expect(s.organizePlanKey).toBeNull();
  });
});
