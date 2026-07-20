/**
 * Tests for RpcError envelope parsing + the retry-capture in `rpc()`.
 *
 * Both the Python sidecar and the Rust transport layer now emit the same
 * structured error envelope: `{ message, code, data: { kind, headline, detail,
 * cancelled } }`. RpcError parses it so ErrorToast can render a plain headline,
 * a demote-able detail, and a Restart/Retry action — and degrades gracefully to
 * today's behavior when the envelope is absent.
 */

import { describe, expect, it, vi } from "vitest";
import { invoke } from "@tauri-apps/api/core";

import { RpcError, rpc } from "./useSidecar";

describe("RpcError envelope parsing", () => {
  it("parses kind/headline/detail from a structured engine_dead envelope", () => {
    const raw = JSON.stringify({
      code: -32001,
      message: "The analysis service stopped unexpectedly.",
      data: {
        kind: "engine_dead",
        headline: "The analysis service stopped unexpectedly.",
        detail:
          "sidecar died mid-request on method 'analyze_directory' (binary: C:/x/vibechek.exe)",
      },
    });
    const e = new RpcError(raw);
    expect(e.kind).toBe("engine_dead");
    expect(e.headline).toBe("The analysis service stopped unexpectedly.");
    expect(e.detail).toContain("analyze_directory");
    // `message` mirrors the headline (never the raw error).
    expect(e.message).toBe("The analysis service stopped unexpectedly.");
    expect(e.raw).toBe(raw);
  });

  it("recognizes the retryable kind", () => {
    const raw = JSON.stringify({
      message: "The library scan is taking longer than expected.",
      data: { kind: "retryable", headline: "x", detail: "timed out after 60s" },
    });
    expect(new RpcError(raw).kind).toBe("retryable");
  });

  it("ignores an unknown kind value (forward-compat)", () => {
    const raw = JSON.stringify({ message: "x", data: { kind: "weird-future-kind" } });
    expect(new RpcError(raw).kind).toBeUndefined();
  });

  it("degrades gracefully for a bare non-JSON string (no envelope)", () => {
    const e = new RpcError("boom, not json");
    expect(e.message).toBe("boom, not json");
    expect(e.kind).toBeUndefined();
    expect(e.headline).toBeUndefined();
    expect(e.detail).toBeUndefined();
    expect(e.cancelled).toBe(false);
  });

  it("still detects cancellation", () => {
    const raw = JSON.stringify({ message: "cancelled", data: { cancelled: true } });
    expect(new RpcError(raw).cancelled).toBe(true);
  });
});

describe("rpc() retry capture", () => {
  it("attaches the failed method + params so a retryable error can re-issue", async () => {
    (invoke as ReturnType<typeof vi.fn>).mockRejectedValueOnce(
      JSON.stringify({ message: "timed out", data: { kind: "retryable" } }),
    );
    await expect(rpc("find_duplicates", { path: "D:/Music" })).rejects.toMatchObject({
      method: "find_duplicates",
      params: { path: "D:/Music" },
      kind: "retryable",
    });
  });

  it("captures empty params as {} (never undefined)", async () => {
    (invoke as ReturnType<typeof vi.fn>).mockRejectedValueOnce("some transport string");
    await expect(rpc("ping")).rejects.toMatchObject({ method: "ping", params: {} });
  });
});
