/**
 * Logs viewer modal.
 *
 * Pulls the last N lines of the sidecar log via `get_log_tail` RPC. Renders
 * them in a fixed-height scrolling pane with a copy-all button and a level
 * filter so users can find the relevant lines after an error.
 *
 * Opened from ErrorToast's "View logs" button.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import { motion } from "framer-motion";
import { X, Copy, Check, RefreshCw, Filter as FilterIcon } from "lucide-react";

import { rpc } from "../hooks/useSidecar";
import { useNotificationStore } from "../stores";

interface Props {
  open: boolean;
  onClose: () => void;
}

type Level = "ALL" | "DEBUG" | "INFO" | "WARN" | "ERROR";

const LEVELS: Level[] = ["ALL", "INFO", "WARN", "ERROR", "DEBUG"];

export function LogsViewer({ open, onClose }: Props) {
  const [lines, setLines] = useState<string[]>([]);
  const [logFile, setLogFile] = useState<string>("");
  const [loading, setLoading] = useState(false);
  const [filter, setFilter] = useState<Level>("ALL");
  const [copied, setCopied] = useState(false);
  const paneRef = useRef<HTMLDivElement>(null);

  const fetchLogs = async () => {
    setLoading(true);
    try {
      const r = await rpc<{ log_file: string; lines: string[] }>("get_log_tail", { n: 500 });
      setLines(r.lines);
      setLogFile(r.log_file);
    } catch {
      setLines(["(failed to read log file)"]);
    } finally {
      setLoading(false);
    }
  };

  // Fetch on open
  useEffect(() => {
    if (open) {
      void fetchLogs();
    }
  }, [open]);

  // Auto-scroll to bottom whenever lines change
  useEffect(() => {
    if (paneRef.current) {
      paneRef.current.scrollTop = paneRef.current.scrollHeight;
    }
  }, [lines, filter]);

  // ESC closes
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  const filtered = useMemo(() => {
    if (filter === "ALL") return lines;
    // Tolerant matcher: looks for the level token surrounded by whitespace
    // (or at line boundaries) so it works regardless of the formatter's
    // exact column-padding rules. WARN also matches WARNING (Python's
    // logging module emits the full word, but `logging.WARN` is also
    // valid — both should match the same filter button).
    const tokens = filter === "WARN" ? ["WARN", "WARNING"] : [filter];
    const re = new RegExp(`(^|\\s)(?:${tokens.join("|")})(\\s|$)`);
    return lines.filter((l) => re.test(l));
  }, [lines, filter]);

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(filtered.join("\n"));
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch (e) {
      // navigator.clipboard rejects when the document isn't focused or the
      // user denied clipboard permission. Surface so the user knows the
      // copy didn't happen rather than silently swallowing it.
      const msg =
        typeof e === "object" && e !== null && "message" in e
          ? String((e as { message: unknown }).message)
          : String(e);
      useNotificationStore
        .getState()
        .notify(`Could not copy logs: ${msg}`, { kind: "info" });
    }
  };

  if (!open) return null;

  return (
    <motion.div
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      exit={{ opacity: 0 }}
      className="fixed inset-0 z-[60] bg-black/60 flex items-center justify-center px-4"
      onClick={onClose}
      role="dialog"
      aria-modal="true"
      aria-label="Logs"
    >
      <motion.div
        initial={{ scale: 0.96, y: 10 }}
        animate={{ scale: 1, y: 0 }}
        className="panel w-full max-w-4xl h-[80vh] flex flex-col"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex items-start gap-3 px-5 py-3 border-b border-white/5">
          <div className="flex-1 min-w-0">
            <h2 className="font-display font-semibold text-lg">Logs</h2>
            <p className="text-xs text-white/40 font-mono truncate">{logFile}</p>
          </div>
          <button onClick={onClose} className="text-white/40 hover:text-white -m-1 p-1" aria-label="Close">
            <X className="w-5 h-5" />
          </button>
        </div>

        {/* Toolbar */}
        <div className="flex items-center gap-2 px-5 py-2 border-b border-white/5 flex-wrap">
          <FilterIcon className="w-3.5 h-3.5 text-white/40" />
          <div className="flex items-center gap-1">
            {LEVELS.map((lvl) => (
              <button
                key={lvl}
                onClick={() => setFilter(lvl)}
                className={`px-2 py-0.5 rounded text-[11px] font-mono ${
                  filter === lvl
                    ? "bg-accent text-white"
                    : "text-white/60 hover:bg-white/5"
                }`}
              >
                {lvl}
              </button>
            ))}
          </div>
          <div className="ml-auto flex items-center gap-2">
            <span className="text-xs text-white/40 tabular-nums">
              {filtered.length} of {lines.length} lines
            </span>
            <button
              onClick={fetchLogs}
              disabled={loading}
              className="btn-ghost text-xs"
              title="Reload from disk"
            >
              <RefreshCw className={`w-3.5 h-3.5 ${loading ? "animate-spin" : ""}`} />
              Refresh
            </button>
            <button onClick={handleCopy} className="btn-ghost text-xs" title="Copy all visible lines">
              {copied ? (
                <Check className="w-3.5 h-3.5 text-accent-green" />
              ) : (
                <Copy className="w-3.5 h-3.5" />
              )}
              {copied ? "Copied" : "Copy"}
            </button>
          </div>
        </div>

        {/* Log pane */}
        <div
          ref={paneRef}
          className="flex-1 overflow-auto px-4 py-3 font-mono text-[11px] leading-relaxed text-white/70 bg-surface-300/40 select-text"
        >
          {filtered.length === 0 ? (
            <div className="text-white/30 text-center py-8">
              {filter === "ALL" ? "No log lines yet" : `No ${filter} lines`}
            </div>
          ) : (
            filtered.map((line, i) => <LogLine key={i} line={line} />)
          )}
        </div>
      </motion.div>
    </motion.div>
  );
}

/** Colorize the level token (INFO/WARN/ERROR) in each log line. */
function LogLine({ line }: { line: string }) {
  let className = "text-white/70";
  if (line.includes(" ERROR ")) className = "text-accent-red";
  else if (line.includes(" WARNING ") || line.includes(" WARN ")) className = "text-accent-yellow";
  else if (line.includes(" DEBUG ")) className = "text-white/40";
  return <div className={`whitespace-pre-wrap break-all ${className}`}>{line}</div>;
}
