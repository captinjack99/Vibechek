/**
 * React hook for invoking the Python sidecar.
 *
 * `rpc(method, params)` round-trips through Tauri to the sidecar and resolves
 * with the result. `useSidecarProgress(handler)` subscribes to progress
 * notifications emitted while a long-running call is in flight.
 */

import { invoke } from "@tauri-apps/api/core";
import { listen, type UnlistenFn } from "@tauri-apps/api/event";
import { useEffect, useRef } from "react";

import type { ProgressEvent } from "../types";

/** Error class from the structured error envelope. Both the Python sidecar
 *  and the Rust transport layer now emit `data.kind` — it drives the recovery
 *  action ErrorToast offers. */
export type RpcErrorKind = "retryable" | "engine_dead" | "fatal";

/**
 * Structured error from a JSON-RPC call. The Rust shell stringifies the
 * error object so we get it as a JSON string in `e.message`; we parse it
 * back here so call sites can branch on `cancelled` / `kind` etc. without
 * string matching.
 *
 * The envelope shape (shared by Python errors AND Rust transport errors) is:
 *
 *     { message, code, data: { kind?, headline?, detail?, cancelled?, ... } }
 *
 * `headline` is plain, user-facing text; `detail` is the technical string to
 * demote behind a toggle. All of `kind`/`headline`/`detail` are optional — a
 * legacy or unstructured error simply leaves them undefined and consumers fall
 * back to today's behavior.
 */
export class RpcError extends Error {
  code: number;
  data?: {
    cancelled?: boolean;
    traceback?: string;
    kind?: string;
    headline?: string;
    detail?: string;
    [k: string]: unknown;
  };
  /** True if the operation was cancelled by the user (not a real failure). */
  cancelled: boolean;
  /** Error class from the envelope (`retryable` / `engine_dead` / `fatal`). */
  kind?: RpcErrorKind;
  /** Plain, user-facing headline from the envelope, when present. */
  headline?: string;
  /** Technical detail from the envelope (the demote target), when present. */
  detail?: string;
  /** Raw JSON string, kept for diagnostics / bug reports. */
  raw: string;
  /** The method + params that produced this error, captured by `rpc()` so a
   *  retryable error can re-issue the exact call with no per-component wiring
   *  (the shared transport wrapper is the single choke point). */
  method?: string;
  params?: object;

  constructor(raw: string) {
    let parsed: { message?: string; code?: number; data?: any } = {};
    try {
      parsed = JSON.parse(raw);
    } catch {
      // Not JSON — a bare transport string. Shouldn't happen now the Rust side
      // emits the structured envelope, but stay defensive.
    }
    super(parsed.message ?? raw);
    this.name = "RpcError";
    this.code = parsed.code ?? -1;
    this.data = parsed.data;
    this.cancelled = !!parsed.data?.cancelled;
    const k = parsed.data?.kind;
    this.kind =
      k === "retryable" || k === "engine_dead" || k === "fatal" ? k : undefined;
    this.headline =
      typeof parsed.data?.headline === "string" ? parsed.data.headline : undefined;
    this.detail =
      typeof parsed.data?.detail === "string" ? parsed.data.detail : undefined;
    this.raw = raw;
  }
}

/** Round-trip a JSON-RPC call through the Tauri shell. */
export async function rpc<T = unknown>(method: string, params?: object): Promise<T> {
  try {
    return await invoke<T>("rpc_call", { method, params: params ?? {} });
  } catch (e) {
    // Tauri rejects with a plain string for Result<_, String> commands.
    const err = new RpcError(typeof e === "string" ? e : String(e));
    // Capture the failed call so a retryable error's "Try again" can re-issue
    // it (A2) without any per-component retry plumbing.
    err.method = method;
    err.params = params ?? {};
    throw err;
  }
}

/** True iff `e` is an RpcError representing a user-initiated cancellation. */
export function isCancellation(e: unknown): boolean {
  return e instanceof RpcError && e.cancelled;
}

/** Read-only info about the sidecar process (binary path, etc.). */
export async function sidecarStatus(): Promise<{ binary: string }> {
  return invoke<{ binary: string }>("sidecar_status");
}

/**
 * Subscribe to `sidecar:progress` events while the component is mounted.
 * Handler is captured by ref so callers don't need to memoize it.
 */
export function useSidecarProgress(handler: (e: ProgressEvent) => void): void {
  const handlerRef = useRef(handler);
  handlerRef.current = handler;

  useEffect(() => {
    let unlisten: UnlistenFn | undefined;
    let cancelled = false;

    listen<ProgressEvent>("sidecar:progress", (evt) => {
      handlerRef.current(evt.payload);
    }).then((u) => {
      if (cancelled) u();
      else unlisten = u;
    });

    return () => {
      cancelled = true;
      unlisten?.();
    };
  }, []);
}

/** Generic event subscription for any `sidecar:*` event (ready, complete, etc.). */
export function useSidecarEvent<T>(name: string, handler: (payload: T) => void): void {
  const handlerRef = useRef(handler);
  handlerRef.current = handler;

  useEffect(() => {
    let unlisten: UnlistenFn | undefined;
    let cancelled = false;

    listen<T>(`sidecar:${name}`, (evt) => {
      handlerRef.current(evt.payload);
    }).then((u) => {
      if (cancelled) u();
      else unlisten = u;
    });

    return () => {
      cancelled = true;
      unlisten?.();
    };
  }, [name]);
}
