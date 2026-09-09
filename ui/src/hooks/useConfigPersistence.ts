/**
 * Settings persistence: load config from disk on app start, auto-save on
 * change (debounced 500ms so we don't hit the disk for every slider tick).
 *
 * Mount this hook ONCE at the root of the app (currently App.tsx).
 */

import { useEffect, useRef } from "react";

import { useConfigStore, useNotificationStore } from "../stores";
import { isCancellation, rpc, RpcError } from "./useSidecar";
import type { VibechekConfig } from "../types";

const SAVE_DEBOUNCE_MS = 500;

/**
 * How long to wait before RE-CHECKING a settings file the sidecar refused to
 * overwrite because it couldn't read it.
 *
 * The overwhelmingly common cause is a transient sharing violation — an AV
 * scan, or OneDrive / Google Drive holding config.json open for a sync read —
 * which clears in well under a second. Long enough to outlast that, short
 * enough that a genuinely broken file still gets its warning while the user is
 * looking at the setting they just changed.
 */
const REFUSAL_RECHECK_MS = 750;

/**
 * How often to RE-CHECK the settings file while the standing "could not be
 * read" warning is up.
 *
 * The one-shot re-probe above only rules out a sub-second blip. A lock that
 * OUTLASTS it — a long AV sweep, a cloud client re-uploading a big file, an
 * editor holding config.json open — clears on its own with no further user
 * edit, and nothing was re-evaluating the condition: the persistent warning
 * stayed on screen aimed at a file that reads perfectly, offering a button that
 * quarantines it. Slow enough to cost nothing (one cheap RPC, and only while
 * the warning is actually up), fast enough that the warning doesn't outlive
 * the fault by long.
 */
const REFUSAL_POLL_MS = 10_000;

/** JSON-RPC INVALID_PARAMS. Mirrors `vibechek/rpc.py::INVALID_PARAMS`. */
const INVALID_PARAMS = -32602;

/** The message text out of any thrown value (RpcError, Error, or a bare string). */
function errorMessage(e: unknown): string {
  return typeof e === "object" && e !== null && "message" in e
    ? String((e as { message: unknown }).message)
    : String(e);
}

/**
 * True when `save_config` REFUSED because the settings file on disk exists but
 * couldn't be read.
 *
 * `vibechek/rpc.py::_save_config` will not overwrite bytes it can't parse — it
 * raises InvalidParams naming the file instead. That is a different failure
 * from the transient ones (disk full, permission denied): it will fail
 * identically on every keystroke until the file is dealt with, and retrying is
 * not the way out. The code alone would also match the handler's other
 * InvalidParams ("'config' must be an object", a caller bug this hook can't
 * produce), so match the message too and fall through to the generic toast if
 * the wording ever moves.
 */
function isUnreadableConfigRefusal(e: unknown): e is RpcError {
  return (
    e instanceof RpcError &&
    e.code === INVALID_PARAMS &&
    /could not be read/i.test(e.message)
  );
}

export function useConfigPersistence() {
  const config = useConfigStore((s) => s.config);
  const loaded = useConfigStore((s) => s.loaded);
  const setConfig = useConfigStore((s) => s.setConfig);

  const saveTimer = useRef<number | null>(null);
  // Autosave is ARMED only by a load we trust. A failed load (or one the
  // backend flagged as a fallback) leaves the store holding DEFAULT_CONFIG, and
  // the debounced save would write those defaults straight over the user's real
  // config.json 500ms after launch — silently erasing models_dir, target_root,
  // review_folder, thresholds, everything. Until it's armed we save nothing.
  const saveArmed = useRef(false);
  // The config that was on screen when an untrusted load settled. Autosave
  // stays disarmed until `config` differs from it, i.e. until the user
  // deliberately changes a setting — which IS an intent to write.
  const untrustedBaseline = useRef<VibechekConfig | null>(null);
  // Throttle the "could not save" toast so a persistent failure (disk full,
  // permission denied) doesn't spam the user every 500ms while they tweak a
  // slider. One notification, then stay quiet for 30s.
  const lastNotifiedAt = useRef<number>(0);
  // The id of the standing "settings file could not be read" toast, or null
  // when none is up. Held (rather than a bare "did we notify" boolean) because
  // the warning has to come back DOWN by itself: its only action quarantines
  // config.json, so it must never outlive the lock that caused it.
  const refusalToastId = useRef<number | null>(null);
  // True while the post-refusal re-probe is in flight, so a burst of refused
  // autosaves schedules exactly one.
  const refusalProbing = useRef(false);
  // The `setInterval` re-checking the file while the warning stands, or null
  // when the warning is down. Runs ONLY while there is a warning to take back
  // down, and is cleared on unmount.
  const refusalPollTimer = useRef<number | null>(null);
  // True while a poll probe is in flight, so a slow `get_config` doesn't stack
  // probes tick after tick.
  const refusalPolling = useRef(false);
  // The unload handlers are registered ONCE (empty-deps effect) and would
  // otherwise close over the first render's config. Mirror the latest value
  // into a ref so a flush-on-quit saves what's actually on screen.
  const latestConfig = useRef(config);
  useEffect(() => {
    latestConfig.current = config;
  }, [config]);

  // Take the standing unreadable-settings warning back down.
  //
  // Called from every path that PROVES the file is readable again — a save
  // that lands, a re-probe that comes back clean, a restore that rewrote it.
  // A persistent toast asserting "your settings can't be read", whose one
  // button renames the user's fully intact config.json out of the way, must
  // not survive the one-second lock that produced it.
  const clearRefusalWarning = () => {
    const id = refusalToastId.current;
    refusalToastId.current = null;
    stopRefusalPoll();
    if (id !== null) useNotificationStore.getState().dismiss(id);
  };

  const stopRefusalPoll = () => {
    if (refusalPollTimer.current === null) return;
    window.clearInterval(refusalPollTimer.current);
    refusalPollTimer.current = null;
  };

  /**
   * True only while this hook's unreadable-settings warning is ACTUALLY on
   * screen.
   *
   * Holding the id isn't enough: the toast can be taken away by someone else —
   * the user's own X button, or the store's hard stack ceiling — and the id
   * then points at nothing. Every "is the warning standing?" test read that
   * stale id as yes, which made `showRefusalWarning` a permanent no-op and
   * suppressed the re-probe with it: a genuinely unreadable config.json dropped
   * every subsequent save in total silence. A vanished toast means NOT
   * standing; forget the id and let the next refusal put a fresh one up.
   */
  const refusalWarningStanding = () => {
    const id = refusalToastId.current;
    if (id === null) return false;
    if (useNotificationStore.getState().items.some((n) => n.id === id)) return true;
    refusalToastId.current = null;
    stopRefusalPoll();
    return false;
  };

  // Keep asking the sidecar whether the settings file reads back yet, for as
  // long as the warning is up. The warning must come down BY ITSELF when the
  // lock clears — the user has no reason to touch a setting again just to find
  // out, and the only button on offer is the destructive one.
  const startRefusalPoll = () => {
    if (refusalPollTimer.current !== null) return;
    refusalPollTimer.current = window.setInterval(() => {
      if (!refusalWarningStanding()) {
        // Nothing left to take down (the check above already stopped the poll,
        // but be explicit — the next refusal starts the cycle from scratch).
        stopRefusalPoll();
        return;
      }
      if (refusalPolling.current) return;
      refusalPolling.current = true;
      rpc<{ load_failed?: boolean }>("get_config")
        .then((c) => {
          refusalPolling.current = false;
          if (c.load_failed) return; // still unreadable — the warning stands
          if (!refusalWarningStanding()) return;
          // It reads clean again, with no user edit in between. Take the
          // warning down and re-issue the save it was refusing so the change
          // that started all this isn't silently dropped. `isRetry` keeps a
          // second refusal at face value instead of opening another round.
          clearRefusalWarning();
          saveConfig(latestConfig.current, /* isRetry */ true);
        })
        .catch(() => {
          refusalPolling.current = false;
          // Couldn't even ask — leave the warning up and try again next tick.
        });
    }, REFUSAL_POLL_MS);
  };

  // The deliberate "overwrite it anyway" path for an unreadable settings file.
  // `restore_default_config` never refuses, and it adopts the load-failure
  // marker so `save()` renames the unreadable bytes to
  // `<name>.corrupt-<timestamp>` instead of destroying the user's only copy.
  const restoreDefaults = () => {
    rpc<{ config: VibechekConfig }>("restore_default_config")
      .then((result) => {
        setConfig(result.config, /* markLoaded */ true);
        // The file on disk is now the config on screen: it is trustworthy
        // again, and autosave may arm.
        useConfigStore.getState().setLoadUntrusted(false);
        untrustedBaseline.current = null;
        saveArmed.current = true;
        // Readable again — the warning is spent, and a LATER failure is new
        // news that deserves its own toast.
        clearRefusalWarning();
        useNotificationStore.getState().notify(
          "Settings restored to defaults",
          {
            kind: "success",
            detail:
              "The unreadable file was kept beside the new one with a " +
              '".corrupt-<timestamp>" suffix.',
          },
        );
      })
      .catch((e) => {
        clearRefusalWarning();
        useNotificationStore.getState().notify(
          `Could not restore default settings: ${errorMessage(e)}`,
          { kind: "warning", persistent: true },
        );
      });
  };

  // Put the standing warning up. Idempotent: one toast per condition, and the
  // id is kept so a later success can dismiss it.
  const showRefusalWarning = (message: string) => {
    if (refusalWarningStanding()) return;
    refusalToastId.current = useNotificationStore.getState().notify(message, {
      kind: "warning",
      persistent: true,
      detail:
        "Your changes are not being saved until this is resolved. " +
        "Restoring defaults QUARANTINES the settings file that's there now — " +
        'it is renamed with a ".corrupt-<timestamp>" suffix and a fresh one is ' +
        "written in its place — so use it only if the file really is broken. " +
        "Your library, analyses and tag backups are not touched.",
      action: { label: "Restore defaults", onClick: restoreDefaults },
    });
    startRefusalPoll();
  };

  // Fire a save immediately (used by both the debounce timer and the
  // flush-on-teardown). Takes the config explicitly so the teardown handler
  // never persists a stale closure value.
  //
  // `isRetry` marks the ONE save re-issued after a re-probe found the file
  // readable again; its refusal is taken at face value instead of starting
  // another probe/retry round.
  const saveConfig = (cfg: VibechekConfig, isRetry = false) => {
    rpc("save_config", { config: cfg }).then(() => {
      // The write landed, so the file is readable: any standing "couldn't be
      // read" warning is stale news, and its Restore-defaults button would now
      // quarantine an intact config.json. Take it down.
      clearRefusalWarning();
    }).catch((e) => {
      // A save failure that gets eaten silently is the worst kind of bug —
      // the user thinks their tweaks stuck. Surface it via a toast (info,
      // not the scary red error toast — the operation will retry on the
      // next change). Cancellations are impossible here but check anyway.
      if (isCancellation(e)) return;
      if (isUnreadableConfigRefusal(e)) {
        // The sidecar's message names the exact file standing in the way, and
        // the only way out of a genuinely unreadable one is to quarantine it —
        // so the toast shows the message verbatim and offers that action.
        //
        // But ONE refused save does not mean the file is broken. `_save_config`
        // re-reads config.json on every autosave, and a slider drag while an AV
        // scan or a cloud-sync client holds the file open produces exactly this
        // refusal for a second. Putting a permanent warning up on that first
        // failure — whose only button RENAMES the user's fully intact settings
        // away and writes factory defaults — is a far worse outcome than the
        // fault. Re-probe the file once, and only speak up if it's still
        // unreadable.
        if (refusalWarningStanding()) return; // already standing
        if (isRetry) {
          // Second refusal after the file read back clean: not a blip.
          showRefusalWarning(e.message);
          return;
        }
        if (refusalProbing.current) return; // one probe per condition
        refusalProbing.current = true;
        window.setTimeout(() => {
          rpc<{ load_failed?: boolean }>("get_config")
            .then((c) => {
              refusalProbing.current = false;
              if (c.load_failed) {
                // The sidecar read it and fell back to defaults — the file is
                // really unreadable, which is what the warning is for.
                showRefusalWarning(e.message);
                return;
              }
              // Readable again: the lock was transient. Re-issue the save that
              // was refused so the user's change isn't silently dropped.
              saveConfig(latestConfig.current, /* isRetry */ true);
            })
            .catch(() => {
              refusalProbing.current = false;
              // Couldn't even ask. Assume the condition stands rather than
              // swallowing a save failure.
              showRefusalWarning(e.message);
            });
        }, REFUSAL_RECHECK_MS);
        return;
      }
      const now = Date.now();
      if (now - lastNotifiedAt.current < 30_000) return;
      lastNotifiedAt.current = now;
      useNotificationStore
        .getState()
        .notify(`Settings could not save: ${errorMessage(e)}`, { kind: "info" });
    });
  };

  // Load on mount — single shot.
  useEffect(() => {
    let cancelled = false;
    // get_config attaches `config_warnings` at the transport level (NOT a config
    // field): the notes the loader collected while snapping invalid / cross-
    // platform saved values back to defaults. Surface them once so a silently
    // reverted setting doesn't masquerade as the user's own choice.
    rpc<VibechekConfig & { config_warnings?: string[]; load_failed?: boolean }>("get_config")
      .then((c) => {
        if (cancelled) return;
        const { config_warnings, load_failed, ...cfg } = c;
        // Decide whether this load is trustworthy BEFORE the setState that
        // wakes the autosave effect — the effect reads these refs.
        if (load_failed) untrustedBaseline.current = cfg as VibechekConfig;
        else saveArmed.current = true;
        // Publish the verdict to the store too: the refs above only gate the
        // autosave inside this hook, but other views must not NUDGE the user
        // into an action that writes the config either (the first-launch tour
        // is unavoidable and its only exits write `seen_onboarding`).
        useConfigStore.getState().setLoadUntrusted(Boolean(load_failed));
        setConfig(cfg as VibechekConfig, /* markLoaded */ true);
        if (load_failed) {
          // The backend couldn't READ config.json (locked by an AV scan, a
          // cloud-sync client, or corrupt) and fell back to defaults. What's on
          // screen is not the user's config, so autosave stays disarmed — a
          // save now would replace a file that's very likely still intact.
          useNotificationStore.getState().notify(
            "Your saved settings couldn't be read — showing defaults",
            {
              kind: "warning",
              persistent: true,
              detail:
                (config_warnings && config_warnings.length > 0
                  ? config_warnings.join("\n") + "\n\n"
                  : "") +
                "Your settings file was left alone. Changing any setting will overwrite it with what you see here.",
            },
          );
          return;
        }
        if (config_warnings && config_warnings.length > 0) {
          useNotificationStore.getState().notify(
            "Some saved settings were invalid and were reset",
            { kind: "info", detail: config_warnings.join("\n") },
          );
        }
      })
      .catch((e) => {
        if (cancelled) return;
        // Do NOT markLoaded. `loaded` is what arms the debounced autosave, and
        // the store is still holding DEFAULT_CONFIG — marking it loaded here
        // wrote those defaults over the user's real config.json 500ms later,
        // silently. Stay disarmed, and say so out loud.
        untrustedBaseline.current = useConfigStore.getState().config;
        useConfigStore.getState().setLoadUntrusted(true);
        const msg = errorMessage(e);
        useNotificationStore.getState().notify(
          "Couldn't load your saved settings — showing defaults",
          {
            kind: "warning",
            persistent: true,
            detail: `${msg}\n\nYour settings file was left alone. Changing any setting will overwrite it with what you see here.`,
          },
        );
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Debounced auto-save on every config change AFTER initial load.
  useEffect(() => {
    if (!saveArmed.current) {
      // Not armed: either the load hasn't settled yet, or it failed / came back
      // flagged as a fallback. Don't save defaults over a real on-disk config.
      // The user's own first edit is unambiguous intent to write — arm on it.
      const baseline = untrustedBaseline.current;
      if (baseline === null || baseline === config) return;
      untrustedBaseline.current = null;
      saveArmed.current = true;
    }
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
      if (saveArmed.current) saveConfig(latestConfig.current);
    };
    const onVisibility = () => {
      if (document.visibilityState === "hidden") flush();
    };
    window.addEventListener("beforeunload", flush);
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      window.removeEventListener("beforeunload", flush);
      document.removeEventListener("visibilitychange", onVisibility);
      // The re-probe interval outlives nothing: this effect is the hook's only
      // unmount hook, so the poll is torn down here.
      stopRefusalPoll();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
}
