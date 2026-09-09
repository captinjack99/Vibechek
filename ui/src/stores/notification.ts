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

/** An inline action button rendered in the toast (e.g. "Open install folder"). */
export interface NotificationAction {
  label: string;
  onClick: () => void;
}

export interface Notification {
  id: number;
  kind: NotificationKind;
  message: string;
  /** Optional secondary line — shows under the main message. */
  detail?: string;
  /**
   * When true, the toast NEVER auto-dismisses — only a click (the X, or the
   * action button) removes it. For app-breaking conditions that must not vanish
   * after a few seconds (e.g. the risky-install-path warning), where an
   * auto-dismiss would leave the user with no record of a launch-hang risk.
   */
  persistent?: boolean;
  /** Optional inline action giving the user a next step in-view. */
  action?: NotificationAction;
}

interface NotificationState {
  items: Notification[];
  /**
   * Push a toast; returns its `id` so a caller that owns a STANDING condition
   * can take its own warning back down once the condition clears (see
   * useConfigPersistence: a transient config.json lock must not leave a
   * persistent toast whose only button quarantines an intact settings file).
   *
   * The id is a handle, not a guarantee: the toast can also go away via the
   * user's X or the hard stack ceiling, so check it is still in `items` before
   * concluding your warning is still up.
   */
  notify: (
    message: string,
    opts?: {
      kind?: NotificationKind;
      detail?: string;
      persistent?: boolean;
      action?: NotificationAction;
    },
  ) => number;
  dismiss: (id: number) => void;
}

let nextNotificationId = 1;

/** Cap the visible stack — a rapid burst of ops used to pile toasts without
 * bound; the oldest drop off first. */
const MAX_STACK = 5;

/** Absolute ceiling, persistent toasts included. `persistent` items are never
 * evicted to make room for an ordinary toast, so a runaway producer of them
 * would otherwise grow the stack without bound. */
const HARD_MAX_STACK = 10;

export const useNotificationStore = create<NotificationState>((set) => ({
  items: [],
  notify: (message, opts) => {
    const id = nextNotificationId++;
    const item: Notification = {
      id,
      kind: opts?.kind ?? "success",
      message,
      detail: opts?.detail,
      persistent: opts?.persistent,
      action: opts?.action,
    };
    set((s) => {
      const next = [...s.items, item];
      // Evict oldest-first, but never a `persistent` toast: that flag means
      // "app-breaking, must not vanish unclicked", and since ordinary toasts
      // self-dismiss after a few seconds a persistent item is ALWAYS the oldest
      // entry — a plain slice(-MAX_STACK) dropped exactly the one toast it had
      // to keep (the launch-hang warning, action button and all). The item we
      // just pushed is exempt too; it's the one the user needs to read now.
      while (next.length > MAX_STACK) {
        const idx = next.slice(0, -1).findIndex((n) => !n.persistent);
        if (idx === -1) break; // nothing evictable — the hard ceiling applies
        next.splice(idx, 1);
      }
      // The hard ceiling is absolute: a `persistent` item CAN be dropped here.
      // Nothing else is left to drop — the pass above already evicts every
      // ordinary toast below the newest — and without it a runaway producer of
      // persistent toasts grows the stack without bound. So it stays a blunt
      // slice, keeping the newest.
      //
      // Which means an id returned by `notify` is NOT a durable handle. A
      // caller holding one for a standing condition must check it is still in
      // `items` before treating its warning as up; a stale id read as "still
      // showing" silences the condition instead (useConfigPersistence re-probes
      // the settings file only while its warning is genuinely on screen).
      return { items: next.length > HARD_MAX_STACK ? next.slice(-HARD_MAX_STACK) : next };
    });
    return id;
  },
  dismiss: (id) =>
    set((s) => ({ items: s.items.filter((n) => n.id !== id) })),
}));
