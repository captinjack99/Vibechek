/**
 * Pre-flight dialog — appears when the user tries to start `analyze` but the
 * sidecar reports it can't yet.
 *
 * On Windows, this dialog walks the user through fully automated setup:
 *
 *   1. WSL row
 *      - WSL not installed     → "Install WSL" button (triggers UAC)
 *      - WSL on, no distro     → "Install Ubuntu" button
 *      - Distro present, no    → "Install Vibechek + Essentia" button
 *        vibechek/essentia
 *      - All green             → analyze will route through WSL automatically
 *
 *   2. Models row
 *      - "Download models now" button (200 MB pulled from essentia.upf.edu)
 *
 * Each step shows live progress from the sidecar. Once everything is green the
 * dialog auto-dismisses and the original Analyze action proceeds.
 */

import { useEffect, useRef, useState } from "react";
import { motion } from "framer-motion";
import {
  X, CheckCircle2, AlertCircle, Download, Copy, ExternalLink, Loader2,
  Terminal, Cpu, StopCircle,
} from "lucide-react";

import { rpc, useSidecarProgress } from "../hooks/useSidecar";
import { useOperationStore } from "../stores";
import type { InstallResult, PreflightResult, WSLStatus } from "../types";

interface Props {
  preflight: PreflightResult;
  onRefresh: (next: PreflightResult) => void;
  onClose: () => void;
  onReady: () => void;
}

type Action = "wsl" | "distro" | "vibechek" | "models" | null;

export function PreflightDialog({ preflight, onRefresh, onClose, onReady }: Props) {
  const begin = useOperationStore((s) => s.begin);
  const finish = useOperationStore((s) => s.finish);
  const fail = useOperationStore((s) => s.fail);

  const [busyAction, setBusyAction] = useState<Action>(null);
  const [actionMessage, setActionMessage] = useState<string | null>(null);
  // Live log lines accumulated from sidecar:progress while a step is running.
  const [logLines, setLogLines] = useState<string[]>([]);
  const logRef = useRef<HTMLDivElement>(null);

  // Subscribe to progress notifications; only collect while a step is busy
  useSidecarProgress((evt) => {
    if (!busyAction) return;
    setLogLines((prev) => {
      const next = [...prev, evt.message || `${evt.current}/${evt.total}`];
      return next.length > 200 ? next.slice(-200) : next;
    });
  });

  // Auto-scroll the log to the bottom on new lines
  useEffect(() => {
    if (logRef.current) {
      logRef.current.scrollTop = logRef.current.scrollHeight;
    }
  }, [logLines]);

  const handleCancel = async () => {
    try {
      await rpc("cancel_operation");
    } catch {
      /* nothing to do; server will surface the error if any */
    }
  };

  const isWindows = preflight.wsl?.is_windows ?? false;

  const reCheck = async (autoCloseIfReady = true) => {
    const next = await rpc<PreflightResult>("preflight", {});
    onRefresh(next);
    if (autoCloseIfReady && next.ready) onReady();
    return next;
  };

  const runWithProgress = async <T extends InstallResult>(
    action: Action,
    method: string,
    params: object = {},
  ): Promise<T | null> => {
    setBusyAction(action);
    setActionMessage(null);
    setLogLines([]);
    begin("download-models"); // generic "busy" indicator
    try {
      const result = await rpc<T>(method, params);
      if (!result.ok) {
        fail(result.error ?? "install failed");
        setActionMessage(result.error ?? null);
        return null;
      }
      finish();
      await reCheck();
      return result;
    } catch (e) {
      fail(String(e));
      setActionMessage(String(e));
      return null;
    } finally {
      setBusyAction(null);
    }
  };

  const handleInstallWsl = async () => {
    await runWithProgress<InstallResult>("wsl", "install_wsl", {
      distro: preflight.wsl?.recommended_distro ?? "Ubuntu-24.04",
    });
  };

  const handleInstallDistro = async () => {
    await runWithProgress<InstallResult>("distro", "install_wsl", {
      distro: preflight.wsl?.recommended_distro ?? "Ubuntu-24.04",
    });
  };

  const handleInstallVibecheckInWsl = async (distro: string) => {
    await runWithProgress<InstallResult>("vibechek", "install_vibechek_in_wsl", {
      distro,
    });
  };

  const handleDownloadModels = async () => {
    setBusyAction("models");
    begin("download-models");
    try {
      await rpc("download_models", { models_dir: preflight.models.models_dir });
      finish();
      await reCheck();
    } catch (e) {
      fail(String(e));
    } finally {
      setBusyAction(null);
    }
  };

  return (
    <motion.div
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      exit={{ opacity: 0 }}
      className="fixed inset-0 z-50 bg-black/60 flex items-center justify-center px-4"
      onClick={onClose}
    >
      <motion.div
        initial={{ scale: 0.96, y: 10 }}
        animate={{ scale: 1, y: 0 }}
        className="panel max-w-xl w-full max-h-[85vh] flex flex-col"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex items-start gap-3 px-5 py-4 border-b border-white/5">
          <AlertCircle className="w-6 h-6 text-accent-yellow flex-none mt-0.5" />
          <div className="flex-1">
            <h2 className="font-display font-semibold text-lg">
              Setup needed before analyze
            </h2>
            <p className="text-sm text-white/60 mt-0.5">
              {isWindows
                ? "Vibechek can set everything up for you automatically. Each step below is a one-click install."
                : "Fix the items below, then click Re-check."}
            </p>
          </div>
          <button onClick={onClose} className="text-white/40 hover:text-white -m-1 p-1">
            <X className="w-5 h-5" />
          </button>
        </div>

        {/* Body */}
        <div className="flex-1 overflow-auto px-5 py-4 space-y-4">
          {isWindows ? (
            <WindowsFlow
              preflight={preflight}
              busyAction={busyAction}
              onInstallWsl={handleInstallWsl}
              onInstallDistro={handleInstallDistro}
              onInstallVibechekInWsl={handleInstallVibecheckInWsl}
            />
          ) : (
            <UnixEssentiaRow check={preflight.essentia} />
          )}

          <ModelsRow
            check={preflight.models}
            busy={busyAction === "models"}
            onDownload={handleDownloadModels}
          />

          {actionMessage && (
            <div className="panel-pad bg-accent-red/10 border-accent-red/30 text-xs text-accent-red">
              {actionMessage}
            </div>
          )}

          {busyAction && logLines.length > 0 && (
            <div className="panel">
              <div className="flex items-center justify-between px-3 py-2 border-b border-white/5">
                <div className="flex items-center gap-2 text-xs text-white/60">
                  <Terminal className="w-3.5 h-3.5" />
                  Live progress
                </div>
                <button
                  onClick={handleCancel}
                  className="text-xs text-accent-red hover:underline flex items-center gap-1"
                  title="Stop this step"
                >
                  <StopCircle className="w-3.5 h-3.5" />
                  Cancel
                </button>
              </div>
              <div
                ref={logRef}
                className="px-3 py-2 max-h-32 overflow-auto font-mono text-[11px] text-white/60 space-y-0.5"
              >
                {logLines.map((line, i) => (
                  <div key={i} className="truncate">{line}</div>
                ))}
              </div>
            </div>
          )}
        </div>

        {/* Footer */}
        <div className="flex items-center justify-between gap-2 px-5 py-3 border-t border-white/5">
          <div className="text-xs text-white/40">
            {preflight.analyze_via && (
              <>
                analyze will run:{" "}
                <span className="text-white/80 font-mono">{preflight.analyze_via}</span>
              </>
            )}
          </div>
          <div className="flex items-center gap-2">
            <button className="btn-ghost" onClick={onClose} disabled={!!busyAction}>
              Close
            </button>
            <button
              className="btn-primary"
              onClick={() => reCheck()}
              disabled={busyAction !== null}
            >
              Re-check
            </button>
          </div>
        </div>
      </motion.div>
    </motion.div>
  );
}

// ===========================================================================
// Windows flow — chained: WSL → distro → vibechek+essentia in WSL
// ===========================================================================

interface WindowsFlowProps {
  preflight: PreflightResult;
  busyAction: Action;
  onInstallWsl: () => void;
  onInstallDistro: () => void;
  onInstallVibechekInWsl: (distro: string) => void;
}

function WindowsFlow({
  preflight,
  busyAction,
  onInstallWsl,
  onInstallDistro,
  onInstallVibechekInWsl,
}: WindowsFlowProps) {
  const wsl: WSLStatus = preflight.wsl!;
  const distro = wsl.distros[0]; // The default / first available distro

  // Step 1: WSL itself
  if (!wsl.wsl_feature_enabled) {
    return (
      <Step
        ok={false}
        title="WSL (Windows Subsystem for Linux)"
        sub="Not enabled on this machine"
        info="Vibechek runs the ML analysis inside a tiny Ubuntu environment. WSL is a Windows-native feature; install takes 5-15 minutes and triggers a UAC prompt."
        action={
          <ActionButton
            label="Install WSL + Ubuntu"
            busy={busyAction === "wsl"}
            onClick={onInstallWsl}
          />
        }
      />
    );
  }

  // Step 2: No distro yet
  if (wsl.distros.length === 0) {
    return (
      <Step
        ok={false}
        title="Ubuntu distribution"
        sub="WSL is enabled but no distro is installed"
        action={
          <ActionButton
            label="Install Ubuntu"
            busy={busyAction === "distro"}
            onClick={onInstallDistro}
          />
        }
      />
    );
  }

  // Step 3: Distro present but missing vibechek/essentia
  const ready = wsl.can_run_vibechek;
  if (!ready) {
    const target = distro.name;
    return (
      <>
        <Step
          ok={true}
          title="WSL + Ubuntu"
          sub={`${target} is installed and ready`}
        />
        <Step
          ok={false}
          title={`Vibechek + Essentia in ${target}`}
          sub="The Python side isn't installed in your WSL distro yet"
          info={
            <>
              Vibechek will run: <code>apt install python3-pip libchromaprint-tools</code>,
              then <code>pip install essentia-tensorflow vibechek</code>. Takes 3-5 minutes.
              No admin prompt — runs entirely inside your distro.
            </>
          }
          action={
            <ActionButton
              icon={<Terminal className="w-4 h-4" />}
              label={`Install in ${target}`}
              busy={busyAction === "vibechek"}
              onClick={() => onInstallVibechekInWsl(target)}
            />
          }
        />
      </>
    );
  }

  // All green
  return (
    <Step
      ok={true}
      title={`WSL: ${wsl.usable_distro} ready`}
      sub="Analyze will route through WSL automatically"
    />
  );
}

// ===========================================================================
// Non-Windows essentia row (Linux / macOS just need pip)
// ===========================================================================

function UnixEssentiaRow({ check }: { check: PreflightResult["essentia"] }) {
  return (
    <Step
      ok={check.installed}
      title="Essentia (Python ML library)"
      sub={check.installed
        ? `Installed${check.version ? ` (${check.version})` : ""}`
        : (check.error ?? "Not installed")}
      info={!check.installed && "Install with:"}
      extra={!check.installed && <CodeBlock>pip install essentia-tensorflow</CodeBlock>}
    />
  );
}

// ===========================================================================
// Models row
// ===========================================================================

function ModelsRow({
  check,
  busy,
  onDownload,
}: {
  check: PreflightResult["models"];
  busy: boolean;
  onDownload: () => void;
}) {
  const ok = check.missing.length === 0;
  return (
    <Step
      ok={ok}
      title="ML model files (~200 MB)"
      sub={ok
        ? `${check.found.length} models, ${check.total_size_mb.toFixed(0)} MB in ${check.models_dir}`
        : `${check.missing.length} of ${check.found.length + check.missing.length} models missing`}
      info={!ok && "Vibechek will download these from essentia.upf.edu."}
      action={!ok && (
        <ActionButton
          icon={<Download className="w-4 h-4" />}
          label="Download models"
          busy={busy}
          onClick={onDownload}
        />
      )}
    />
  );
}

// ===========================================================================
// Reusable primitives
// ===========================================================================

interface StepProps {
  ok: boolean;
  title: string;
  sub: string;
  info?: React.ReactNode;
  extra?: React.ReactNode;
  action?: React.ReactNode;
}

function Step({ ok, title, sub, info, extra, action }: StepProps) {
  return (
    <div className="panel-pad">
      <div className="flex items-start gap-3">
        {ok ? (
          <CheckCircle2 className="w-5 h-5 text-accent-green flex-none mt-0.5" />
        ) : (
          <AlertCircle className="w-5 h-5 text-accent-red flex-none mt-0.5" />
        )}
        <div className="flex-1 min-w-0">
          <div className="text-sm font-medium text-white">{title}</div>
          <div className={`text-xs mt-0.5 ${ok ? "text-accent-green" : "text-accent-red"}`}>
            {sub}
          </div>
          {(info || extra || action) && !ok && (
            <div className="mt-3 space-y-2 text-sm text-white/70">
              {info && <div>{info}</div>}
              {extra}
              {action && <div className="pt-1">{action}</div>}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function ActionButton({
  label,
  icon,
  busy,
  onClick,
}: {
  label: string;
  icon?: React.ReactNode;
  busy: boolean;
  onClick: () => void;
}) {
  return (
    <button className="btn-primary" onClick={onClick} disabled={busy}>
      {busy ? (
        <>
          <Loader2 className="w-4 h-4 animate-spin" />
          Working...
        </>
      ) : (
        <>
          {icon ?? <Cpu className="w-4 h-4" />}
          {label}
        </>
      )}
    </button>
  );
}

function CodeBlock({ children }: { children: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="relative">
      <pre className="bg-surface-300 border border-white/10 rounded-md p-3 text-xs font-mono text-white/80 overflow-x-auto whitespace-pre">
        {children}
      </pre>
      <button
        onClick={() => {
          void navigator.clipboard.writeText(children);
          setCopied(true);
          setTimeout(() => setCopied(false), 1500);
        }}
        className="absolute top-2 right-2 p-1 rounded text-white/40 hover:text-white hover:bg-white/10"
        title="Copy"
      >
        {copied ? (
          <CheckCircle2 className="w-3.5 h-3.5 text-accent-green" />
        ) : (
          <Copy className="w-3.5 h-3.5" />
        )}
      </button>
    </div>
  );
}

// Unused helpers retained for the help text on unsupported platforms
export const _UNUSED = { ExternalLink };
