/**
 * Generic progress dialog for the opt-in genre engine setups (CLAP audio
 * student / online web-synthesis resolver). Both `setup_clap_engine` and
 * `setup_genre_resolver` emit `progress` notifications (0-100) while they
 * install deps + download a multi-GB model; this modal shows a live bar + the
 * current step, and a clear success / error / cancel state. Mirrors
 * OnnxSetupDialog but parameterized by title/message.
 */
import { useState } from "react";
import { Loader2, CheckCircle2, AlertCircle, X, Cpu, StopCircle } from "lucide-react";

import { useSidecarProgress } from "../hooks/useSidecar";

export type GenreSetupState =
  | { phase: "running" }
  | { phase: "done" }
  | { phase: "error"; error: string }
  | null;

export function GenreSetupDialog({
  state,
  title,
  doneMessage,
  onClose,
  onCancel,
}: {
  state: GenreSetupState;
  title: string;
  doneMessage: string;
  onClose: () => void;
  onCancel?: () => void;
}) {
  const [progress, setProgress] = useState({ pct: 0, message: "Starting…" });

  useSidecarProgress((e) => {
    if (state?.phase === "running") {
      const pct = e.total > 0 ? Math.min(100, Math.round((e.current / e.total) * 100)) : 0;
      setProgress({ pct, message: e.message || "Working…" });
    }
  });

  if (!state) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4">
      <div className="w-[480px] max-w-full rounded-xl border border-white/10 bg-surface-100 p-5 shadow-2xl">
        <div className="mb-4 flex items-center justify-between">
          <h3 className="flex items-center gap-2 text-sm font-semibold text-white">
            <Cpu className="h-4 w-4 text-accent" /> {title}
          </h3>
          {state.phase !== "running" && (
            <button onClick={onClose} className="p-1 text-white/40 hover:text-white" aria-label="Close">
              <X className="h-4 w-4" />
            </button>
          )}
        </div>

        {state.phase === "running" && (
          <div className="space-y-3">
            <div className="flex items-center gap-2 text-xs text-white/80">
              <Loader2 className="h-4 w-4 flex-none animate-spin text-accent" />
              <span className="truncate">{progress.message}</span>
            </div>
            <div className="h-2 overflow-hidden rounded-full bg-white/10">
              <div className="h-full rounded-full bg-accent transition-all duration-300" style={{ width: `${progress.pct}%` }} />
            </div>
            <p className="text-[11px] leading-snug text-white/40">
              The first run installs the engine and downloads a multi-GB model, so it can take several
              minutes — the app isn't hung. You can keep using other tabs.
            </p>
            {onCancel && (
              <button
                onClick={onCancel}
                className="flex items-center gap-1.5 rounded-lg bg-white/10 px-3 py-2 text-sm font-medium text-white hover:bg-white/20"
                title="Stop the setup"
              >
                <StopCircle className="h-4 w-4" />
                Cancel
              </button>
            )}
          </div>
        )}

        {state.phase === "done" && (
          <div className="space-y-3">
            <div className="flex items-center gap-2 text-sm font-medium text-green-400">
              <CheckCircle2 className="h-5 w-5" /> Ready
            </div>
            <p className="text-xs leading-snug text-white/60">{doneMessage}</p>
            <button onClick={onClose} className="w-full rounded-lg bg-accent px-3 py-2 text-sm font-medium text-white hover:bg-accent/90">
              Done
            </button>
          </div>
        )}

        {state.phase === "error" && (
          <div className="space-y-3">
            <div className="flex items-center gap-2 text-sm font-medium text-accent-red">
              <AlertCircle className="h-5 w-5" /> Setup didn't finish
            </div>
            <p className="max-h-32 overflow-auto whitespace-pre-wrap break-words font-mono text-[11px] leading-snug text-white/60">
              {state.error}
            </p>
            <button onClick={onClose} className="w-full rounded-lg bg-white/10 px-3 py-2 text-sm font-medium text-white hover:bg-white/20">
              Close
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
