import { motion } from "framer-motion";
import { Loader2, StopCircle } from "lucide-react";

import { useOperationStore } from "../stores";
import { rpc } from "../hooks/useSidecar";

// Cancelling is best-effort. The sidecar may already have moved past the
// cancellation point, the RPC pipe may be momentarily dropped, etc. None of
// those should crash the UI or surface as a scary error toast — the user
// just wanted to stop, not be told something went wrong.
function logCancelFailure(e: unknown): void {
  // eslint-disable-next-line no-console
  console.warn("cancel_operation RPC failed:", e);
}

const KIND_LABELS: Record<string, string> = {
  analyze: "Analyzing library",
  dedupe: "Finding duplicates",
  organize: "Organizing files",
  tag: "Writing tags",
  backup: "Backing up tags",
  "download-models": "Downloading ML models",
};

export function AnalysisProgress() {
  const active = useOperationStore((s) => s.active);
  const progress = useOperationStore((s) => s.progress);
  const startedAt = useOperationStore((s) => s.startedAt);

  if (!active) return null;

  const label = KIND_LABELS[active] ?? active;
  const pct = progress && progress.total > 0
    ? Math.round((progress.current / progress.total) * 100)
    : null;
  const elapsedSec = startedAt
    ? Math.round((Date.now() - startedAt) / 1000)
    : 0;

  return (
    <motion.div
      initial={{ opacity: 0, y: 20 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0, y: 20 }}
      className="fixed bottom-6 left-1/2 -translate-x-1/2 z-50"
    >
      <div className="panel-pad min-w-[420px] max-w-[640px] shadow-2xl">
        <div className="flex items-center gap-3 mb-3">
          <Loader2 className="w-4 h-4 text-accent animate-spin" />
          <div className="flex-1">
            <div className="text-sm font-medium text-white">{label}</div>
            {progress?.message && (
              <div className="text-xs text-white/40 font-mono truncate">
                {progress.message}
              </div>
            )}
          </div>
          {pct !== null && (
            <div className="text-sm font-mono text-white/60 tabular-nums">
              {pct}%
            </div>
          )}
          <button
            onClick={() => {
              rpc("cancel_operation").catch(logCancelFailure);
            }}
            className="text-white/40 hover:text-accent-red flex items-center gap-1 text-xs"
            title="Cancel this operation"
          >
            <StopCircle className="w-4 h-4" />
            Cancel
          </button>
        </div>

        {/* Progress bar */}
        <div className="h-1.5 bg-white/5 rounded-full overflow-hidden">
          <div
            className="h-full bg-accent transition-all duration-200"
            style={{ width: pct !== null ? `${pct}%` : "30%" }}
          />
        </div>

        <div className="flex justify-between mt-2 text-[11px] text-white/40 font-mono">
          <span>
            {progress
              ? `${progress.current.toLocaleString()} / ${progress.total.toLocaleString()}`
              : "starting…"}
          </span>
          <span>{elapsedSec}s elapsed</span>
        </div>
      </div>
    </motion.div>
  );
}
