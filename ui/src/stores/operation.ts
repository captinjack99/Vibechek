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
  /** For a retryable error, the exact call to re-issue on "Try again".
   *  `kind` is the operation that was running when it failed (when there was
   *  one) — ErrorToast re-issues under `begin(kind)` so the replay gets the
   *  progress panel and the Cancel button instead of running invisibly. */
  retry?: { method: string; params: object; kind?: Exclude<OperationKind, null> };
  /** A component-owned retry closure for a failure whose result must land back
   *  in a store (so the generic `retry` re-issue — which discards the result —
   *  can't work). Created by the component (stores stay transport-free) and
   *  passed to `fail(error, { retryAction })`; ErrorToast prefers it over
   *  `retry`. The closure owns its own error handling (it calls `fail()` on a
   *  fresh failure), so callers must not double-wrap it. */
  retryAction?: () => Promise<void>;
  /** For an engine death mid-analyze: how many tracks had been analyzed when
   *  the service stopped (read from the last progress frame). */
  analyzedCount?: number;
  analyzedTotal?: number;
  /** Machine-readable recovery affordances the backend attached to `error.data`
   *  (see vibechek/errors.py `options`). Each flag turns on ITS action button in
   *  the ErrorToast — a memory refusal carries `canSwitchClassifier` /
   *  `canIncreaseMemory`; a WSL-missing failure carries `canInstallWsl`. */
  options?: {
    canSwitchClassifier?: boolean;
    canIncreaseMemory?: boolean;
    canInstallWsl?: boolean;
  };
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
 * Methods the generic rpc-replay retry must NEVER re-issue on its own.
 *
 * The generic path (ErrorToast → `rpc(method, params)`) throws the resolved
 * payload away, so replaying a call whose RESULT has to land in a store gives
 * the user a button that appears to do nothing: `analyze_directory` — the
 * dominant producer of `kind:"retryable"` — would re-run for hours and then
 * drop the AnalysisReport (no tracks, no completion toast, no persist_error
 * warning). The mutating ones would additionally re-move / re-tag / re-trash
 * files with no confirmation.
 *
 * These sites own their retry: they pass `fail(e, { retryAction })`, a closure
 * that re-enters the real code path (begin() → RPC → store write). A method
 * listed here with no `retryAction` simply gets no Try-again button — no
 * button is better than one that lies.
 */
const NON_REPLAYABLE_METHODS = new Set([
  "analyze_directory",
  "scan_directory",
  "scan_only",
  "find_duplicates",
  "handle_duplicates",
  "plan_organization",
  "organize",
  "apply_ml_tags",
  "backup_tags",
  "restore_tags",
  "restore_tags_with_remap",
]);

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
      error.kind === "retryable" &&
      error.method &&
      !NON_REPLAYABLE_METHODS.has(error.method)
        ? { method: error.method, params: error.params ?? {} }
        : undefined;
    // Pull the backend's recovery-option flags off `error.data` (merged there at
    // the top level by UserFacingError.to_error_data). Only build `options` when
    // at least one is set so an ordinary error stays flag-free.
    const d = error.data ?? {};
    const options =
      d.can_switch_classifier === true ||
      d.can_increase_memory === true ||
      d.can_install_wsl === true
        ? {
            canSwitchClassifier: d.can_switch_classifier === true,
            canIncreaseMemory: d.can_increase_memory === true,
            canInstallWsl: d.can_install_wsl === true,
          }
        : undefined;
    return { headline, detail, kind: error.kind, raw: error.raw, retry, options };
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

/**
 * Every operation kind, as a RUNTIME value — the single source of truth the
 * `OperationKind` type below is derived from.
 *
 * It exists so a test can iterate the real set instead of a hand-mirrored
 * literal: a subset of a union is assignable to `OperationKind[]`, so the copy
 * in AnalysisProgress.test.tsx silently stayed valid when a member was added
 * and could never catch the missing label it claimed to guard.
 */
export const OPERATION_KINDS = [
  "analyze",
  "dedupe",
  "organize",
  "tag",
  "backup",
  "download-models",
  "install-wsl",
  "install-essentia",
  "install-cuda",
  "revert",
  // The DESTRUCTIVE half of dedupe (move / send-to-trash), distinct from the
  // read-only "dedupe" scan so the progress overlay can't label an irreversible
  // delete "Finding duplicates". Matches the backend kind in vibechek/rpc.py's
  // _CANCELLABLE_METHODS.
  "dedupe-handle",
] as const;

export type OperationKind = (typeof OPERATION_KINDS)[number] | null;

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
  /**
   * Fingerprint of the parameters `organizePlan` was previewed with, or null
   * when the plan carries none.
   *
   * It lives HERE, next to the plan, because the plan outlives the OrganizeView
   * component (switching tabs unmounts the view but the plan stays in this
   * store). While the key was component-local `useState` it reset to null on
   * every remount, permanently disarming the staleness check — and organize is
   * a destructive, no-undo bulk move, so the confirmed preview MUST match what
   * executes.
   */
  organizePlanKey: string | null;

  /** Mark an op active and return its correlation id (thread it into the RPC
   *  call's `op_id` so progress events can be attributed back to this op). */
  begin: (kind: Exclude<OperationKind, null>) => string;
  setProgress: (p: ProgressEvent) => void;
  finish: () => void;
  /** Set the error state. User-cancellations are detected and silently dropped.
   *  `extras.retryAction` attaches a component-owned retry closure (and defaults
   *  the kind to "retryable" when the error carried none) — for failures whose
   *  result must land in a store, which the generic `retry` path can't do. */
  fail: (error: unknown, extras?: { retryAction?: () => Promise<void> }) => void;
  clearError: () => void;

  setDuplicateReport: (r: DuplicateReport | null) => void;
  /**
   * Store the previewed plan together with the parameter fingerprint it was
   * built from. Omitting `paramsKey` (or clearing the plan) leaves the plan
   * unkeyed — callers gating Execute on the key must treat "no key" as stale.
   */
  setOrganizePlan: (p: OrganizePlan | null, paramsKey?: string | null) => void;
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
  organizePlanKey: null,

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
  fail: (error, extras) => {
    // Cancellation detection. `RpcError.cancelled` is the reliable signal
    // (set from the server's structured `data.cancelled`). We also accept a
    // raw JSON string carrying `"cancelled":true` for the rare path where a
    // caller passed an unparsed error string. We deliberately do NOT do a
    // loose `includes("cancelled by user")` substring match anymore — a
    // genuine sidecar-death error whose message happened to contain that
    // phrase (e.g. an install path) would be wrongly suppressed.
    const cancelled =
      (typeof error === "object" && error !== null && (error as { cancelled?: unknown }).cancelled === true) ||
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

    // Stamp the op that was running onto the generic retry handle. Without it
    // ErrorToast replays the call bare — no begin(), so `active` stays null and
    // the progress panel (and its Cancel button) never appear for a run that
    // can take many minutes.
    if (info.retry && prev.active) info.retry = { ...info.retry, kind: prev.active };

    // Mid-analyze death: surface how many tracks had been analyzed. The last
    // progress frame is the most reliable count we hold, and fail() is about to
    // clear it. (These tracks were analyzed, not necessarily saved to disk —
    // the toast phrases it honestly.)
    if (prev.active === "analyze" && prev.progress && prev.progress.current > 0) {
      info.analyzedCount = prev.progress.current;
      if (prev.progress.total > 0) info.analyzedTotal = prev.progress.total;
    }

    // A component-owned retry closure (for a failure whose result must land back
    // in a store — the generic `retry` re-issue discards the result). Attach it
    // and, since a plain-string reason carries no envelope `kind`, default to
    // "retryable" so ErrorToast renders the Try-again button.
    if (extras?.retryAction) {
      info.retryAction = extras.retryAction;
      info.kind = info.kind ?? "retryable";
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
  setOrganizePlan: (p, paramsKey) =>
    // A cleared plan can't keep a key, and a plan stored without one is
    // explicitly unkeyed — never inherit the previous plan's fingerprint.
    set({ organizePlan: p, organizePlanKey: p ? (paramsKey ?? null) : null }),
}));
