/**
 * Progress dialog for the one-click "Set up ONNX engine" flow.
 *
 * The `setup_onnx_engine` RPC stages the bundled converted heads, installs the
 * ONNX engine env if needed, fetches the EffNet backbone, and verifies — all
 * while emitting `progress` notifications. This modal shows a live bar + the
 * current step message so the user knows the app is working, not hung, and a
 * clear success / error state at the end.
 *
 * A11y: role="dialog" + aria-modal; Esc closes the settled (done/error)
 * states; progress resets when a new run starts.
 */
import { useEffect, useRef, useState } from "react";
import { Loader2, CheckCircle2, AlertCircle, X, Cpu, StopCircle } from "lucide-react";

import { useSidecarProgress } from "../hooks/useSidecar";

export type OnnxSetupState =
  | { phase: "running" }
  | { phase: "done"; staged: number }
  | { phase: "error"; error: string }
  | null;

export function OnnxSetupDialog({
  state,
  onClose,
  onCancel,
}: {
  state: OnnxSetupState;
  onClose: () => void;
  // Abort an in-flight setup. setup_onnx_engine is a Cancellable backend op
  // (it calls cancellation.check() during the network fetch/install), so a
  // user on a slow/flaky mirror isn't trapped behind a non-dismissable modal.
  onCancel?: () => void;
}) {
  const [progress, setProgress] = useState({ current: 0, total: 4, message: "Starting…" });

  // While the setup is running, the only in-flight op is setup_onnx_engine, so
  // its progress notifications are the ones we render.
  useSidecarProgress((e) => {
    if (state?.phase === "running") {
      setProgress({ current: e.current, total: e.total || 4, message: e.message || "Working…" });
    }
  });

  // Reset stale progress when a new run starts (the component stays mounted
  // with state=null between runs).
  const prevPhase = useRef<string | null>(null);
  useEffect(() => {
    if (state?.phase === "running" && prevPhase.current !== "running") {
      setProgress({ current: 0, total: 4, message: "Starting…" });
    }
    prevPhase.current = state?.phase ?? null;
  }, [state]);

  // Esc closes the settled states.
  useEffect(() => {
    if (!state || state.phase === "running") return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [state, onClose]);

  if (!state) return null;
  const pct = progress.total > 0 ? Math.min(100, Math.round((progress.current / progress.total) * 100)) : 0;

  return (
    <div
      className="fixed inset-0 z-[60] flex items-center justify-center bg-black/60 p-4"
      role="dialog"
      aria-modal="true"
      aria-label="Set up ONNX engine"
    >
      <div className="panel w-[480px] max-w-full p-5 shadow-2xl">
        <div className="mb-4 flex items-center justify-between">
          <h3 className="flex items-center gap-2 text-sm font-semibold text-white">
            <Cpu className="h-4 w-4 text-accent" /> Set up ONNX engine
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
              <div className="h-full rounded-full bg-accent transition-all duration-300" style={{ width: `${pct}%` }} />
            </div>
            <p className="text-[11px] leading-snug text-white/50">
              Step {Math.min(progress.current, progress.total)} of {progress.total}. The first run installs the
              engine and fetches the EffNet backbone, so it can take a few minutes — the app isn't hung. You can
              keep using other tabs.
            </p>
            {onCancel && (
              <button
                onClick={onCancel}
                className="btn-ghost bg-white/10 hover:bg-white/20"
                title="Stop the ONNX engine setup"
              >
                <StopCircle className="h-4 w-4" />
                Cancel
              </button>
            )}
          </div>
        )}

        {state.phase === "done" && (
          <div className="space-y-3">
            <div className="flex items-center gap-2 text-sm font-medium text-accent-green">
              <CheckCircle2 className="h-5 w-5" /> ONNX engine ready
            </div>
            <p className="text-xs leading-snug text-white/60">
              {state.staged} model files staged. Switch the inference engine to ONNX (if you haven't) and
              re-analyze your library to use it.
            </p>
            <button onClick={onClose} className="btn-primary w-full justify-center">
              Done
            </button>
          </div>
        )}

        {state.phase === "error" && (
          <div className="space-y-3">
            <div className="flex items-center gap-2 text-sm font-medium text-accent-red">
              <AlertCircle className="h-5 w-5" /> Setup didn't finish
            </div>
            <p className="max-h-32 overflow-auto whitespace-pre-wrap break-words font-mono text-[11px] leading-snug text-white/60 select-text">
              {state.error}
            </p>
            <button onClick={onClose} className="btn-ghost w-full justify-center bg-white/10 hover:bg-white/20">
              Close
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
