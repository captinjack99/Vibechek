/**
 * Operation store — tracks the currently-running long op (analyze, dedupe,
 * organize, tag, backup, download-models) plus its progress and last error.
 *
 * Split out of stores/index.ts. Re-exported from `../stores` for backwards
 * compatibility.
 */

import { create } from "zustand";

import type { DuplicateReport, OrganizePlan, ProgressEvent } from "../types";
import { RpcError, type RpcErrorKind } from "../hooks/useSidecar";

/**
 * A classified operation failure: a plain, user-facing headline plus the
 * technical detail to demote behind a toggle, the envelope `kind` (which drives
 * ErrorToast's Retry/Restart affordance), and — for a retryable error — the
 * exact call to re-issue.
 *
 * Note this store never *calls* rpc; it only records the (method, params) so a
 * component (ErrorToast) can re-issue on the user's click. Stores stay
 * transport-free.
 */
export interface OperationError {
  /** Plain, user-facing headline — also what the inline per-view banners show
   *  (they read the sibling `error` string). */
  headline: string;
  /** Technical detail for the toast's <details> toggle. Absent when there's
   *  nothing worth demoting. */
  detail?: string;
  /** Envelope error class: `retryable` → Try again, `engine_dead` → Restart. */
  kind?: RpcErrorKind;
  /** Full raw error string (the JSON envelope when structured) for Copy /
   *  Report — technical identifiers are demoted, never deleted. */
  raw: string;
  /** For a retryable error, the exact call to re-issue on "Try again". */
  retry?: { method: string; params: object };
  /** For an engine death mid-analyze: how many tracks had been analyzed when
   *  the service stopped (read from the last progress frame). */
  analyzedCount?: number;
  analyzedTotal?: number;
}

/** Strip JSON-RPC protocol prefixes that are debugging noise, not user text. */
function cleanMessage(msg: string): string {
  return msg
    .replace(/^Invalid params:\s*/, "")
    .replace(/^Application error:\s*/, "")
    .replace(/^Error invoking remote method '[^']+':\s*/, "")
    .trim();
}

/**
 * Turn any thrown value into a structured {@link OperationError}. Prefers the
 * envelope's `headline`/`detail`/`kind` (present on both Python errors and the
 * Rust transport envelope); degrades gracefully to the cleaned message when the
 * envelope is absent (legacy / unstructured errors).
 */
function classifyError(error: unknown): OperationError {
  if (error instanceof RpcError) {
    const headline = error.headline ?? cleanMessage(error.message);
    let detail = error.detail;
    if (!detail) {
      // No structured detail — fall back to whatever technical text we have so
      // it's still demote-able (never deleted; bug reports need it).
      const parts: string[] = [];
      if (error.message && error.message !== headline) parts.push(error.message);
      const tb = error.data?.traceback;
      if (typeof tb === "string" && tb) parts.push(tb);
      detail = parts.length ? parts.join("\n\n") : undefined;
    }
    const retry =
      error.kind === "retryable" && error.method
        ? { method: error.method, params: error.params ?? {} }
        : undefined;
    return { headline, detail, kind: error.kind, raw: error.raw, retry };
  }
  if (typeof error === "object" && error !== null && "message" in error) {
    const msg = cleanMessage(String((error as { message: unknown }).message));
    return { headline: msg, raw: msg };
  }
  const s = String(error);
  return { headline: cleanMessage(s), raw: s };
}

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
  /** Plain user-facing headline of the last failure (also shown by the inline
   *  per-view banners). `null` when there's no error. */
  error: string | null;
  /** Structured form of the same failure — headline + detail + kind + retry.
   *  Non-null exactly when `error` is non-null. ErrorToast reads this. */
  errorInfo: OperationError | null;

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

export const useOperationStore = create<OperationState>((set, get) => ({
  active: null,
  opId: null,
  progress: null,
  startedAt: null,
  error: null,
  errorInfo: null,

  duplicateReport: null,
  organizePlan: null,

  begin: (kind) => {
    const opId = newOpId();
    set({
      active: kind,
      opId,
      progress: null,
      error: null,
      errorInfo: null,
      startedAt: Date.now(),
    });
    return opId;
  },
  setProgress: (p) => set({ progress: p }),
  finish: () => set({ active: null, opId: null, progress: null, startedAt: null }),
  // `fail(error)` is what every component's catch handler calls. Three things:
  //
  //   1. Cancellations exit silently. A user clicking Cancel is not a failure;
  //      we detect RpcError.cancelled (typed flag) AND the legacy string forms
  //      (in case any caller did `fail(String(e))` and lost the typed flag).
  //
  //   2. The error is CLASSIFIED into a plain headline + a technical detail +
  //      a kind (retryable / engine_dead / fatal) via `classifyError`. The
  //      headline lands on `error` (what the inline banners show); the full
  //      structured form on `errorInfo` (what ErrorToast renders, incl. the
  //      Retry/Restart action). The raw string is preserved for Copy / Report.
  //
  //   3. Mid-analyze engine deaths get the "N tracks were analyzed" count read
  //      from the last progress frame — captured here before we clear it.
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
      set({
        active: null,
        opId: null,
        progress: null,
        startedAt: null,
        error: null,
        errorInfo: null,
      });
      return;
    }

    const prev = get();
    const info = classifyError(error);

    // Mid-analyze death: surface how many tracks had been analyzed. The last
    // progress frame is the most reliable count we hold, and fail() is about to
    // clear it. (These tracks were analyzed, not necessarily saved to disk —
    // the toast phrases it honestly.)
    if (prev.active === "analyze" && prev.progress && prev.progress.current > 0) {
      info.analyzedCount = prev.progress.current;
      if (prev.progress.total > 0) info.analyzedTotal = prev.progress.total;
    }

    set({
      active: null,
      opId: null,
      progress: null,
      startedAt: null,
      error: info.headline,
      errorInfo: info,
    });
  },
  clearError: () => set({ error: null, errorInfo: null }),

  setDuplicateReport: (r) => set({ duplicateReport: r }),
  setOrganizePlan: (p) => set({ organizePlan: p }),
}));
