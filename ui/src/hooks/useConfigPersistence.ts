/**
 * Settings persistence: load config from disk on app start, auto-save on
 * change (debounced 500ms so we don't hit the disk for every slider tick).
 *
 * Mount this hook ONCE at the root of the app (currently App.tsx).
 */

import { useEffect, useRef } from "react";

import { useConfigStore, useNotificationStore } from "../stores";
import { isCancellation, rpc } from "./useSidecar";
import type { VibechekConfig } from "../types";

const SAVE_DEBOUNCE_MS = 500;

export function useConfigPersistence() {
  const config = useConfigStore((s) => s.config);
  const loaded = useConfigStore((s) => s.loaded);
  const setConfig = useConfigStore((s) => s.setConfig);

  const saveTimer = useRef<number | null>(null);
  // Throttle the "could not save" toast so a persistent failure (disk full,
  // permission denied) doesn't spam the user every 500ms while they tweak a
  // slider. One notification, then stay quiet for 30s.
  const lastNotifiedAt = useRef<number>(0);

  // Load on mount — single shot.
  useEffect(() => {
    let cancelled = false;
    rpc<VibechekConfig>("get_config")
      .then((c) => {
        if (!cancelled) setConfig(c, /* markLoaded */ true);
      })
      .catch(() => {
        // Stick with defaults; still mark loaded so saves kick in on next change
        if (!cancelled) setConfig(useConfigStore.getState().config, true);
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Debounced auto-save on every config change AFTER initial load.
  useEffect(() => {
    if (!loaded) return; // Don't save defaults over a real on-disk config
    if (saveTimer.current !== null) {
      window.clearTimeout(saveTimer.current);
    }
    saveTimer.current = window.setTimeout(() => {
      rpc("save_config", { config }).catch((e) => {
        // A save failure that gets eaten silently is the worst kind of bug —
        // the user thinks their tweaks stuck. Surface it via a toast (info,
        // not the scary red error toast — the operation will retry on the
        // next change). Cancellations are impossible here but check anyway.
        if (isCancellation(e)) return;
        const now = Date.now();
        if (now - lastNotifiedAt.current < 30_000) return;
        lastNotifiedAt.current = now;
        const msg =
          typeof e === "object" && e !== null && "message" in e
            ? String((e as { message: unknown }).message)
            : String(e);
        useNotificationStore
          .getState()
          .notify(`Settings could not save: ${msg}`, { kind: "info" });
      });
    }, SAVE_DEBOUNCE_MS);
    return () => {
      if (saveTimer.current !== null) {
        window.clearTimeout(saveTimer.current);
      }
    };
  }, [config, loaded]);
}
