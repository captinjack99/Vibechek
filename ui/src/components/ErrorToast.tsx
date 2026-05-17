/**
 * Global error banner. Shown whenever `useOperationStore.error` is set so an
 * operation failure can never silently disappear into the void. Dismissed by
 * clicking the close button or by starting a new operation.
 */

import { motion } from "framer-motion";
import { AlertCircle, X } from "lucide-react";

import { useOperationStore } from "../stores";

export function ErrorToast() {
  const error = useOperationStore((s) => s.error);
  const clearError = useOperationStore((s) => s.clearError);

  if (!error) return null;

  // Heuristic: pull a friendlier summary line out of the sidecar's error JSON
  // when possible. The Rust shell wraps RPC errors as JSON strings.
  const message = friendlyMessage(error);

  return (
    <motion.div
      initial={{ opacity: 0, y: -20 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0, y: -20 }}
      className="fixed top-4 left-1/2 -translate-x-1/2 z-50 max-w-2xl w-full px-4"
    >
      <div className="panel-pad bg-accent-red/10 border-accent-red/40 flex items-start gap-3 shadow-lg">
        <AlertCircle className="w-5 h-5 text-accent-red flex-none mt-0.5" />
        <div className="flex-1 min-w-0">
          <div className="text-sm font-medium text-accent-red">
            Operation failed
          </div>
          <div className="text-xs text-white/70 mt-1 break-words">
            {message}
          </div>
          {message !== error && (
            <details className="mt-2 text-[11px] text-white/40">
              <summary className="cursor-pointer hover:text-white/60">
                Full error
              </summary>
              <pre className="mt-1 font-mono whitespace-pre-wrap break-all">
                {error}
              </pre>
            </details>
          )}
        </div>
        <button
          onClick={clearError}
          className="text-white/40 hover:text-white p-1 -m-1 flex-none"
          title="Dismiss"
        >
          <X className="w-4 h-4" />
        </button>
      </div>
    </motion.div>
  );
}

function friendlyMessage(raw: string): string {
  // Sidecar errors arrive as JSON-stringified {code, message, data}
  try {
    const stripped = raw.replace(/^sidecar error:\s*/i, "");
    const parsed = JSON.parse(stripped);
    if (parsed.message) return String(parsed.message);
  } catch {
    /* not JSON, fall through */
  }
  // Trim noisy prefixes
  return raw.replace(/^Error invoking remote method '[^']+':\s*/, "").trim();
}
