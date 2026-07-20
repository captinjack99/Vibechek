/**
 * Global error banner. Shown whenever `useOperationStore.errorInfo` is set so an
 * operation failure can never silently disappear.
 *
 * The failure arrives pre-classified (see stores/operation.ts `classifyError`):
 * a plain user-facing headline, a technical `detail` demoted behind a toggle,
 * and a `kind` that decides which recovery action we offer:
 *   - kind "retryable"   → "Try again" re-issues the exact failed call
 *   - kind "engine_dead" → "Restart Vibechek" relaunches the app
 *
 * Plus the always-available actions: Copy details, View logs, Report on GitHub.
 */

import { useEffect, useState } from "react";
import { motion } from "framer-motion";
import {
  AlertCircle,
  X,
  Copy,
  Check,
  ExternalLink,
  FileText,
  RotateCcw,
  RefreshCw,
  Download,
} from "lucide-react";
import { open as openUrl } from "@tauri-apps/plugin-shell";
import { isTauri } from "@tauri-apps/api/core";

import { useNotificationStore, useOperationStore } from "../stores";
import { rpc, sidecarStatus } from "../hooks/useSidecar";
import { installWSL } from "../api/rpc";
import { LogsViewer } from "./LogsViewer";
import { MemoryRefusalActions } from "./MemoryRefusalActions";

const ISSUES_URL = "https://github.com/captinjack99/Vibechek/issues/new";

export function ErrorToast() {
  const errorInfo = useOperationStore((s) => s.errorInfo);
  const clearError = useOperationStore((s) => s.clearError);
  const notify = useNotificationStore((s) => s.notify);

  const [copied, setCopied] = useState(false);
  const [showLogs, setShowLogs] = useState(false);
  const [restarting, setRestarting] = useState(false);

  // Reset the copy state when a new error appears
  useEffect(() => {
    setCopied(false);
  }, [errorInfo]);

  if (!errorInfo) return null;

  const { headline, detail, raw, kind } = errorInfo;
  const canRetry = kind === "retryable" && !!errorInfo.retry;
  const canRestart = kind === "engine_dead";
  // Backend-attached recovery affordances (from error.data). Each gates its own
  // action button; the two memory ones are rendered by the shared component.
  const canSwitchClassifier = !!errorInfo.options?.canSwitchClassifier;
  const canIncreaseMemory = !!errorInfo.options?.canIncreaseMemory;
  const canInstallWsl = !!errorInfo.options?.canInstallWsl;

  // Mid-analyze death: how many tracks had been analyzed when it stopped.
  // Phrased honestly — analyzed, not necessarily saved.
  const analyzedLine =
    kind === "engine_dead" && errorInfo.analyzedCount
      ? errorInfo.analyzedTotal
        ? `${errorInfo.analyzedCount} of ${errorInfo.analyzedTotal} tracks were analyzed before it stopped.`
        : `${errorInfo.analyzedCount} tracks were analyzed before it stopped.`
      : null;

  const handleCopy = () => {
    void navigator.clipboard.writeText(raw);
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  };

  // "Try again" re-issues the exact call that failed (captured by the shared
  // rpc wrapper). We clear the banner first, then re-fire; a fresh failure
  // re-surfaces via fail(). Generic by design — no per-component retry wiring.
  const handleRetry = async () => {
    const retry = errorInfo.retry;
    if (!retry) return;
    clearError();
    try {
      await rpc(retry.method, retry.params);
    } catch (e) {
      useOperationStore.getState().fail(e);
    }
  };

  // "Install WSL" runs the existing install_wsl self-heal, reusing the app's
  // long-op progress idiom: begin("install-wsl") hands the global AnalysisProgress
  // overlay the running state (and clears this banner), the RPC emits progress,
  // and we notify on completion. A failure re-surfaces through fail(). We never
  // stand up a bespoke install UI here.
  const handleInstallWsl = async () => {
    const { begin, finish, fail } = useOperationStore.getState();
    const opId = begin("install-wsl");
    try {
      const res = await installWSL({}, opId);
      finish();
      if (res.ok) {
        notify("Windows' Linux environment installed", {
          kind: "success",
          detail: res.note
            ? String(res.note)
            : "Try your analysis again. Windows may ask you to restart first.",
        });
      } else {
        notify("Couldn't install the Linux environment", {
          kind: "info",
          detail: res.error ? String(res.error) : undefined,
        });
      }
    } catch (e) {
      fail(e);
    }
  };

  // "Restart Vibechek" relaunches the app (same plugin the updater uses). No-op
  // outside the Tauri shell (dev browser / tests) so the button is inert there.
  const handleRestart = async () => {
    if (!isTauri()) return;
    setRestarting(true);
    try {
      const { relaunch } = await import("@tauri-apps/plugin-process");
      await relaunch();
    } catch {
      // Relaunch failed — nothing more we can do; leave the banner up.
      setRestarting(false);
    }
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
      raw.slice(0, 4000),
      "```",
      "",
      "---",
      `Vibechek version: ${version}`,
      `Analysis service: ${sidecar}`,
      `Platform: ${navigator.userAgent}`,
    ].join("\n");

    const url = `${ISSUES_URL}?title=${encodeURIComponent("Error: " + headline.slice(0, 80))}&body=${encodeURIComponent(body)}`;

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
          <div className="text-sm font-medium text-accent-red break-words">
            {headline}
          </div>
          {analyzedLine && (
            <div className="text-xs text-white/70 mt-1">{analyzedLine}</div>
          )}

          {/* Primary recovery actions — the "what do I do now" the doctrine
              requires. Only shown for the kinds that support them. */}
          {(canRetry || canRestart) && (
            <div className="mt-3 flex items-center gap-2">
              {canRetry && (
                <button
                  onClick={handleRetry}
                  className="text-xs font-medium text-white bg-accent-red/30 hover:bg-accent-red/50 border border-accent-red/40 rounded px-2.5 py-1 inline-flex items-center gap-1.5"
                >
                  <RotateCcw className="w-3.5 h-3.5" />
                  Try again
                </button>
              )}
              {canRestart && (
                <button
                  onClick={handleRestart}
                  disabled={restarting}
                  className="text-xs font-medium text-white bg-accent-red/30 hover:bg-accent-red/50 border border-accent-red/40 rounded px-2.5 py-1 inline-flex items-center gap-1.5 disabled:opacity-60"
                >
                  <RefreshCw className={`w-3.5 h-3.5 ${restarting ? "animate-spin" : ""}`} />
                  {restarting ? "Restarting…" : "Restart Vibechek"}
                </button>
              )}
            </div>
          )}

          {/* Memory-refusal recovery — the SAME two buttons Settings' worker
              slider shows, from one shared component (no duplicated handlers). */}
          <MemoryRefusalActions
            canSwitchClassifier={canSwitchClassifier}
            canIncreaseMemory={canIncreaseMemory}
            className="mt-3 flex flex-wrap items-center gap-2"
          />

          {/* WSL-missing recovery — reuse the install self-heal + progress overlay. */}
          {canInstallWsl && (
            <div className="mt-3 flex items-center gap-2">
              <button
                onClick={handleInstallWsl}
                className="text-xs font-medium text-white bg-accent-red/30 hover:bg-accent-red/50 border border-accent-red/40 rounded px-2.5 py-1 inline-flex items-center gap-1.5"
              >
                <Download className="w-3.5 h-3.5" />
                Install WSL
              </button>
            </div>
          )}

          {/* Secondary / always-available actions. */}
          <div className="mt-3 flex items-center gap-2 flex-wrap">
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
              title="Show recent log lines from the analysis service"
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

          {detail && (
            <details className="mt-2 text-[11px] text-white/40">
              <summary className="cursor-pointer hover:text-white/60">
                Technical details
              </summary>
              <pre className="mt-1 font-mono whitespace-pre-wrap break-all max-h-48 overflow-auto">
                {detail}
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
