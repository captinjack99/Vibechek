/**
 * The two recovery buttons for a "not enough memory to run the advanced genre
 * model" refusal — shared by BOTH surfaces that hit it:
 *
 *   - ErrorToast, when an analyze fails with the memory-refusal envelope
 *     (`can_switch_classifier` / `can_increase_memory` on `error.data`).
 *   - Settings' worker-slider, when the computed budget refuses even one worker.
 *
 * The handlers live here ONCE so the two call sites can't drift (the audit
 * required a shared component/hook, not copy-pasted logic). Each button renders
 * only when its capability flag is set.
 *
 *   • "Switch to the standard genre model" flips `genre_classifier` to discogs
 *     through the SAME config-update path Settings uses — `updateAnalysis` +
 *     the debounced autosave (useConfigPersistence) persists it via save_config,
 *     and the next analyze reads the new value.
 *   • "Give Vibechek more memory" calls the `increase_wsl_memory` self-heal. On
 *     a real bump it raises a PERSISTENT notice telling the user a WSL/Windows
 *     restart is needed — it NEVER restarts anything automatically.
 */

import { useState } from "react";
import { Cpu, MemoryStick } from "lucide-react";

import { useConfigStore, useNotificationStore } from "../stores";
import { increaseWslMemory } from "../api/rpc";

const BUTTON_CLASS =
  "text-xs font-medium text-white bg-accent-red/30 hover:bg-accent-red/50 " +
  "border border-accent-red/40 rounded px-2.5 py-1 inline-flex items-center " +
  "gap-1.5 disabled:opacity-60";

export function MemoryRefusalActions({
  canSwitchClassifier,
  canIncreaseMemory,
  className,
}: {
  canSwitchClassifier?: boolean;
  canIncreaseMemory?: boolean;
  /** Layout override for the wrapping row (defaults to a top-margin flex row). */
  className?: string;
}) {
  const [raisingMemory, setRaisingMemory] = useState(false);
  const notify = useNotificationStore((s) => s.notify);
  const updateAnalysis = useConfigStore((s) => s.updateAnalysis);

  if (!canSwitchClassifier && !canIncreaseMemory) return null;

  // Reuse the existing analysis-config update flow (NOT a bespoke save_config
  // call): the debounced autosave persists it and the next analyze picks it up.
  const handleSwitchClassifier = () => {
    updateAnalysis({ genre_classifier: "discogs" });
    notify("Switched to the standard genre model", {
      kind: "success",
      detail:
        "It needs far less memory than the advanced model — your next " +
        "analysis will use it.",
    });
  };

  const handleIncreaseMemory = async () => {
    setRaisingMemory(true);
    try {
      const res = await increaseWslMemory();
      if (res.ok && res.changed && res.restart_required) {
        // The doctrine's persistent+don't-auto-restart case: the limit is raised
        // on disk but only bites after a restart, so make the notice stick and
        // say plainly what to do — we NEVER restart WSL/Windows for the user.
        const from = res.old ? `from ${res.old} ` : "";
        notify(`Memory limit raised ${from}to ${res.new ?? "a higher value"}.`, {
          kind: "warning",
          persistent: true,
          detail:
            "Restart Windows' Linux environment (or reboot) for it to take " +
            "effect — analyses until then keep the old limit.",
        });
      } else if (res.ok) {
        // Nothing to change — the limit was already high enough.
        notify(
          res.message ??
            "The Linux analysis environment already has enough memory.",
          { kind: "info" },
        );
      } else {
        notify(res.headline ?? "Couldn't raise the memory limit.", {
          kind: "info",
          detail: res.detail ?? res.error ?? undefined,
        });
      }
    } catch (e) {
      notify("Couldn't raise the memory limit.", {
        kind: "info",
        detail: e instanceof Error ? e.message : String(e),
      });
    } finally {
      setRaisingMemory(false);
    }
  };

  return (
    <div className={className ?? "mt-2 flex flex-wrap items-center gap-2"}>
      {canSwitchClassifier && (
        <button onClick={handleSwitchClassifier} className={BUTTON_CLASS}>
          <Cpu className="w-3.5 h-3.5" />
          Switch to the standard genre model
        </button>
      )}
      {canIncreaseMemory && (
        <button
          onClick={handleIncreaseMemory}
          disabled={raisingMemory}
          className={BUTTON_CLASS}
        >
          <MemoryStick className="w-3.5 h-3.5" />
          {raisingMemory ? "Raising memory…" : "Give Vibechek more memory"}
        </button>
      )}
    </div>
  );
}
