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

/**
 * Structured error from a JSON-RPC call. The Rust shell stringifies the
 * error object so we get it as a JSON string in `e.message`; we parse it
 * back here so call sites can branch on `cancelled` etc. without string
 * matching.
 */
export class RpcError extends Error {
  code: number;
  data?: { cancelled?: boolean; traceback?: string; [k: string]: unknown };
  /** True if the operation was cancelled by the user (not a real failure). */
  cancelled: boolean;
  /** Raw JSON string, kept for diagnostics. */
  raw: string;

  constructor(raw: string) {
    let parsed: { message?: string; code?: number; data?: any } = {};
    try {
      parsed = JSON.parse(raw);
    } catch {
      // Not JSON — likely a sidecar transport error from the Rust side.
    }
    super(parsed.message ?? raw);
    this.name = "RpcError";
    this.code = parsed.code ?? -1;
    this.data = parsed.data;
    this.cancelled = !!parsed.data?.cancelled;
    this.raw = raw;
  }
}

/** Round-trip a JSON-RPC call through the Tauri shell. */
export async function rpc<T = unknown>(method: string, params?: object): Promise<T> {
  try {
    return await invoke<T>("rpc_call", { method, params: params ?? {} });
  } catch (e) {
    // Tauri rejects with a plain string for Result<_, String> commands.
    throw new RpcError(typeof e === "string" ? e : String(e));
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
