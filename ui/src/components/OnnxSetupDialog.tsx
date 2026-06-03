/**
 * Progress dialog for the one-click "Set up ONNX engine" flow.
 *
 * The `setup_onnx_engine` RPC stages the bundled converted heads, installs the
 * ONNX engine env if needed, fetches the EffNet backbone, and verifies — all
 * while emitting `progress` notifications. This modal shows a live bar + the
 * current step message so the user knows the app is working, not hung, and a
 * clear success / error state at the end.
 */
import { useState } from "react";
import { Loader2, CheckCircle2, AlertCircle, X, Cpu } from "lucide-react";

import { useSidecarProgress } from "../hooks/useSidecar";

export type OnnxSetupState =
  | { phase: "running" }
  | { phase: "done"; staged: number }
  | { phase: "error"; error: string }
  | null;

export function OnnxSetupDialog({
  state,
  onClose,
}: {
  state: OnnxSetupState;
  onClose: () => void;
}) {
  const [progress, setProgress] = useState({ current: 0, total: 4, message: "Starting…" });

  // While the setup is running, the only in-flight op is setup_onnx_engine, so
  // its progress notifications are the ones we render.
  useSidecarProgress((e) => {
    if (state?.phase === "running") {
      setProgress({ current: e.current, total: e.total || 4, message: e.message || "Working…" });
    }
  });

  if (!state) return null;
  const pct = progress.total > 0 ? Math.min(100, Math.round((progress.current / progress.total) * 100)) : 0;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4">
      <div className="w-[480px] max-w-full rounded-xl border border-white/10 bg-surface-100 p-5 shadow-2xl">
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
            <p className="text-[11px] leading-snug text-white/40">
              Step {Math.min(progress.current, progress.total)} of {progress.total}. The first run installs the
              engine and fetches the EffNet backbone, so it can take a few minutes — the app isn't hung. You can
              keep using other tabs.
            </p>
          </div>
        )}

        {state.phase === "done" && (
          <div className="space-y-3">
            <div className="flex items-center gap-2 text-sm font-medium text-green-400">
              <CheckCircle2 className="h-5 w-5" /> ONNX engine ready
            </div>
            <p className="text-xs leading-snug text-white/60">
              {state.staged} model files staged. Switch the inference engine to ONNX (if you haven't) and
              re-analyze your library to use it.
            </p>
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
