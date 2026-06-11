/**
 * Operation store — tracks the currently-running long op (analyze, dedupe,
 * organize, tag, backup, download-models) plus its progress and last error.
 *
 * Split out of stores/index.ts. Re-exported from `../stores` for backwards
 * compatibility.
 */

import { create } from "zustand";

import type { DuplicateReport, OrganizePlan, ProgressEvent } from "../types";

/**
 * Generate a client-side correlation id for a long op. Sent to the sidecar as
 * `op_id`; the sidecar echoes it on every progress notification the op emits,
 * which lets consumers attribute events on the shared stream to the exact
 * operation instance (see `progressMatches`).
 */
export function newOpId(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  // Non-secure-context fallback (tests / odd embeds) — uniqueness within one
  // app session is all that's required.
  return `op-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

/**
 * True iff a progress event should be attributed to the op `opId`.
 *
 * Drops an event only on a POSITIVE mismatch — both sides carry an id and
 * they differ. Unstamped events (legacy sidecar, ops started without an id)
 * and consumers with no active id keep today's permissive behavior, so the
 * filter can roll out incrementally without silencing anything.
 */
export function progressMatches(
  evt: ProgressEvent,
  opId: string | null | undefined,
): boolean {
  return !evt.op_id || !opId || evt.op_id === opId;
}

export type OperationKind =
  | "analyze"
  | "dedupe"
  | "organize"
  | "tag"
  | "backup"
  | "download-models"
  | "install-wsl"
  | "install-essentia"
  | "install-cuda"
  | "revert"
  | null;

interface OperationState {
  active: OperationKind;
  /** Correlation id of the active op — what `begin()` generated. Pass it to
   *  the api wrapper so the sidecar echoes it on progress events. */
  opId: string | null;
  progress: ProgressEvent | null;
  startedAt: number | null;
  error: string | null;

  duplicateReport: DuplicateReport | null;
  organizePlan: OrganizePlan | null;

  /** Mark an op active and return its correlation id (thread it into the RPC
   *  call's `op_id` so progress events can be attributed back to this op). */
  begin: (kind: Exclude<OperationKind, null>) => string;
  setProgress: (p: ProgressEvent) => void;
  finish: () => void;
  /** Set the error state. User-cancellations are detected and silently dropped. */
  fail: (error: unknown) => void;
  clearError: () => void;

  setDuplicateReport: (r: DuplicateReport | null) => void;
  setOrganizePlan: (p: OrganizePlan | null) => void;
}

export const useOperationStore = create<OperationState>((set) => ({
  active: null,
  opId: null,
  progress: null,
  startedAt: null,
  error: null,

  duplicateReport: null,
  organizePlan: null,

  begin: (kind) => {
    const opId = newOpId();
    set({ active: kind, opId, progress: null, error: null, startedAt: Date.now() });
    return opId;
  },
  setProgress: (p) => set({ progress: p }),
  finish: () => set({ active: null, opId: null, progress: null, startedAt: null }),
  // `fail(error)` is what every component's catch handler calls. Two important
  // behaviors:
  //
  //   1. Cancellations exit silently. A user clicking Cancel is not a failure;
  //      we detect RpcError.cancelled (typed flag) AND the legacy string forms
  //      (in case any caller did `fail(String(e))` and lost the typed flag).
  //
  //   2. RpcError messages get cleaned up. The raw form is the Python error
  //      message ("Invalid params: ..."), which is JSON noise to non-devs.
  //      We strip the "Invalid params:" prefix and prepend the method name
  //      where the RpcError carries one. The full raw JSON stays on `error`
  //      for the LogsViewer / clipboard copy path.
  fail: (error) => {
    // Cancellation detection. `RpcError.cancelled` is the reliable signal
    // (set from the server's structured `data.cancelled`). We also accept a
    // raw JSON string carrying `"cancelled":true` for the rare path where a
    // caller passed an unparsed error string. We deliberately do NOT do a
    // loose `includes("cancelled by user")` substring match anymore — a
    // genuine sidecar-death error whose message happened to contain that
    // phrase (e.g. an install path) would be wrongly suppressed.
    const cancelled =
      (typeof error === "object" && error !== null && (error as any).cancelled === true) ||
      (typeof error === "string" && error.includes('"cancelled":true'));
    if (cancelled) {
      set({ active: null, opId: null, progress: null, startedAt: null, error: null });
      return;
    }

    // Build a user-readable message. RpcError exposes `.message` which is
    // the server-side error text without the JSON envelope.
    let msg: string;
    if (typeof error === "object" && error !== null && "message" in error) {
      msg = String((error as { message: unknown }).message);
      // Strip the noisy "Invalid params:" / "Application error:" prefixes —
      // they're for protocol-level debugging, not end users.
      msg = msg.replace(/^Invalid params:\s*/, "")
               .replace(/^Application error:\s*/, "")
               .replace(/^sidecar error:\s*/, "");
    } else {
      msg = String(error);
    }

    set({ active: null, opId: null, progress: null, startedAt: null, error: msg });
  },
  clearError: () => set({ error: null }),

  setDuplicateReport: (r) => set({ duplicateReport: r }),
  setOrganizePlan: (p) => set({ organizePlan: p }),
}));
