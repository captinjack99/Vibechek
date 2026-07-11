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
  // The unload handlers are registered ONCE (empty-deps effect) and would
  // otherwise close over the first render's config / loaded. Mirror the latest
  // values into refs so a flush-on-quit saves what's actually on screen.
  const latestConfig = useRef(config);
  const loadedRef = useRef(loaded);
  useEffect(() => {
    latestConfig.current = config;
  }, [config]);
  useEffect(() => {
    loadedRef.current = loaded;
  }, [loaded]);

  // Fire a save immediately (used by both the debounce timer and the
  // flush-on-teardown). Takes the config explicitly so the teardown handler
  // never persists a stale closure value.
  const saveConfig = (cfg: VibechekConfig) => {
    rpc("save_config", { config: cfg }).catch((e) => {
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
  };

  // Load on mount — single shot.
  useEffect(() => {
    let cancelled = false;
    // get_config attaches `config_warnings` at the transport level (NOT a config
    // field): the notes the loader collected while snapping invalid / cross-
    // platform saved values back to defaults. Surface them once so a silently
    // reverted setting doesn't masquerade as the user's own choice.
    rpc<VibechekConfig & { config_warnings?: string[] }>("get_config")
      .then((c) => {
        if (cancelled) return;
        const { config_warnings, ...cfg } = c;
        setConfig(cfg as VibechekConfig, /* markLoaded */ true);
        if (config_warnings && config_warnings.length > 0) {
          useNotificationStore.getState().notify(
            "Some saved settings were invalid and were reset",
            { kind: "info", detail: config_warnings.join("\n") },
          );
        }
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
      saveTimer.current = null; // cleared BEFORE the save so flush sees "no pending"
      saveConfig(config);
    }, SAVE_DEBOUNCE_MS);
    return () => {
      if (saveTimer.current !== null) {
        window.clearTimeout(saveTimer.current);
      }
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [config, loaded]);

  // Flush a still-pending save when the window is being torn down. The 500ms
  // debounce otherwise swallows the user's VERY LAST change on quit — they
  // change a setting and immediately Cmd/Alt-F4, the timer never fires, and the
  // tweak is silently lost. beforeunload is the hard-close signal; visibility
  // change→hidden fires earlier and more reliably (some platforms skip
  // beforeunload), giving the fire-and-forget save the best chance to land.
  useEffect(() => {
    const flush = () => {
      if (saveTimer.current === null) return; // nothing pending
      window.clearTimeout(saveTimer.current);
      saveTimer.current = null;
      if (loadedRef.current) saveConfig(latestConfig.current);
    };
    const onVisibility = () => {
      if (document.visibilityState === "hidden") flush();
    };
    window.addEventListener("beforeunload", flush);
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      window.removeEventListener("beforeunload", flush);
      document.removeEventListener("visibilitychange", onVisibility);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
}
