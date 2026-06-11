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
  X, CheckCircle2, AlertCircle, Download, Loader2,
  Terminal, Cpu, StopCircle, Wrench,
} from "lucide-react";

import { isCancellation, rpc, useSidecarProgress } from "../hooks/useSidecar";
import { useConfigStore, useNotificationStore, useOperationStore } from "../stores";
import type { InstallResult, PreflightResult, WSLStatus } from "../types";

interface Props {
  preflight: PreflightResult;
  onRefresh: (next: PreflightResult) => void;
  onClose: () => void;
  onReady: () => void;
}

type Action = "wsl" | "distro" | "vibechek" | "models" | null;

export function PreflightDialog({ preflight, onRefresh, onClose, onReady }: Props) {
  const active = useOperationStore((s) => s.active);
  const begin = useOperationStore((s) => s.begin);
  const finish = useOperationStore((s) => s.finish);
  const fail = useOperationStore((s) => s.fail);
  const notify = useNotificationStore((s) => s.notify);
  // The selected inference engine drives which venv/models the dialog checks +
  // installs, so ONNX is gated and provisioned exactly like the TF path.
  const engine = useConfigStore((s) => s.config.analysis.inference_engine);
  const isOnnx = engine === "onnx";

  const [busyAction, setBusyAction] = useState<Action>(null);
  const [actionMessage, setActionMessage] = useState<string | null>(null);
  // Sticky note from install_wsl letting the user know a reboot might be
  // needed before WSL is fully usable. Rendered above the live log.
  const [postInstallNote, setPostInstallNote] = useState<string | null>(null);
  const [repairBusy, setRepairBusy] = useState(false);
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
    // After an install we MUST do the slow WSL probe — that's the only way to
    // detect that essentia just landed inside a distro. Ask the sidecar for a
    // full (non-quick) preflight in a single round-trip. The dialog is
    // already user-blocking; the extra 5-10s for the slow probe is fine here.
    const next = await rpc<PreflightResult>("preflight", { quick: false, engine });
    onRefresh(next);
    if (autoCloseIfReady && next.ready) onReady();
    return next;
  };

  const runWithProgress = async <T extends InstallResult>(
    action: Action,
    method: string,
    params: object = {},
  ): Promise<T | null> => {
    // Don't clobber a long op that's already running (a 30-min genre setup,
    // an analyze): begin() would overwrite the global active/progress state,
    // the sidecar would busy-reject this call anyway, and the fail() below
    // would then clear `active` while the REAL op was still running — every
    // active-gated button in the app would re-enable mid-operation.
    if (useOperationStore.getState().active !== null) {
      setActionMessage("Another operation is running — wait for it to finish (or cancel it) first.");
      return null;
    }
    setBusyAction(action);
    setActionMessage(null);
    setPostInstallNote(null);
    setLogLines([]);
    begin("download-models"); // generic "busy" indicator
    try {
      const result = await rpc<T>(method, params);
      if (!result.ok) {
        // previously called `finish()` even when the
        // op reported failure, which left the global op state inconsistent.
        // Route through `fail()` (which handles the cancellation case too).
        fail(result.error ?? "install failed");
        setActionMessage(result.error ?? null);
        return null;
      }
      finish();
      // install_wsl returns a `note` ("may need a
      // reboot...") that we used to drop on the floor. Surface it.
      if (result.note) setPostInstallNote(result.note);
      await reCheck();
      return result;
    } catch (e) {
      fail(e);
      // Cancellations are intentional and already handled by `fail` — don't
      // also flash a red error block in the dialog body.
      if (!isCancellation(e)) {
        const msg =
          typeof e === "object" && e !== null && "message" in e
            ? String((e as { message: unknown }).message)
            : String(e);
        setActionMessage(msg);
      }
      return null;
    } finally {
      setBusyAction(null);
    }
  };

  /**
   * Explicit "Repair WSL shim" affordance.
   *
   * The cuda-env.sh patch in pre-beta.10 builds wrote a bash line into the
   * Python entry-point script, breaking analyze with a SyntaxError. The
   * sidecar already auto-repairs on probe, but we expose a manual button
   * for users who hit the broken state after upgrading.
   */
  const handleRepairWslShim = async () => {
    const distro = preflight.wsl?.usable_distro
      ?? preflight.wsl?.distros[0]?.name
      ?? null;
    if (!distro) {
      notify("No usable WSL distro to repair.", { kind: "info" });
      return;
    }
    setRepairBusy(true);
    try {
      const r = await rpc<{
        ok: boolean;
        repaired?: boolean;
        message?: string;
        error?: string;
      }>("repair_wsl_shim", { distro });
      if (r.ok) {
        notify(r.message ?? "Shim repair completed.", { kind: "success" });
        await reCheck(false);
      } else {
        notify(`Repair failed: ${r.error ?? "unknown"}`, { kind: "info" });
      }
    } catch (e) {
      if (!isCancellation(e)) {
        const msg =
          typeof e === "object" && e !== null && "message" in e
            ? String((e as { message: unknown }).message)
            : String(e);
        notify(`Repair failed: ${msg}`, { kind: "info" });
      }
    } finally {
      setRepairBusy(false);
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
      engine,
    });
  };

  const handleInstallEssentiaNative = async () => {
    await runWithProgress<InstallResult>("vibechek", "install_essentia_native", { engine });
  };

  const handleDownloadModels = async () => {
    setBusyAction("models");
    begin("download-models");
    try {
      await rpc("download_models", { models_dir: preflight.models.models_dir, engine });
      finish();
      await reCheck();
    } catch (e) {
      fail(e);
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
      // don't let a stray backdrop click dismiss the
      // dialog mid-install. The user would lose the live progress log and
      // the post-install note; the underlying install keeps running but they
      // can't see it. Suppress the click while any step is busy.
      onClick={busyAction ? undefined : onClose}
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
              Vibechek can set everything up for you automatically. Each step
              below is a one-click install.
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
              isOnnx={isOnnx}
              onInstallWsl={handleInstallWsl}
              onInstallDistro={handleInstallDistro}
              onInstallVibechekInWsl={handleInstallVibecheckInWsl}
            />
          ) : (
            <UnixEssentiaFlow
              preflight={preflight}
              busyAction={busyAction}
              isOnnx={isOnnx}
              onInstallEssentiaNative={handleInstallEssentiaNative}
            />
          )}

          <ModelsRow
            check={preflight.models}
            busy={busyAction === "models"}
            isOnnx={isOnnx}
            onDownload={handleDownloadModels}
          />

          {actionMessage && (
            <div className="panel-pad bg-accent-red/10 border-accent-red/30 text-xs text-accent-red">
              {actionMessage}
            </div>
          )}

          {/* surface install_wsl's "may need a reboot"
              note. Persists across re-renders until the next install runs. */}
          {postInstallNote && (
            <div className="panel-pad bg-accent-yellow/10 border-accent-yellow/30 text-xs text-accent-yellow/90">
              {postInstallNote}
            </div>
          )}

          {busyAction && logLines.length > 0 && (
            <div className="panel">
              <div className="flex items-center justify-between px-3 py-2 border-b border-white/5">
                <div className="flex items-center gap-2 text-xs text-white/60">
                  <Terminal className="w-3.5 h-3.5" />
                  Live progress
                </div>
                {/* only offer Cancel when there is an
                    actually-cancellable op in flight. Without the active
                    check we'd send a no-op cancel_operation that confuses
                    the user when nothing happens. */}
                {active !== null && (
                  <button
                    onClick={handleCancel}
                    className="text-xs text-accent-red hover:underline flex items-center gap-1"
                    title="Stop this step"
                  >
                    <StopCircle className="w-3.5 h-3.5" />
                    Cancel
                  </button>
                )}
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

          {/* Troubleshooting affordance — small, low-visibility "repair shim"
              button. Only shown when WSL is at least
              present; otherwise there's nothing to repair. */}
          {(preflight.wsl?.distros.length ?? 0) > 0 && (
            <div className="text-[11px] text-white/40 flex items-center gap-2 pt-1">
              <Wrench className="w-3 h-3" />
              <span>Analyze failing with a SyntaxError in WSL?</span>
              <button
                onClick={handleRepairWslShim}
                disabled={repairBusy || busyAction !== null}
                className="underline hover:text-white/70 disabled:opacity-50"
              >
                {repairBusy ? "Repairing…" : "Repair WSL shim"}
              </button>
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
  isOnnx: boolean;
  onInstallWsl: () => void;
  onInstallDistro: () => void;
  onInstallVibechekInWsl: (distro: string) => void;
}

function WindowsFlow({
  preflight,
  busyAction,
  isOnnx,
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
          title={isOnnx ? `Vibechek + ONNX engine in ${target}` : `Vibechek + Essentia in ${target}`}
          sub={isOnnx
            ? "The TF-free ONNX engine isn't installed in your WSL distro yet"
            : "The Python side isn't installed in your WSL distro yet"}
          info={
            <>
              Vibechek will run: <code>apt install python3-pip libchromaprint-tools</code>,
              then{" "}
              {isOnnx ? (
                <><code>pip install essentia onnxruntime vibechek</code> into a separate{" "}
                <code>~/.vibechek/venv-onnx</code> (plain Essentia + ONNX Runtime, no TensorFlow)</>
              ) : (
                <><code>pip install essentia-tensorflow vibechek</code></>
              )}. Takes 3-5 minutes. No admin prompt — runs entirely inside your distro.
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
// Non-Windows essentia flow (Linux / macOS — one-click managed-venv install)
// ===========================================================================

interface UnixFlowProps {
  preflight: PreflightResult;
  busyAction: Action;
  isOnnx: boolean;
  onInstallEssentiaNative: () => void;
}

function UnixEssentiaFlow({
  preflight,
  busyAction,
  isOnnx,
  onInstallEssentiaNative,
}: UnixFlowProps) {
  const sidecarHasIt = preflight.essentia.installed;
  const nv = preflight.native_venv;

  // Case 1: essentia is already in the sidecar's own Python (rare — only
  // when running from a dev checkout where the user pip-installed
  // essentia-tensorflow into the same venv).
  if (sidecarHasIt) {
    return (
      <Step
        ok={true}
        title="Essentia (Python ML library)"
        sub={`Installed in the sidecar process${preflight.essentia.version ? ` (${preflight.essentia.version})` : ""}`}
      />
    );
  }

  // Case 2: essentia is in the managed venv — analyze will route there.
  if (nv?.essentia_installed && nv?.vibechek_installed) {
    return (
      <Step
        ok={true}
        title="Essentia (managed venv)"
        sub={`Installed at ${nv.venv_dir}${nv.essentia_version ? ` (${nv.essentia_version})` : ""}`}
      />
    );
  }

  // Case 3: not installed — show the one-click install button.
  return (
    <Step
      ok={false}
      title="Essentia (Python ML library)"
      sub={
        nv?.supported === false
          ? "Native install not supported on this OS"
          : "Not installed yet"
      }
      info={
        <>
          Vibechek will create a managed Python virtual environment at{" "}
          <code>{nv?.venv_dir ?? (isOnnx ? "~/.vibechek/venv-onnx" : "~/.vibechek/venv")}</code>{" "}
          and install{" "}
          {isOnnx ? (
            <><code>essentia</code> + <code>onnxruntime</code> (no TensorFlow)</>
          ) : (
            <><code>essentia-tensorflow</code></>
          )}{" "}
          + <code>vibechek</code> into it. Takes ~3-5 minutes. No admin prompt;
          doesn&apos;t touch your system Python.
        </>
      }
      action={
        <ActionButton
          icon={<Terminal className="w-4 h-4" />}
          label="Install Essentia"
          busy={busyAction === "vibechek"}
          onClick={onInstallEssentiaNative}
        />
      }
    />
  );
}

// ===========================================================================
// Models row
// ===========================================================================

function ModelsRow({
  check,
  busy,
  isOnnx,
  onDownload,
}: {
  check: PreflightResult["models"];
  busy: boolean;
  isOnnx: boolean;
  onDownload: () => void;
}) {
  const ok = check.missing.length === 0;
  return (
    <Step
      ok={ok}
      title={isOnnx ? "ML model files (ONNX)" : "ML model files (~200 MB)"}
      sub={ok
        ? `${check.found.length} models, ${check.total_size_mb.toFixed(0)} MB in ${check.models_dir}`
        : `${check.missing.length} of ${check.found.length + check.missing.length} models missing`}
      info={!ok && (isOnnx
        ? "Vibechek will download the converted ONNX models from its release mirror."
        : "Vibechek will download these from essentia.upf.edu.")}
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

// CodeBlock helper removed: the Unix essentia flow no longer asks the user
// to copy a pip command — Vibechek installs Essentia for them now via
// `install_essentia_native`. If a future flow needs an inline copyable
// snippet, restore from git history.

