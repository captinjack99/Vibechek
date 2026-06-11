/**
 * Notification store — transient, auto-dismissing success/info/warning toasts.
 *
 * Different from useOperationStore.error (which is sticky, scary, and
 * reserved for operation failures). Notifications are the "done" pat on
 * the back — and "warning" carries non-fatal caution (e.g. the sidecar's
 * risky-install-path notice) without dressing it as cheerful info.
 *
 * Split out of stores/index.ts. Re-exported from `../stores` for
 * backwards compatibility.
 */

import { create } from "zustand";

export type NotificationKind = "success" | "info" | "warning";

export interface Notification {
  id: number;
  kind: NotificationKind;
  message: string;
  /** Optional secondary line — shows under the main message. */
  detail?: string;
}

interface NotificationState {
  items: Notification[];
  notify: (message: string, opts?: { kind?: NotificationKind; detail?: string }) => void;
  dismiss: (id: number) => void;
}

let nextNotificationId = 1;

/** Cap the visible stack — a rapid burst of ops used to pile toasts without
 * bound; the oldest drop off first. */
const MAX_STACK = 5;

export const useNotificationStore = create<NotificationState>((set) => ({
  items: [],
  notify: (message, opts) => {
    const id = nextNotificationId++;
    const item: Notification = {
      id,
      kind: opts?.kind ?? "success",
      message,
      detail: opts?.detail,
    };
    set((s) => ({ items: [...s.items, item].slice(-MAX_STACK) }));
  },
  dismiss: (id) =>
    set((s) => ({ items: s.items.filter((n) => n.id !== id) })),
}));
