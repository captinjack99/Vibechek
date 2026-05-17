/**
 * Settings persistence: load config from disk on app start, auto-save on
 * change (debounced 500ms so we don't hit the disk for every slider tick).
 *
 * Mount this hook ONCE at the root of the app (currently App.tsx).
 */

import { useEffect, useRef } from "react";

import { useConfigStore } from "../stores";
import { rpc } from "./useSidecar";
import type { VibechekConfig } from "../types";

const SAVE_DEBOUNCE_MS = 500;

export function useConfigPersistence() {
  const config = useConfigStore((s) => s.config);
  const loaded = useConfigStore((s) => s.loaded);
  const setConfig = useConfigStore((s) => s.setConfig);

  const saveTimer = useRef<number | null>(null);

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
      rpc("save_config", { config }).catch(() => {
        /* surfaced via global error toast */
      });
    }, SAVE_DEBOUNCE_MS);
    return () => {
      if (saveTimer.current !== null) {
        window.clearTimeout(saveTimer.current);
      }
    };
  }, [config, loaded]);
}
