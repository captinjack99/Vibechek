/**
 * Global error banner. Shown whenever `useOperationStore.error` is set so an
 * operation failure can never silently disappear.
 *
 * Two actions:
 *   - Copy details — full error to clipboard
 *   - Report issue — opens a prefilled GitHub Issue with the error + system
 *     context so the user doesn't have to type it all out
 */

import { useEffect, useState } from "react";
import { motion } from "framer-motion";
import { AlertCircle, X, Copy, Check, ExternalLink, FileText } from "lucide-react";
import { open as openUrl } from "@tauri-apps/plugin-shell";

import { useNotificationStore, useOperationStore } from "../stores";
import { rpc, sidecarStatus } from "../hooks/useSidecar";
import { LogsViewer } from "./LogsViewer";

const ISSUES_URL = "https://github.com/captinjack99/Vibechek/issues/new";

export function ErrorToast() {
  const error = useOperationStore((s) => s.error);
  const clearError = useOperationStore((s) => s.clearError);
  const notify = useNotificationStore((s) => s.notify);

  const [copied, setCopied] = useState(false);
  const [showLogs, setShowLogs] = useState(false);

  // Reset the copy state when a new error appears
  useEffect(() => {
    setCopied(false);
  }, [error]);

  if (!error) return null;

  const message = friendlyMessage(error);

  const handleCopy = () => {
    void navigator.clipboard.writeText(error);
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  };

  const handleReport = async () => {
    // Gather a bit of context to prefill the issue body
    let version = "unknown";
    let sidecar = "unknown";
    try {
      const v = await rpc<{ version: string }>("version");
      version = v.version;
    } catch { /* ignore */ }
    try {
      const s = await sidecarStatus();
      sidecar = s.binary;
    } catch { /* ignore */ }

    const body = [
      "**What I was doing:**",
      "<!-- e.g. clicked Analyze on D:\\Music -->",
      "",
      "**Expected:**",
      "<!-- what should have happened -->",
      "",
      "**Got:**",
      "```",
      error.slice(0, 4000),
      "```",
      "",
      "---",
      `Vibechek version: ${version}`,
      `Sidecar: ${sidecar}`,
      `Platform: ${navigator.userAgent}`,
    ].join("\n");

    const url = `${ISSUES_URL}?title=${encodeURIComponent("Error: " + message.slice(0, 80))}&body=${encodeURIComponent(body)}`;

    try {
      await openUrl(url);
    } catch (e) {
      // Fallback: copy URL to clipboard so the user can paste it
      void navigator.clipboard.writeText(url);
      notify("Couldn't open browser", {
        detail: "URL copied to clipboard — paste it anywhere to file the issue.",
        kind: "info",
      });
    }
  };

  return (
    <motion.div
      initial={{ opacity: 0, y: -20 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0, y: -20 }}
      className="fixed top-4 left-1/2 -translate-x-1/2 z-[70] max-w-2xl w-full px-4"
      role="alert"
    >
      <div className="panel-pad bg-accent-red/10 border-accent-red/40 flex items-start gap-3 shadow-lg">
        <AlertCircle className="w-5 h-5 text-accent-red flex-none mt-0.5" />
        <div className="flex-1 min-w-0">
          <div className="text-sm font-medium text-accent-red">
            Something went wrong
          </div>
          <div className="text-xs text-white/70 mt-1 break-words">
            {message}
          </div>
          <div className="mt-3 flex items-center gap-2">
            <button
              onClick={handleCopy}
              className="text-xs text-white/60 hover:text-white inline-flex items-center gap-1"
              title="Copy the full error message"
            >
              {copied ? (
                <Check className="w-3 h-3 text-accent-green" />
              ) : (
                <Copy className="w-3 h-3" />
              )}
              {copied ? "Copied" : "Copy details"}
            </button>
            <button
              onClick={() => setShowLogs(true)}
              className="text-xs text-white/60 hover:text-white inline-flex items-center gap-1"
              title="Show recent log lines from the sidecar"
            >
              <FileText className="w-3 h-3" />
              View logs
            </button>
            <button
              onClick={handleReport}
              className="text-xs text-white/60 hover:text-white inline-flex items-center gap-1"
              title="Open a pre-filled GitHub issue in your browser"
            >
              <ExternalLink className="w-3 h-3" />
              Report on GitHub
            </button>
          </div>
          {message !== error && (
            <details className="mt-2 text-[11px] text-white/40">
              <summary className="cursor-pointer hover:text-white/60">
                Full error
              </summary>
              <pre className="mt-1 font-mono whitespace-pre-wrap break-all max-h-48 overflow-auto">
                {error}
              </pre>
            </details>
          )}
        </div>
        <button
          onClick={clearError}
          className="text-white/40 hover:text-white p-1 -m-1 flex-none"
          title="Dismiss"
          aria-label="Dismiss error"
        >
          <X className="w-4 h-4" />
        </button>
      </div>
      <LogsViewer open={showLogs} onClose={() => setShowLogs(false)} />
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
  return raw.replace(/^Error invoking remote method '[^']+':\s*/, "").trim();
}
