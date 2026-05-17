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

/** Round-trip a JSON-RPC call through the Tauri shell. */
export async function rpc<T = unknown>(method: string, params?: object): Promise<T> {
  return invoke<T>("rpc_call", { method, params: params ?? {} });
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
