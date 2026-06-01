/**
 * Auto-update hook.
 *
 * Wraps `@tauri-apps/plugin-updater` + `@tauri-apps/plugin-process` behind a
 * small, typed state machine the Settings panel can drive with a single button.
 *
 * --- Dev / non-Tauri safety ---
 * `check()` and friends throw at runtime when there's no Tauri IPC (e.g. the
 * Vite dev server in a plain browser, or vitest). Every entry point here is
 * guarded by `isTauri()`, so the hook no-ops gracefully instead of crashing —
 * the app still builds, type-checks, and runs in the browser.
 *
 * --- Flow ---
 *   idle → (checkForUpdate) → checking
 *     → "no update"  : available=false, back to idle-ish
 *     → "update"     : available=true, holds the Update handle
 *   available → (downloadAndInstall) → downloading → installing → relaunch
 *     → (relaunch never resolves; the app restarts)
 */

import { useCallback, useRef, useState } from "react";
import { isTauri } from "@tauri-apps/api/core";
// Type-only import — erased at compile time, so it never pulls the plugin's
// runtime module into a non-Tauri bundle path. The actual plugin functions are
// lazy-imported (dynamic import) inside isTauri()-guarded code below.
import type { Update } from "@tauri-apps/plugin-updater";

/** Where the update flow currently is. Drives the Settings button copy. */
export type UpdaterPhase =
  | "idle"
  | "checking"
  | "available"
  | "up-to-date"
  | "downloading"
  | "installing"
  | "error";

export interface UpdaterState {
  phase: UpdaterPhase;
  /** Version string of the pending update, when one is available. */
  version: string | null;
  /** Release notes / body from `latest.json`, when present. */
  notes: string | null;
  /** Human-readable error message when phase === "error". */
  error: string | null;
}

const INITIAL: UpdaterState = {
  phase: "idle",
  version: null,
  notes: null,
  error: null,
};

function errMessage(e: unknown): string {
  if (typeof e === "object" && e !== null && "message" in e) {
    return String((e as { message: unknown }).message);
  }
  return String(e);
}

/**
 * Imperative updater controller. Lazy-imports the plugins so a non-Tauri build
 * never pulls the Tauri IPC modules into the initial bundle path at call time.
 */
export function useUpdater() {
  const [state, setState] = useState<UpdaterState>(INITIAL);

  // The plugin's `Update` handle. Held across check → install without
  // re-checking. `Update | null` is a type-only reference (erased at runtime).
  const updateRef = useRef<Update | null>(null);

  /** True when running inside the Tauri shell (false in dev browser / tests). */
  const supported = isTauri();

  const checkForUpdate = useCallback(async () => {
    if (!isTauri()) {
      // Dev/browser: nothing to check. Surface a benign state, no throw.
      setState({ ...INITIAL, phase: "up-to-date" });
      return;
    }
    setState({ phase: "checking", version: null, notes: null, error: null });
    try {
      const { check } = await import("@tauri-apps/plugin-updater");
      const update = await check();
      if (update) {
        updateRef.current = update;
        setState({
          phase: "available",
          version: update.version,
          notes: update.body ?? null,
          error: null,
        });
      } else {
        updateRef.current = null;
        setState({ ...INITIAL, phase: "up-to-date" });
      }
    } catch (e) {
      updateRef.current = null;
      setState({ ...INITIAL, phase: "error", error: errMessage(e) });
    }
  }, []);

  const downloadAndInstall = useCallback(async () => {
    if (!isTauri()) return;
    const update = updateRef.current;
    if (!update) return;
    setState((s) => ({ ...s, phase: "downloading", error: null }));
    try {
      await update.downloadAndInstall((event) => {
        // "Started" → "Progress" (repeated) → "Finished". Once finished, the
        // bundle is staged; flip to "installing" before relaunch.
        if (event.event === "Finished") {
          setState((s) => ({ ...s, phase: "installing" }));
        }
      });
      // Install is applied on relaunch. process::relaunch restarts the app.
      const { relaunch } = await import("@tauri-apps/plugin-process");
      await relaunch();
    } catch (e) {
      setState((s) => ({ ...s, phase: "error", error: errMessage(e) }));
    }
  }, []);

  const reset = useCallback(() => {
    updateRef.current = null;
    setState(INITIAL);
  }, []);

  return { state, supported, checkForUpdate, downloadAndInstall, reset };
}
