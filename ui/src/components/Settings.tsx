import { useEffect, useRef, useState } from "react";
import { open as openDialog } from "@tauri-apps/plugin-dialog";
import {
  Download, Cpu, FolderOpen, Settings as SettingsIcon, Shield,
  Zap, AlertTriangle, CheckCircle2, RotateCcw, ChevronDown, ChevronRight,
  FileText, Wrench, StopCircle, HelpCircle, Disc3, Loader2, RefreshCw,
} from "lucide-react";
import { AnimatePresence } from "framer-motion";

import { useConfigStore, useNotificationStore, useOperationStore } from "../stores";
import { newOpId, progressMatches } from "../stores/operation";
import { isCancellation, rpc, sidecarStatus, useSidecarProgress } from "../hooks/useSidecar";
import { setupOnnxEngine, setupClapEngine, setupGenreResolver } from "../api/rpc";
import {
  listProfiles, loadProfile, doctor as runDoctor, verifyModels,
  upgradeVibechekInWSL,
} from "../api/rpc";
import type { ListProfilesResult } from "../api/methods";
import type { EngineGpuInfo, GpuDevice, PreflightResult, SystemResources, VibechekConfig } from "../types";
import { useUpdater } from "../hooks/useUpdater";
import { ConfirmModal } from "./ConfirmModal";
import { LogsViewer } from "./LogsViewer";
import { PreflightDialog } from "./PreflightDialog";
import { OnnxSetupDialog, type OnnxSetupState } from "./OnnxSetupDialog";
import { GenreSetupDialog, type GenreSetupState } from "./GenreSetupDialog";

// The workers slider used to cap at sysInfo.cpu_count,
// which silently truncated a user-set value when sysInfo loaded asynchronously
// (race: type 32, sysInfo arrives with cpu_count=8, slider clamps to 8). Use
// a generous static ceiling instead — anyone with > 96 cores is going to set
// this via config.toml anyway, and we now display a warning when their value
// exceeds the detected core count.
const WORKERS_MAX = 96;

export function Settings() {
  const cfg = useConfigStore((s) => s.config);
  const setConfig = useConfigStore((s) => s.setConfig);
  const updateAnalysis = useConfigStore((s) => s.updateAnalysis);
  const updateTagging = useConfigStore((s) => s.updateTagging);
  const updateDuplicates = useConfigStore((s) => s.updateDuplicates);
  const updateOrganization = useConfigStore((s) => s.updateOrganization);

  const active = useOperationStore((s) => s.active);
  const begin = useOperationStore((s) => s.begin);
  const finish = useOperationStore((s) => s.finish);
  const fail = useOperationStore((s) => s.fail);

  const [showAdvanced, setShowAdvanced] = useState(false);
  const [showLogs, setShowLogs] = useState(false);
  const [showPreflightDialog, setShowPreflightDialog] = useState(false);
  // restoring all defaults is destructive — gate it
  // behind a confirm modal so a stray click can't wipe a tweaked setup.
  const [showRestoreConfirm, setShowRestoreConfirm] = useState(false);

  // track mount so async preflight / sysInfo callbacks
  // don't call setState after the component unmounts (the user switched
  // tabs while the slow WSL probe was in flight).
  const isMounted = useRef(true);
  useEffect(() => {
    isMounted.current = true;
    return () => {
      isMounted.current = false;
    };
  }, []);

  // handleDownloadModels used to close over the
  // initial `cfg.analysis.models_dir`. If the user edited the field after
  // the component mounted, the closure kept the old value. Read from the
  // store at call time via a ref instead.
  const cfgRef = useRef(cfg);
  useEffect(() => {
    cfgRef.current = cfg;
  }, [cfg]);

  const handleRestoreAll = async () => {
    setShowRestoreConfirm(false);
    try {
      const result = await rpc<{ config: VibechekConfig }>("restore_default_config");
      if (!isMounted.current) return;
      setConfig(result.config, true);
    } catch (e) {
      fail(e);
    }
  };

  const [sidecarBinary, setSidecarBinary] = useState<string | null>(null);
  const [sysInfo, setSysInfo] = useState<SystemResources | null>(null);
  const [preflightResult, setPreflightResult] = useState<PreflightResult | null>(null);
  // models_dir is a free-text input. Validate on blur
  // (cheap `scan_directory` quick call) so an obvious typo or stale path
  // surfaces before the user kicks off a download or analyze.
  const [modelsDirWarning, setModelsDirWarning] = useState<string | null>(null);
  // Engine-side GPU truth — what TF inside WSL (or native) actually sees.
  // Probed lazily after preflight resolves; null means "haven't asked yet",
  // engineProbing=true means "asking, ~10s wait".
  const [engineGpu, setEngineGpu] = useState<EngineGpuInfo | null>(null);
  const [engineProbing, setEngineProbing] = useState(false);

  const notify = useNotificationStore((s) => s.notify);

  // ---- DJ profiles (list_profiles / load_profile) ----
  const [profiles, setProfiles] = useState<ListProfilesResult["profiles"]>([]);
  const [profileBusy, setProfileBusy] = useState<string | null>(null);
  useEffect(() => {
    let cancelled = false;
    listProfiles()
      .then((r) => { if (!cancelled) setProfiles(r.profiles); })
      .catch(() => { /* non-fatal — the picker just stays empty */ });
    return () => { cancelled = true; };
  }, []);

  const handleLoadProfile = async (name: string) => {
    setProfileBusy(name);
    try {
      const res = await loadProfile({ name });
      // load_profile applies + persists server-side and returns the new config.
      if (res.config) setConfig(res.config as VibechekConfig, true);
      notify(`Applied "${name}" profile`, { kind: "success" });
    } catch (e) {
      notify(
        typeof e === "object" && e !== null && "message" in e
          ? `Couldn't load profile: ${String((e as { message: unknown }).message)}`
          : `Couldn't load profile: ${String(e)}`,
        { kind: "info" },
      );
    } finally {
      if (isMounted.current) setProfileBusy(null);
    }
  };

  // ---- Diagnostics & maintenance (doctor / verify_models / upgrade WSL) ----
  const [diagBusy, setDiagBusy] = useState<
    null | "doctor" | "verify" | "upgrade" | "onnx-setup" | "clap-setup" | "resolver-setup"
  >(null);
  const [onnxSetup, setOnnxSetup] = useState<OnnxSetupState>(null);
  const [clapSetup, setClapSetup] = useState<GenreSetupState>(null);
  const [resolverSetup, setResolverSetup] = useState<GenreSetupState>(null);

  const handleCopyDiagnostic = async () => {
    setDiagBusy("doctor");
    try {
      const res = await runDoctor();
      await navigator.clipboard.writeText(res.markdown);
      notify("Diagnostic copied to clipboard", {
        kind: "success",
        detail: "Paste it into a GitHub issue or support thread.",
      });
    } catch (e) {
      notify(
        typeof e === "object" && e !== null && "message" in e
          ? `Diagnostic failed: ${String((e as { message: unknown }).message)}`
          : `Diagnostic failed: ${String(e)}`,
        { kind: "info" },
      );
    } finally {
      if (isMounted.current) setDiagBusy(null);
    }
  };

  const handleVerifyModels = async () => {
    setDiagBusy("verify");
    try {
      const res = await verifyModels();
      const bad = res.results.filter((r) => r.ok === false);
      const unpinned = res.results.filter((r) => r.ok === null);
      const okCount = res.results.filter((r) => r.ok === true).length;
      if (bad.length > 0) {
        notify(`${bad.length} model file(s) failed verification`, {
          kind: "info",
          detail: bad.map((b) => `${b.name}.${b.suffix}: ${b.reason ?? "mismatch"}`).join("\n"),
        });
      } else {
        notify("Models verified", {
          kind: "success",
          detail: `${okCount} matched the pinned hashes`
            + (unpinned.length ? `, ${unpinned.length} not yet pinned (OK).` : "."),
        });
      }
    } catch (e) {
      notify(
        typeof e === "object" && e !== null && "message" in e
          ? `Verify failed: ${String((e as { message: unknown }).message)}`
          : `Verify failed: ${String(e)}`,
        { kind: "info" },
      );
    } finally {
      if (isMounted.current) setDiagBusy(null);
    }
  };

  const handleUpgradeWsl = async () => {
    const distro = preflightResult?.wsl?.usable_distro;
    if (!distro) {
      notify("No usable WSL distro detected.", { kind: "info" });
      return;
    }
    setDiagBusy("upgrade");
    const opId = begin("install-essentia");
    try {
      const res = await upgradeVibechekInWSL({ distro }, opId);
      finish();
      if (res.ok) {
        notify("WSL Vibechek updated", {
          kind: "success",
          detail: "The WSL analyzer now matches the app version.",
        });
        refreshPreflight();
      } else {
        notify(`Update failed: ${res.error ?? "unknown error"}`, { kind: "info" });
      }
    } catch (e) {
      fail(e);
    } finally {
      if (isMounted.current) setDiagBusy(null);
    }
  };

  // Correlation id of the in-flight engine setup (ONNX / CLAP / resolver) —
  // passed to the corresponding dialog so it renders only ITS op's progress
  // events. One ref is enough: the sidecar serializes long ops, so at most one
  // of the three setup dialogs is in the running phase at a time.
  const engineSetupOpIdRef = useRef<string | null>(null);

  // Provision the TF-free ONNX engine: a separate managed venv (~/.vibechek/
  // venv-onnx) with plain essentia + onnxruntime. essentia and essentia-
  // tensorflow can't share a venv, so the ONNX engine gets its own. Idempotent
  // — safe to re-run. Routes to the WSL or native installer per analyze_via.
  const handleSetupOnnx = async () => {
    // One self-healing RPC handles everything: stage the bundled converted
    // heads, install the ONNX engine env if needed, fetch the EffNet backbone,
    // clean stale files, and verify. It auto-detects WSL vs native + the distro,
    // so there's no fast-click race to guard. The dialog shows live progress.
    setDiagBusy("onnx-setup");
    const opId = begin("install-essentia");
    engineSetupOpIdRef.current = opId;
    setOnnxSetup({ phase: "running" });
    try {
      const distro = preflightResult?.wsl?.usable_distro;
      const res = await setupOnnxEngine(distro ? { distro } : {}, opId);
      finish();
      if (res.cancelled) {
        // Cancel during the install step RESOLVES with cancelled=true (the
        // installer returns rather than raises) — close quietly instead of
        // rendering "Setup didn't finish — Cancelled by user" as an error.
        setOnnxSetup(null);
      } else if (res.ready) {
        setOnnxSetup({ phase: "done", staged: res.staged?.length ?? 0 });
        refreshPreflight();
      } else {
        setOnnxSetup({
          phase: "error",
          // Prefer the real install/fetch failure reason the sidecar reports;
          // fall back to the preflight reasons, then a generic message.
          error: res.error
            || (res.reasons_not_ready ?? []).join("; ")
            || "Setup did not complete.",
        });
      }
    } catch (e) {
      fail(e);
      if (!isCancellation(e)) {
        setOnnxSetup({ phase: "error", error: e instanceof Error ? e.message : String(e) });
      } else {
        setOnnxSetup(null);
      }
    } finally {
      if (isMounted.current) setDiagBusy(null);
    }
  };

  // Abort an in-flight ONNX setup. setup_onnx_engine is Cancellable on the
  // sidecar (it calls cancellation.check() during the network fetch/install),
  // so this actually stops the op. The in-flight setupOnnxEngine() promise then
  // rejects with a cancellation error, which handleSetupOnnx's catch maps to
  // isCancellation -> setOnnxSetup(null), closing the modal cleanly.
  const handleCancelOnnxSetup = async () => {
    try {
      await rpc("cancel_operation");
    } catch {
      /* user-cancel — sidecar handles its own logging */
    }
  };

  // Opt-in genre engine setups (CLAP audio student / online web resolver). Both
  // install heavy deps + download a multi-GB model into the analysis venv, with
  // live progress + cancellation — same shape as the ONNX setup above.
  const handleSetupGenreEngine = async (
    kind: "clap" | "resolver",
    setState: (s: GenreSetupState) => void,
    call: (p: { distro?: string; inference_engine?: string }, opId?: string) =>
      Promise<{ ok: boolean; error?: string | null; cancelled?: boolean }>,
  ) => {
    setDiagBusy(kind === "clap" ? "clap-setup" : "resolver-setup");
    const opId = begin("install-essentia");
    engineSetupOpIdRef.current = opId;
    setState({ phase: "running" });
    try {
      const distro = preflightResult?.wsl?.usable_distro;
      // Send the LIVE engine selection: the setup targets this engine's venv,
      // and the saved config can lag the selector by the autosave debounce.
      const engine = useConfigStore.getState().config.analysis.inference_engine;
      const res = await call({ ...(distro ? { distro } : {}), inference_engine: engine }, opId);
      finish();
      if (res.cancelled) {
        // A user-initiated Cancel resolves (not rejects) with cancelled=true —
        // close the dialog quietly instead of showing a scary error state.
        setState(null);
      } else if (res.ok) {
        setState({ phase: "done" });
        // Also toast: the dialog state is local to Settings, so a user who
        // switched tabs during the multi-minute install would otherwise get
        // ZERO completion feedback.
        notify(
          kind === "clap"
            ? "CLAP genre engine ready — re-analyze to use it"
            : "Online genre resolver ready — enable it and re-analyze",
          { kind: "success" },
        );
        refreshPreflight();
      } else {
        setState({ phase: "error", error: res.error || "Setup did not complete." });
      }
    } catch (e) {
      fail(e);
      if (!isCancellation(e)) {
        setState({ phase: "error", error: e instanceof Error ? e.message : String(e) });
      } else {
        setState(null);
      }
    } finally {
      if (isMounted.current) setDiagBusy(null);
    }
  };
  const handleSetupClap = () => handleSetupGenreEngine("clap", setClapSetup, setupClapEngine);
  const handleSetupResolver = () =>
    handleSetupGenreEngine("resolver", setResolverSetup, setupGenreResolver);
  const handleCancelGenreSetup = async () => {
    try {
      await rpc("cancel_operation");
    } catch {
      /* user-cancel */
    }
  };

  /**
   * Ask the actual analyze engine what GPUs it sees.
   * - Native essentia: ~instant.
   * - WSL: ~10s (boots TF inside the distro). We show a spinner.
   * Cached server-side for 5 min — `force=true` bypasses.
   */
  const refreshEngineGpu = (distro: string | null, force = false) => {
    if (!isMounted.current) return;
    setEngineProbing(true);
    // Read the engine at CALL time from the store, not from the captured `cfg`
    // closure: this fn is invoked from a mount-time effect whose closure can
    // hold the pre-disk-load default (essentia_tf). An onnx user would then get
    // their GPU block probed for the wrong engine until a manual re-probe.
    const probeEngine = useConfigStore.getState().config.analysis.inference_engine;
    rpc<EngineGpuInfo>("engine_gpu_status", { distro, force, engine: probeEngine })
      .then((info) => {
        if (isMounted.current) setEngineGpu(info);
      })
      .catch((e) => {
        if (!isMounted.current) return;
        setEngineGpu({
          engine: distro ? "wsl" : "native",
          distro,
          ok: false,
          gpu_available: false,
          gpu_count: 0,
          devices: [],
          gpu_hardware_visible: false,
          missing_cuda_libs: [],
          tf_version: null,
          tf_built_with_cuda: null,
          nvidia_driver: null,
          nvidia_smi_available: false,
          provider: null,
          runtime: null,
          // Unwrap RpcError.message (which String(e) would render as
          // "[object Object]"); fall back to String for non-Error rejections.
          error:
            typeof e === "object" && e !== null && "message" in e
              ? String((e as { message: unknown }).message)
              : String(e),
          probed_at: Date.now() / 1000,
        });
      })
      .finally(() => {
        if (isMounted.current) setEngineProbing(false);
      });
  };

  const refreshPreflight = () => {
    // Two-phase load: fast quick=true preflight for instant UI feedback,
    // then upgrade with a full quick=false call so we know the real WSL
    // state and can pick the right engine GPU probe target.
    rpc<PreflightResult>("preflight", { quick: true })
      .then((quick) => {
        if (!isMounted.current) return;
        setPreflightResult(quick);
        // Upgrade: full preflight (slow WSL probe on Windows, ~5-10s).
        // the full preflight can take up to ~10s
        // for cold WSL probes; sidecar.rs::timeout_for() categorises
        // "preflight" as QUICK (60s) which is enough headroom even when a
        // distro hasn't been booted in a while. If users start hitting
        // the 60s ceiling we'll need to either bump it or split preflight
        // into preflight + preflight_full with their own timeouts.
        rpc<PreflightResult>("preflight", { quick: false })
          .then((full) => {
            if (!isMounted.current) return;
            setPreflightResult(full);
            // Now that we know where analyze will run, ask the actual
            // engine what GPUs it sees. The probe is cached server-side;
            // first call inside WSL takes ~10s while TF loads.
            if (full.analyze_via === "wsl" && full.wsl?.usable_distro) {
              refreshEngineGpu(full.wsl.usable_distro);
            } else if (full.analyze_via === "native" || full.analyze_via === "native_venv") {
              refreshEngineGpu(null);
            }
          })
          .catch(() => {});
      })
      .catch(() => {});
  };

  const validateModelsDir = async (path: string) => {
    if (!path.trim()) {
      setModelsDirWarning(null);
      return;
    }
    try {
      // scan_directory is QUICK-tier on the sidecar (~instant). On error or
      // for a missing path the RPC rejects, which we treat as "warn the user
      // but don't block — they may be about to point at a directory the
      // download will create".
      await rpc("scan_directory", { path, recursive: false });
      if (isMounted.current) setModelsDirWarning(null);
    } catch (e) {
      if (!isMounted.current) return;
      const msg =
        typeof e === "object" && e !== null && "message" in e
          ? String((e as { message: unknown }).message)
          : String(e);
      setModelsDirWarning(
        `Couldn't read this directory: ${msg}. Vibechek will try to create it on download.`,
      );
    }
  };

  useEffect(() => {
    sidecarStatus()
      .then((s) => {
        if (isMounted.current) setSidecarBinary(s.binary);
      })
      .catch(() => {});
    rpc<SystemResources>("system_info")
      .then((info) => {
        if (!isMounted.current) return;
        setSysInfo(info);
        if (cfg.analysis.workers === 0) {
          updateAnalysis({ workers: info.recommended_workers });
        }
      })
      .catch(() => {});
    refreshPreflight();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Re-probe the engine GPU whenever the user switches inference engines.
  // The probe is engine-specific server-side (ONNX populates provider/runtime
  // and can see AMD/Intel/Apple GPUs; TF populates tf_version and is
  // NVIDIA-only), so without this the whole GPU panel — the System>GPU stat,
  // EngineGpuBlock, the acceleration hint, the "On" enabled/disabled state, and
  // the TF CUDA-wheels fixer — keeps rendering the PREVIOUS engine's truth until
  // a manual Re-probe or app restart. Seed the ref with the current engine so
  // the mount-time probe (in refreshPreflight) isn't duplicated on first run.
  const lastProbedEngine = useRef(cfg.analysis.inference_engine);
  useEffect(() => {
    if (lastProbedEngine.current === cfg.analysis.inference_engine) return;
    lastProbedEngine.current = cfg.analysis.inference_engine;
    const distro = preflightResult?.wsl?.usable_distro ?? null;
    // force=true so the engine-keyed server cache for the NEW engine is fetched
    // rather than returning the prior engine's cached probe.
    refreshEngineGpu(distro, true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cfg.analysis.inference_engine]);

  const handleDownloadModels = async () => {
    const opId = begin("download-models");
    try {
      // read the latest models_dir from the store
      // via cfgRef so we don't ship a stale value from the original render.
      await rpc("download_models", {
        models_dir: cfgRef.current.analysis.models_dir || undefined,
        op_id: opId,
      });
      finish();
      refreshPreflight();
    } catch (e) {
      fail(e);
    }
  };

  const handlePickDir = async (
    setter: (path: string) => void,
  ) => {
    const path = await openDialog({ directory: true, multiple: false });
    if (typeof path === "string") setter(path);
  };

  return (
    <div className="h-full overflow-auto px-6 py-5">
      <h1 className="font-display font-semibold text-2xl mb-6 flex items-center gap-2">
        <SettingsIcon className="w-6 h-6 text-accent" />
        Settings
      </h1>

      <PreflightSection
        preflight={preflightResult}
        onRefresh={refreshPreflight}
        onSetupClick={() => setShowPreflightDialog(true)}
      />

      <ResourcesSection
        sysInfo={sysInfo}
        engineGpu={engineGpu}
        engineProbing={engineProbing}
        analyzeVia={
          // Narrow the wire-level `string | null` to the literal union the
          // component expects. Anything unexpected falls back to null.
          // `native_venv` is a real value but the
          // old check dropped it, which made the engine GPU block render
          // the wrong copy when analyze routed through the managed venv.
          preflightResult?.analyze_via === "wsl" ||
          preflightResult?.analyze_via === "native" ||
          preflightResult?.analyze_via === "native_venv"
            ? preflightResult.analyze_via
            : null
        }
        preflight={preflightResult}
        onRefreshEngine={() => {
          const distro = preflightResult?.wsl?.usable_distro ?? null;
          refreshEngineGpu(distro, true);
        }}
        onRefreshPreflight={refreshPreflight}
      />

      <Section
        icon={<Cpu className="w-5 h-5" />}
        title="Analysis"
        subtitle="How much of your machine to use"
      >
        <Field label={`Worker processes ${sysInfo ? `(of ${sysInfo.cpu_count} cores)` : ""}`}>
          <div className="flex items-center gap-3">
            <input
              type="range"
              min={1}
              // static high ceiling instead of
              // sysInfo.cpu_count, which used to snap a user-set value
              // (e.g. 32) down when sysInfo loaded asynchronously with a
              // smaller cpu_count. Warning below if value > cpu_count.
              max={WORKERS_MAX}
              step={1}
              value={Math.max(1, cfg.analysis.workers)}
              onChange={(e) => updateAnalysis({ workers: Number(e.target.value) })}
              className="flex-1 accent-accent"
            />
            <span className="text-sm font-mono w-12 text-right tabular-nums">
              {Math.max(1, cfg.analysis.workers)}
            </span>
            {sysInfo && (
              <button
                className="btn-ghost text-xs"
                onClick={() =>
                  updateAnalysis({ workers: sysInfo.recommended_workers })
                }
                title={`Recommended: ${sysInfo.recommended_workers}`}
              >
                auto
              </button>
            )}
          </div>
          <Hint>
            Each worker holds ~500 MB of model weights in RAM. Best:{" "}
            <code>cpu_count − 1</code> for a responsive system,{" "}
            <code>cpu_count</code> for max throughput.
          </Hint>
          {sysInfo && cfg.analysis.workers > sysInfo.cpu_count && (
            <div className="text-xs text-accent-yellow/90 mt-1 flex items-start gap-1">
              <AlertTriangle className="w-3 h-3 flex-none mt-0.5" />
              <span>
                {cfg.analysis.workers} workers exceeds the {sysInfo.cpu_count}{" "}
                CPU cores Vibechek detected. The extra workers will compete
                for CPU time rather than add throughput.
              </span>
            </div>
          )}
        </Field>

        <Field label="GPU acceleration">
          <div className="flex gap-2">
            {(["auto", "on", "off"] as const).map((mode) => {
              // when no GPU is available "on" silently
              // falls back to CPU. Disable the button (with a tooltip) so the
              // user understands why their choice doesn't stick.
              const noGpu = engineGpu?.ok === true && engineGpu.gpu_available === false;
              const disabled = mode === "on" && noGpu;
              return (
                <button
                  key={mode}
                  onClick={() => !disabled && updateAnalysis({ use_gpu: mode })}
                  disabled={disabled}
                  title={
                    disabled
                      ? "No GPU available — analyze will run on CPU regardless."
                      : undefined
                  }
                  className={`btn ${
                    cfg.analysis.use_gpu === mode
                      ? "bg-accent text-white"
                      : "bg-white/5 text-white/70 hover:bg-white/10"
                  } ${disabled ? "opacity-40 cursor-not-allowed" : ""}`}
                >
                  {mode === "auto" && <Zap className="w-3.5 h-3.5" />}
                  <span className="capitalize">{mode}</span>
                </button>
              );
            })}
          </div>
          {/* Extra warning when use_gpu="on" is selected and engine reports no GPU */}
          {cfg.analysis.use_gpu === "on" && engineGpu?.ok && !engineGpu.gpu_available && (
            <div className="text-xs text-accent-yellow/90 mt-1 flex items-start gap-1">
              <AlertTriangle className="w-3 h-3 flex-none mt-0.5" />
              <span>
                "On" is selected but the analyze engine reports no usable GPU.
                Analyze will fall back to CPU.
              </span>
            </div>
          )}
          <Hint>
            {(() => {
              // The engine probe is ground truth — it asks TF (in WSL or
              // native) what it actually sees. Falls back to the host probe
              // until engine probe finishes (or if it's not applicable).
              if (engineProbing && !engineGpu) return "Asking the analyze engine what GPUs it sees…";
              if (engineGpu?.ok) {
                if (engineGpu.gpu_available) {
                  const dev = engineGpu.devices[0]?.name ?? "GPU";
                  const where = engineGpu.engine === "wsl"
                    ? ` (visible to TF inside ${engineGpu.distro})`
                    : "";
                  return `${dev}${where} — auto will use it.`;
                }
                if (engineGpu.nvidia_smi_available) {
                  return `NVIDIA driver ${engineGpu.nvidia_driver ?? "?"} is present but TensorFlow ${engineGpu.engine === "wsl" ? `inside ${engineGpu.distro}` : ""} can't see the GPU. Check CUDA/cuDNN versions inside the engine.`;
                }
                return "No GPU visible to the analyze engine. Stays on CPU.";
              }
              if (!sysInfo) return "Detecting GPU…";
              return sysInfo.gpu_available
                ? `${sysInfo.gpu_devices[0]?.name ?? "GPU"} detected on the host — actual GPU use depends on the engine.`
                : "No GPU detected. Stays on CPU regardless of choice.";
            })()}
          </Hint>
        </Field>

        {/* Hybrid CPU+GPU. Only meaningful when GPU mode isn't "off". */}
        <Toggle
          label="Hybrid CPU + GPU"
          checked={cfg.analysis.hybrid_cpu_gpu}
          onChange={(v) => updateAnalysis({ hybrid_cpu_gpu: v })}
          hint="Run GPU workers AND extra CPU workers together against a shared queue, so a small GPU worker-cap doesn't leave your cores idle. Whichever device finishes a track grabs the next — it self-balances. Ignored when GPU mode is off."
        />

        {/* Inference engine: essentia-tensorflow (default) vs ONNX Runtime.
            ONNX runs the SAME models through onnxruntime — cross-vendor GPU
            (AMD/Intel/Apple via DirectML/CoreML) and NO bundled TensorFlow.
            Selecting it provisions a separate engine (plain essentia +
            onnxruntime) on the next analyze. Validated to parity with the TF
            path — see docs/ONNX_MIGRATION.md. */}
        <Field label="Inference engine">
          <div className="flex gap-2">
            {([
              { id: "essentia_tf", label: "Essentia · TensorFlow" },
              { id: "onnx", label: "ONNX Runtime" },
            ] as const).map(({ id, label }) => (
              <button
                key={id}
                onClick={() => updateAnalysis({ inference_engine: id })}
                className={`btn ${
                  cfg.analysis.inference_engine === id
                    ? "bg-accent text-white"
                    : "bg-white/5 text-white/70 hover:bg-white/10"
                }`}
              >
                <span>{label}</span>
              </button>
            ))}
          </div>
          {cfg.analysis.inference_engine === "onnx" && (
            <div className="text-xs text-accent-yellow/90 mt-1 flex items-start gap-1">
              <AlertTriangle className="w-3 h-3 flex-none mt-0.5" />
              <span>
                Experimental. ONNX runs in a separate engine (plain Essentia +
                ONNX Runtime, no TensorFlow). Click <strong>Set up ONNX
                engine</strong> below to install it (one-time), then re-analyze
                your library so every track is scored by the same engine.
              </span>
            </div>
          )}
          {cfg.analysis.inference_engine === "onnx" && (
            <button
              className="btn-primary mt-2"
              onClick={handleSetupOnnx}
              disabled={diagBusy !== null || active !== null}
            >
              <Download className="w-4 h-4" />
              {diagBusy === "onnx-setup" ? "Setting up ONNX engine…" : "Set up ONNX engine"}
            </button>
          )}
          <Hint>
            <strong>Essentia · TensorFlow</strong> is the default (NVIDIA-only
            GPU).{" "}
            <strong>ONNX Runtime</strong> runs the same models with cross-vendor
            GPU — AMD, Intel, and Apple Silicon via DirectML/CoreML — and drops
            the end-of-life TensorFlow runtime entirely. Validated to match the
            default engine's output; see <code>docs/ONNX_MIGRATION.md</code>.
          </Hint>
        </Field>

        <Field label="Genre source (existing tag vs ML)">
          <select
            className="input"
            value={cfg.analysis.genre_source_policy}
            onChange={(e) =>
              updateAnalysis({
                genre_source_policy: e.target.value as
                  | "prefer_tag" | "prefer_ml" | "tag_only" | "ml_only",
              })
            }
          >
            <option value="prefer_tag">Prefer existing tag (recommended)</option>
            <option value="prefer_ml">Prefer ML analysis</option>
            <option value="tag_only">Existing tag only (never override)</option>
            <option value="ml_only">ML only (ignore existing tags)</option>
          </select>
          <Hint>
            Many libraries (e.g. Beatport downloads) already carry accurate genre
            tags. <strong>Prefer existing tag</strong> trusts a <em>specific</em>{" "}
            existing genre and uses ML only to fill gaps or override when it's very
            confident and disagrees — generic junk tags ("Dance/Pop", "Electronic")
            are ignored in favor of ML. <strong>ML only</strong> is pure audio.
          </Hint>
        </Field>

        <Field label="Genre classifier (audio model)">
          <div className="flex gap-2">
            {([
              { id: "discogs", label: "Discogs-EffNet (bundled)" },
              { id: "clap", label: "CLAP audio (better)" },
            ] as const).map(({ id, label }) => (
              <button
                key={id}
                onClick={() => updateAnalysis({ genre_classifier: id })}
                className={`btn ${
                  cfg.analysis.genre_classifier === id
                    ? "bg-accent text-white"
                    : "bg-white/5 text-white/70 hover:bg-white/10"
                }`}
              >
                <span>{label}</span>
              </button>
            ))}
          </div>
          {cfg.analysis.genre_classifier === "clap" && (
            <button
              className="btn-primary mt-2"
              onClick={handleSetupClap}
              disabled={diagBusy !== null || active !== null}
            >
              <Download className="w-4 h-4" />
              {diagBusy === "clap-setup" ? "Setting up CLAP…" : "Set up CLAP genre engine"}
            </button>
          )}
          <Hint>
            <strong>CLAP audio</strong> is a pure-audio genre model ~2x as accurate
            as the bundled Discogs model, and it works even on untagged tracks.
            One-time setup downloads a ~2.2 GB model; BPM/key/mood are unchanged.
            Falls back to Discogs if not set up.
          </Hint>
        </Field>

        <Field label="Online genre lookup">
          <Toggle
            label="Resolve genre online (local LLM + web)"
            checked={cfg.analysis.genre_web_lookup}
            onChange={(v) => updateAnalysis({ genre_web_lookup: v })}
          />
          {cfg.analysis.genre_web_lookup && (
            <button
              className="btn-primary mt-2"
              onClick={handleSetupResolver}
              disabled={diagBusy !== null || active !== null}
            >
              <Download className="w-4 h-4" />
              {diagBusy === "resolver-setup" ? "Setting up resolver…" : "Set up online resolver"}
            </button>
          )}
          <Hint>
            Looks up each track's genre online (a local LLM reads web results for
            the artist + title), then layers it into reconciliation (tag › web ›
            audio) — the most accurate option (~60%) on tagged libraries. Needs
            network + a local LLM (one-time ~4.7 GB setup, fully private). Adds time
            per track; off by default.
          </Hint>
        </Field>

        <Field label="Models directory">
          <div className="flex gap-2">
            <input
              type="text"
              className="input flex-1 font-mono text-xs"
              value={cfg.analysis.models_dir}
              placeholder="(default: user data dir)"
              onChange={(e) => updateAnalysis({ models_dir: e.target.value })}
              onBlur={(e) => void validateModelsDir(e.target.value)}
            />
            <button
              className="btn-ghost"
              onClick={() => {
                handlePickDir((p) => {
                  updateAnalysis({ models_dir: p });
                  void validateModelsDir(p);
                });
              }}
              aria-label="Choose models folder"
              title="Choose models folder"
            >
              <FolderOpen className="w-4 h-4" />
            </button>
          </div>
          <Hint>~800 MB total when downloaded.</Hint>
          {modelsDirWarning && (
            <div className="text-xs text-accent-yellow/90 mt-1 flex items-start gap-1">
              <AlertTriangle className="w-3 h-3 flex-none mt-0.5" />
              <span>{modelsDirWarning}</span>
            </div>
          )}
          <button
            className="btn-primary mt-2"
            onClick={handleDownloadModels}
            disabled={active !== null}
          >
            <Download className="w-4 h-4" />
            Download models now
          </button>
        </Field>
      </Section>

      {/* Advanced disclosure — hides things most users shouldn't touch */}
      <div className="mb-4">
        <button
          onClick={() => setShowAdvanced((v) => !v)}
          className="btn-ghost w-full justify-start"
        >
          {showAdvanced ? <ChevronDown className="w-4 h-4" /> : <ChevronRight className="w-4 h-4" />}
          Advanced settings (tagging, duplicates, organization)
        </button>
      </div>

      {showAdvanced && <>
      <Section
        icon={<Shield className="w-5 h-5" />}
        title="Tagging"
        subtitle="How ML results are written to files"
      >
        <Field label="Genre confidence threshold">
          <div className="flex items-center gap-3">
            <input
              type="range"
              min={0}
              max={1}
              step={0.01}
              value={cfg.tagging.genre_confidence_threshold}
              onChange={(e) =>
                updateTagging({ genre_confidence_threshold: Number(e.target.value) })
              }
              className="flex-1 accent-accent"
            />
            <span className="text-sm font-mono w-12 text-right tabular-nums">
              {Math.round(cfg.tagging.genre_confidence_threshold * 100)}%
            </span>
          </div>
          <Hint>Tracks below this ML confidence won't get their genre tag rewritten. Other fields below are independent.</Hint>
        </Field>

        <Field label="Write these fields">
          <div className="grid grid-cols-2 gap-x-4 gap-y-1">
            <Toggle label="Genre" checked={cfg.tagging.write_genre}
              onChange={(v) => updateTagging({ write_genre: v })}
              hint="Subject to the confidence threshold above." />
            <Toggle label="Energy" checked={cfg.tagging.write_energy}
              onChange={(v) => updateTagging({ write_energy: v })} />
            <Toggle label="Mood" checked={cfg.tagging.write_mood}
              onChange={(v) => updateTagging({ write_mood: v })} />
            <Toggle label="Vocal" checked={cfg.tagging.write_vocal}
              onChange={(v) => updateTagging({ write_vocal: v })} />
            <Toggle label="Timeslot" checked={cfg.tagging.write_timeslot}
              onChange={(v) => updateTagging({ write_timeslot: v })} />
            <Toggle label="Direction" checked={cfg.tagging.write_direction}
              onChange={(v) => updateTagging({ write_direction: v })} />
            <Toggle label="BPM" checked={cfg.tagging.write_bpm}
              onChange={(v) => updateTagging({ write_bpm: v })}
              hint="Off by default — Rekordbox is usually more accurate." />
            <Toggle label="Key" checked={cfg.tagging.write_key}
              onChange={(v) => updateTagging({ write_key: v })}
              hint="Off by default — Rekordbox is usually more accurate." />
          </div>
          <Hint>Each field is written independently — genre confidence only gates the genre.</Hint>
        </Field>

        <Field label="Vocal detection sensitivity">
          <div className="flex items-center gap-3">
            <span className="text-[11px] text-white/40 w-20">Instrumental ≤</span>
            <input
              type="range" min={0.3} max={0.95} step={0.01}
              value={cfg.tagging.vocal_instrumental_max}
              onChange={(e) => updateTagging({ vocal_instrumental_max: Number(e.target.value) })}
              className="flex-1 accent-accent"
            />
            <span className="text-sm font-mono w-12 text-right tabular-nums">
              {Math.round(cfg.tagging.vocal_instrumental_max * 100)}%
            </span>
          </div>
          <div className="flex items-center gap-3 mt-1">
            <span className="text-[11px] text-white/40 w-20">Vocal ≥</span>
            <input
              type="range" min={0.5} max={1} step={0.01}
              value={cfg.tagging.vocal_full_min}
              onChange={(e) => updateTagging({ vocal_full_min: Number(e.target.value) })}
              className="flex-1 accent-accent"
            />
            <span className="text-sm font-mono w-12 text-right tabular-nums">
              {Math.round(cfg.tagging.vocal_full_min * 100)}%
            </span>
          </div>
          <Hint>
            Below the first cutoff → Instrumental; above the second → Vocal; between → Light Vocal.
            Raise the first if instrumentals are tagged "Vocal". Re-tag to apply (no re-analysis needed).
          </Hint>
        </Field>

        <Toggle
          label="Preserve Rekordbox cue points & beat grids"
          checked={cfg.tagging.preserve_rekordbox_frames}
          onChange={(v) => updateTagging({ preserve_rekordbox_frames: v })}
          hint="Keeps GEOB and PRIV binary frames intact. Turning this off can destroy performance data."
          danger={!cfg.tagging.preserve_rekordbox_frames}
        />
        <Toggle
          label="Use subgenre as main genre"
          checked={cfg.tagging.write_subgenre_as_main_genre}
          onChange={(v) => updateTagging({ write_subgenre_as_main_genre: v })}
          hint="Rekordbox can only sort by the main genre field, so subgenres get written there."
        />
        <Toggle
          label="Always back up tags before writing"
          checked={cfg.tagging.backup_before_write}
          onChange={(v) => updateTagging({ backup_before_write: v })}
        />

        {/* surface id3_text_encoding so users on legacy
            software (especially Rekordbox 5, which only reads UTF-16 frames)
            can switch off the modern UTF-8 default without editing
            config.toml by hand. */}
        <Field label="ID3 text encoding">
          <select
            className="input"
            value={cfg.tagging.id3_text_encoding}
            onChange={(e) =>
              updateTagging({ id3_text_encoding: Number(e.target.value) })
            }
          >
            <option value={3}>UTF-8 (modern, default)</option>
            <option value={1}>UTF-16 (Rekordbox 5 compatible)</option>
            <option value={0}>ISO-8859-1 (legacy, ASCII only)</option>
          </select>
          <Hint>
            Controls the encoding flag written to ID3v2 text frames. UTF-8 is
            the universal default; pick UTF-16 if your DJ software (e.g.
            Rekordbox 5) can't read non-ASCII characters in genre/title
            tags. ISO-8859-1 is for legacy compatibility only and will
            strip accented characters.
          </Hint>
        </Field>
      </Section>

      <Section
        icon={<Cpu className="w-5 h-5" />}
        title="Duplicate detection"
        subtitle="What to look for, what to do"
      >
        <Toggle
          label="MD5 (catches exact byte-identical copies)"
          checked={cfg.duplicates.use_md5}
          onChange={(v) => updateDuplicates({ use_md5: v })}
        />
        <Toggle
          label="Chromaprint audio fingerprint (catches re-encoded copies)"
          checked={cfg.duplicates.use_chromaprint}
          onChange={(v) => updateDuplicates({ use_chromaprint: v })}
          hint="Requires `fpcalc` on PATH; falls back gracefully if missing."
        />
        <Toggle
          label="Keep distinct versions (Extended / Radio / Remix)"
          checked={cfg.duplicates.keep_distinct_versions}
          onChange={(v) => updateDuplicates({ keep_distinct_versions: v })}
          hint="On (recommended): different versions of a song are never auto-removed — only redundant encodings of the SAME version collapse. Off: dedupe across versions too (keep one file per song)."
        />
        <Toggle
          label="Keep one file per format (FLAC + MP3)"
          checked={cfg.duplicates.keep_all_formats}
          onChange={(v) => updateDuplicates({ keep_all_formats: v })}
          hint="Within a version, keep the best of each format instead of only the single best — e.g. a FLAC master AND an MP3 for a controller that can't read FLAC."
        />
        <Field label="Action">
          <select
            className="input"
            value={cfg.duplicates.action}
            onChange={(e) =>
              updateDuplicates({ action: e.target.value as "report" | "move" | "trash" })
            }
          >
            <option value="report">Report only (safe)</option>
            <option value="move">Move to review folder</option>
            <option value="trash">Send to OS trash</option>
          </select>
        </Field>

        {cfg.duplicates.action === "move" && (
          <Field label="Review folder">
            <div className="flex gap-2">
              <input
                type="text"
                className="input flex-1 font-mono text-xs"
                value={cfg.duplicates.review_folder ?? ""}
                placeholder="(required when action = move)"
                onChange={(e) => updateDuplicates({ review_folder: e.target.value })}
              />
              <button
                className="btn-ghost"
                onClick={() => handlePickDir((p) => updateDuplicates({ review_folder: p }))}
                aria-label="Choose review folder"
                title="Choose review folder"
              >
                <FolderOpen className="w-4 h-4" />
              </button>
            </div>
          </Field>
        )}
      </Section>

      <Section
        icon={<FolderOpen className="w-5 h-5" />}
        title="Organization"
        subtitle="Folder layout rules"
      >
        <Toggle
          label="Use subgenre subfolders"
          checked={cfg.organization.use_subgenres}
          onChange={(v) => updateOrganization({ use_subgenres: v })}
          hint="Off: House/track.mp3. On: House/Deep House/track.mp3."
        />
        <Field label="Min tracks per genre folder">
          <input
            type="number"
            min={1}
            max={500}
            className="input w-24"
            value={cfg.organization.min_genre_size}
            onChange={(e) => {
              // Clamp to [1, 500] on the way into config — OrganizeView clamps
              // the input control, but a value persisted here (or hand-edited
              // in config.json) flows straight into plan_organization, so the
              // store must hold a sane value too. NaN → 1.
              const raw = Number(e.target.value);
              const clamped = Number.isFinite(raw)
                ? Math.min(500, Math.max(1, Math.round(raw)))
                : 1;
              updateOrganization({ min_genre_size: clamped });
            }}
          />
          <Hint>Genres with fewer tracks get bucketed into Other/. (1–500)</Hint>
        </Field>
        <Field label="Target root (override)">
          <div className="flex gap-2">
            <input
              type="text"
              className="input flex-1 font-mono text-xs"
              value={cfg.organization.target_root ?? ""}
              placeholder="(default: same parent as analyzed tracks)"
              onChange={(e) => updateOrganization({ target_root: e.target.value })}
            />
            <button
              className="btn-ghost"
              onClick={() => handlePickDir((p) => updateOrganization({ target_root: p }))}
              aria-label="Choose target root folder"
              title="Choose target root folder"
            >
              <FolderOpen className="w-4 h-4" />
            </button>
          </div>
        </Field>
      </Section>
      </>}

      <div className="mb-6">
        <button
          onClick={() => setShowRestoreConfirm(true)}
          className="btn-ghost text-sm"
        >
          <RotateCcw className="w-4 h-4" />
          Restore all settings to defaults
        </button>
      </div>

      <ConfirmModal
        open={showRestoreConfirm}
        title="Restore all settings?"
        message={
          <>
            <p>
              Every setting on this page will go back to its default value:
              analysis, tagging, duplicates, organization, UI preferences.
            </p>
            <p className="text-white/60 text-xs">
              This doesn't touch your library files or analysis results — only
              configuration.
            </p>
          </>
        }
        confirmLabel="Restore defaults"
        variant="danger"
        onConfirm={handleRestoreAll}
        onCancel={() => setShowRestoreConfirm(false)}
      />

      {profiles.length > 0 && (
        <Section
          icon={<Disc3 className="w-5 h-5" />}
          title="DJ profiles"
          subtitle="one-click presets"
        >
          <p className="text-xs text-white/50">
            Apply a preset tuned for a style of set — adjusts confidence
            thresholds, min genre size, GPU preference, and timeslot BPM bands.
            Overwrites the matching settings; your other tweaks stay.
          </p>
          <div className="grid grid-cols-2 gap-2">
            {profiles.map((p) => {
              const name = String(p.name);
              const busy = profileBusy === name;
              return (
                <button
                  key={name}
                  onClick={() => handleLoadProfile(name)}
                  disabled={profileBusy !== null}
                  className="panel-pad text-left hover:bg-white/[0.04] transition-colors disabled:opacity-50"
                  title={String(p.description ?? "")}
                >
                  <div className="flex items-center gap-2">
                    {busy
                      ? <Loader2 className="w-3.5 h-3.5 animate-spin text-accent" />
                      : <Disc3 className="w-3.5 h-3.5 text-accent" />}
                    <span className="text-sm font-medium text-white">
                      {String(p.label ?? name)}
                    </span>
                  </div>
                  {p.description != null && (
                    <div className="text-[11px] text-white/40 mt-1 line-clamp-2">
                      {String(p.description)}
                    </div>
                  )}
                </button>
              );
            })}
          </div>
        </Section>
      )}

      <Section
        icon={<Wrench className="w-5 h-5" />}
        title="Diagnostics & maintenance"
        subtitle=""
      >
        <div className="flex flex-wrap gap-2">
          <button
            className="btn-ghost"
            onClick={handleCopyDiagnostic}
            disabled={diagBusy !== null}
            title="Copy a full environment report (version, OS, WSL/venv, GPU, log tail) for bug reports"
          >
            {diagBusy === "doctor"
              ? <Loader2 className="w-4 h-4 animate-spin" />
              : <HelpCircle className="w-4 h-4" />}
            Copy diagnostic
          </button>
          <button
            className="btn-ghost"
            onClick={handleVerifyModels}
            disabled={diagBusy !== null}
            title="SHA256-verify every downloaded ML model against the pinned hashes"
          >
            {diagBusy === "verify"
              ? <Loader2 className="w-4 h-4 animate-spin" />
              : <Shield className="w-4 h-4" />}
            Verify model integrity
          </button>
          {preflightResult?.wsl?.usable_distro && (
            <button
              className="btn-ghost"
              onClick={handleUpgradeWsl}
              disabled={diagBusy !== null || active !== null}
              title="Re-install the Vibechek package inside WSL so the analyzer matches this app version (fast — skips apt + essentia)"
            >
              {diagBusy === "upgrade"
                ? <Loader2 className="w-4 h-4 animate-spin" />
                : <Download className="w-4 h-4" />}
              Update WSL install
            </button>
          )}
        </div>
        <Hint>
          Use “Update WSL install” if analyze fails with an “out of date”
          message — it brings the WSL analyzer up to this app’s version without
          a full re-install.
        </Hint>
      </Section>

      <UpdatesSection />

      <Section title="About" subtitle="">
        <div className="text-xs text-white/40 font-mono break-all">
          Sidecar: {sidecarBinary ?? "?"}
        </div>
        <div className="text-xs text-white/40 mt-1">
          Settings are saved to <code className="font-mono">config.toml</code> automatically (debounced 500ms).
        </div>
        <div className="mt-3">
          <button
            className="btn-ghost text-xs"
            onClick={() => setShowLogs(true)}
          >
            <FileText className="w-3.5 h-3.5" />
            View logs
          </button>
        </div>
      </Section>

      <LogsViewer open={showLogs} onClose={() => setShowLogs(false)} />

      <OnnxSetupDialog
        state={onnxSetup}
        onClose={() => setOnnxSetup(null)}
        onCancel={handleCancelOnnxSetup}
        opId={engineSetupOpIdRef.current}
      />

      <GenreSetupDialog
        state={clapSetup}
        title="Set up CLAP genre engine"
        doneMessage="CLAP audio genre engine ready. Re-analyze your library to use it."
        onClose={() => setClapSetup(null)}
        onCancel={handleCancelGenreSetup}
        opId={engineSetupOpIdRef.current}
      />
      <GenreSetupDialog
        state={resolverSetup}
        title="Set up online genre resolver"
        doneMessage="Online genre resolver ready. Re-analyze with online lookup enabled to use it."
        onClose={() => setResolverSetup(null)}
        onCancel={handleCancelGenreSetup}
        opId={engineSetupOpIdRef.current}
      />

      <AnimatePresence>
        {showPreflightDialog && preflightResult && (
          <PreflightDialog
            preflight={preflightResult}
            onRefresh={setPreflightResult}
            onClose={() => setShowPreflightDialog(false)}
            onReady={() => {
              setShowPreflightDialog(false);
              refreshPreflight();
            }}
          />
        )}
      </AnimatePresence>
    </div>
  );
}

/**
 * Software updates: a non-intrusive "Check for updates" affordance backed by
 * tauri-plugin-updater. No-ops cleanly outside the Tauri shell (dev browser /
 * tests) — the button simply reports "up to date".
 */
function UpdatesSection() {
  const { state, supported, checkForUpdate, downloadAndInstall } = useUpdater();
  const busy = state.phase === "checking"
    || state.phase === "downloading"
    || state.phase === "installing";

  const checkLabel = (() => {
    switch (state.phase) {
      case "checking": return "Checking…";
      case "downloading": return "Downloading…";
      case "installing": return "Installing…";
      default: return "Check for updates";
    }
  })();

  return (
    <Section
      icon={<RefreshCw className="w-5 h-5" />}
      title="Software updates"
      subtitle=""
    >
      <div className="flex flex-wrap items-center gap-2">
        <button
          className="btn-ghost"
          onClick={checkForUpdate}
          disabled={busy}
          title="Check GitHub Releases for a newer signed build"
        >
          {busy
            ? <Loader2 className="w-4 h-4 animate-spin" />
            : <RefreshCw className="w-4 h-4" />}
          {checkLabel}
        </button>

        {state.phase === "available" && (
          <button
            className="btn-primary"
            onClick={downloadAndInstall}
            title="Download the update and restart Vibechek"
          >
            <Download className="w-4 h-4" />
            Download &amp; install v{state.version}
          </button>
        )}
      </div>

      {state.phase === "up-to-date" && (
        <div className="text-xs text-white/50 mt-1 flex items-center gap-1">
          <CheckCircle2 className="w-3.5 h-3.5 text-accent-green flex-none" />
          {supported
            ? "You're on the latest version."
            : "Updates apply to the installed desktop app only."}
        </div>
      )}

      {state.phase === "available" && (
        <div className="text-xs text-white/50 mt-2">
          Version <span className="font-mono">{state.version}</span> is available.
          {state.notes && (
            <div className="text-[11px] text-white/40 mt-1 whitespace-pre-wrap line-clamp-4">
              {state.notes}
            </div>
          )}
        </div>
      )}

      {state.phase === "error" && state.error && (
        <div className="text-xs text-accent-yellow/90 mt-1 flex items-start gap-1">
          <AlertTriangle className="w-3.5 h-3.5 flex-none mt-0.5" />
          <span>Update check failed: {state.error}</span>
        </div>
      )}

      <Hint>
        Vibechek checks GitHub Releases for a newer signed build and installs it
        in place. Updates are cryptographically verified before they're applied.
      </Hint>
    </Section>
  );
}

function Section({
  icon,
  title,
  subtitle,
  children,
}: {
  icon?: React.ReactNode;
  title: string;
  subtitle: string;
  children: React.ReactNode;
}) {
  return (
    <div className="mb-8">
      <div className="flex items-baseline gap-3 mb-3">
        {icon && <div className="text-accent">{icon}</div>}
        <h2 className="font-display font-semibold text-lg">{title}</h2>
        {subtitle && <span className="text-xs text-white/40">{subtitle}</span>}
      </div>
      <div className="panel-pad space-y-4">{children}</div>
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <div className="label">{label}</div>
      {children}
    </div>
  );
}

function Hint({ children }: { children: React.ReactNode }) {
  // /50 ≈ 5.2:1 on the dark surface (WCAG AA); the old /40 (~3.9:1) failed
  // for what is genuinely informational text.
  return <div className="text-xs text-white/50 mt-1">{children}</div>;
}

/**
 * Live readiness banner: shows whether analyze can actually run right now.
 * Sits at the top of Settings so the user always knows if something's wrong.
 */
function PreflightSection({
  preflight,
  onRefresh,
  onSetupClick,
}: {
  preflight: PreflightResult | null;
  onRefresh: () => void;
  onSetupClick: () => void;
}) {
  if (!preflight) {
    return (
      <Section icon={<Cpu className="w-5 h-5" />} title="Ready to analyze?" subtitle="checking...">
        <div className="text-sm text-white/40">Loading preflight...</div>
      </Section>
    );
  }

  const ready = preflight.ready;
  const isWindows = preflight.wsl?.is_windows ?? false;
  const wslReady = preflight.wsl?.can_run_vibechek ?? false;
  const usableDistro = preflight.wsl?.usable_distro ?? null;
  const nativeVenv = preflight.native_venv;
  const nativeVenvReady =
    (nativeVenv?.essentia_installed && nativeVenv?.vibechek_installed) ?? false;

  // The Essentia row tells the user where analyze gets its ML engine from.
  // Three possible "ready" sources, three possible "not yet" messages.
  const essentiaRow = (() => {
    if (preflight.essentia.installed) {
      return {
        ok: true,
        label: "Essentia",
        detail: `installed in the sidecar${preflight.essentia.version ? ` (${preflight.essentia.version})` : ""}`,
      };
    }
    if (isWindows && wslReady && usableDistro) {
      return {
        ok: true,
        label: "Essentia",
        detail: `available via WSL (${usableDistro})`,
      };
    }
    if (nativeVenvReady) {
      return {
        ok: true,
        label: "Essentia",
        detail: `available via managed venv${nativeVenv?.essentia_version ? ` (${nativeVenv.essentia_version})` : ""}`,
      };
    }
    if (isWindows) {
      return {
        ok: false,
        label: "Essentia (via WSL)",
        detail: "not installed inside your WSL distro yet — click Set up below",
      };
    }
    return {
      ok: false,
      label: "Essentia",
      detail: "not installed yet — click Set up below",
    };
  })();

  const subtitle = ready
    ? (preflight.analyze_via === "wsl"
        ? "analyze will route through WSL"
        : preflight.analyze_via === "native_venv"
        ? "analyze will route through the managed venv"
        : "all prerequisites satisfied")
    : "see below";

  return (
    <Section
      icon={
        ready ? (
          <CheckCircle2 className="w-5 h-5 text-accent-green" />
        ) : (
          <AlertTriangle className="w-5 h-5 text-accent-yellow" />
        )
      }
      title={ready ? "Ready to analyze" : "Not ready to analyze"}
      subtitle={subtitle}
    >
      <Row ok={essentiaRow.ok} label={essentiaRow.label} detail={essentiaRow.detail} />
      <Row
        ok={preflight.models.missing.length === 0}
        label="ML models"
        detail={
          preflight.models.missing.length === 0
            ? `${preflight.models.found.length} models, ${preflight.models.total_size_mb.toFixed(0)} MB`
            : `${preflight.models.missing.length} of ${preflight.models.found.length + preflight.models.missing.length} missing`
        }
      />

      <div className="flex justify-end gap-2">
        {!ready && (
          <button
            className="btn-primary text-xs"
            onClick={onSetupClick}
            title="Open the setup walkthrough"
          >
            <Wrench className="w-3.5 h-3.5" />
            Set up now
          </button>
        )}
        <button className="btn-ghost text-xs" onClick={onRefresh}>
          Re-check
        </button>
      </div>
    </Section>
  );
}

function Row({ ok, label, detail }: { ok: boolean; label: string; detail: string }) {
  return (
    <div className="flex items-center gap-3">
      {ok ? (
        <CheckCircle2 className="w-4 h-4 text-accent-green flex-none" />
      ) : (
        <AlertTriangle className="w-4 h-4 text-accent-yellow flex-none" />
      )}
      <div className="text-sm text-white">{label}</div>
      <div className={`flex-1 text-xs ${ok ? "text-white/50" : "text-accent-yellow/90"} truncate`}>
        {detail}
      </div>
    </div>
  );
}

/**
 * Read-only summary of detected system resources, shown at the top of Settings
 * so users see what's available before choosing how much to spend.
 *
 * The GPU stat is the *ground truth* — what the actual analyze engine sees,
 * not just what the host has. On Windows with WSL routing, that's TF inside
 * the distro; on Linux/macOS or native, it's local TF / nvidia-smi.
 */
function ResourcesSection({
  sysInfo,
  engineGpu,
  engineProbing,
  analyzeVia,
  preflight,
  onRefreshEngine,
  onRefreshPreflight,
}: {
  sysInfo: SystemResources | null;
  engineGpu: EngineGpuInfo | null;
  engineProbing: boolean;
  analyzeVia: "native" | "native_venv" | "wsl" | null;
  preflight: PreflightResult | null;
  onRefreshEngine: () => void;
  onRefreshPreflight: () => void;
}) {
  if (!sysInfo) {
    return (
      <Section
        icon={<Cpu className="w-5 h-5" />}
        title="System"
        subtitle="Detecting resources..."
      >
        <div className="text-sm text-white/40">Loading...</div>
      </Section>
    );
  }

  const memPct =
    sysInfo.memory_total_mb && sysInfo.memory_available_mb
      ? Math.round(
          ((sysInfo.memory_total_mb - sysInfo.memory_available_mb) /
            sysInfo.memory_total_mb) *
            100,
        )
      : null;

  // The GPU stat: prefer the engine's view (ground truth) when we have it.
  // While probing, show "checking..." instead of the misleading host view.
  const gpuStat = (() => {
    if (engineProbing && !engineGpu) {
      return { value: "checking…", accent: "neutral" as const };
    }
    if (engineGpu?.ok) {
      return engineGpu.gpu_available
        ? { value: "available", accent: "green" as const }
        : { value: "none", accent: "neutral" as const };
    }
    // Fall back to host view if engine probe failed or didn't run.
    return sysInfo.gpu_available
      ? { value: "available (host)", accent: "green" as const }
      : { value: "none", accent: "neutral" as const };
  })();

  return (
    <Section
      icon={<Cpu className="w-5 h-5" />}
      title="System"
      subtitle="What Vibechek can see on this machine"
    >
      <div className="grid grid-cols-3 gap-4">
        <Stat label="CPU cores" value={sysInfo.cpu_count} />
        <Stat
          label="Memory"
          value={
            sysInfo.memory_total_mb
              ? `${(sysInfo.memory_total_mb / 1024).toFixed(1)} GB`
              : "?"
          }
          sub={memPct !== null ? `${memPct}% in use` : undefined}
        />
        <Stat
          label="GPU"
          value={gpuStat.value}
          accent={gpuStat.accent}
        />
      </div>

      {/* Engine-side GPU truth block. Replaces the old host-only block so
          the UI never lies about whether analyze will actually use the GPU. */}
      <EngineGpuBlock
        sysInfo={sysInfo}
        engineGpu={engineGpu}
        engineProbing={engineProbing}
        analyzeVia={analyzeVia}
        preflightDistro={preflight?.wsl?.usable_distro ?? null}
        onRefresh={onRefreshEngine}
      />

      {/* Cross-vendor inventory + honesty callout. Surfaces every GPU the
          host has, not just the NVIDIA one TF can use. Renders nothing
          (and shows nothing in the UI) when no GPUs of any kind exist. */}
      <CrossVendorGpuInventory sysInfo={sysInfo} />

      {/* troubleshooting affordance for the broken
          venv shim case (pre-beta.10 cuda-env.sh patch). Tiny low-visibility
          row — mirrors the one in PreflightDialog so users who keep Settings
          open can repair without re-opening the setup dialog. */}
      {(preflight?.wsl?.distros.length ?? 0) > 0 && (
        <TroubleshootingRow
          distro={
            preflight?.wsl?.usable_distro
            ?? preflight?.wsl?.distros[0]?.name
            ?? null
          }
          onRepaired={onRefreshPreflight}
        />
      )}

      <div className="mt-3 text-[11px] text-white/30 font-mono break-all">
        {sysInfo.platform}
      </div>
    </Section>
  );
}

/**
 * Explicit RPC button for `repair_wsl_shim`.
 *
 * Keeps the user out of the docs/manual-repair path if their analyze starts
 * failing with a SyntaxError caused by the pre-beta.10 cuda-env.sh patch.
 */
function TroubleshootingRow({
  distro,
  onRepaired,
}: {
  distro: string | null;
  onRepaired: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const notify = useNotificationStore((s) => s.notify);

  const handleClick = async () => {
    if (!distro) {
      notify("No WSL distro detected to repair.", { kind: "info" });
      return;
    }
    setBusy(true);
    try {
      const r = await rpc<{
        ok: boolean;
        repaired?: boolean;
        message?: string;
        error?: string;
      }>("repair_wsl_shim", { distro });
      if (r.ok) {
        notify(r.message ?? "Shim repair completed.", { kind: "success" });
        onRepaired();
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
      setBusy(false);
    }
  };

  return (
    <div className="mt-3 text-[11px] text-white/40 flex items-center gap-2">
      <Wrench className="w-3 h-3" />
      <span>Analyze failing with a SyntaxError in WSL?</span>
      <button
        onClick={handleClick}
        disabled={busy}
        className="underline hover:text-white/70 disabled:opacity-50"
      >
        {busy ? "Repairing…" : "Repair WSL shim"}
      </button>
    </div>
  );
}

/**
 * State: GPU hardware visible to TF but TF refuses to register it (missing
 * CUDA libs inside the engine). We tell the user what's missing and offer
 * an "Enable GPU" button that installs the libs.
 *
 * Shows live progress + Cancel while installing.
 * The install timeout is one hour (sidecar.rs MEDIUM tier) but most installs
 * complete in 30-60 sec; we still want a Cancel button so a user on a flaky
 * mirror isn't stuck.
 *
 * Re-derive distro from the latest preflight result
 * instead of the (possibly stale) engineGpu.distro snapshot. After a
 * successful install_essentia_native call on Windows, engineGpu can flip to
 * "native" while preflight.wsl?.usable_distro still names the right target.
 */
function EngineGpuFixableBlock({
  engineGpu,
  preflightDistro,
  onRefresh,
}: {
  engineGpu: EngineGpuInfo;
  preflightDistro: string | null;
  onRefresh: () => void;
}) {
  const [installing, setInstalling] = useState(false);
  const [installError, setInstallError] = useState<string | null>(null);
  const [installResult, setInstallResult] = useState<string | null>(null);
  const [latestProgress, setLatestProgress] = useState<string>("");
  const mountedRef = useRef(true);
  // Correlation id of OUR install op — events stamped with another op's id
  // (stragglers from a previous/parallel op) must not repaint this banner.
  const opIdRef = useRef<string | null>(null);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  useSidecarProgress((evt) => {
    if (!installing) return;
    if (!progressMatches(evt, opIdRef.current)) return;
    setLatestProgress(evt.message || `${evt.current}/${evt.total}`);
  });

  // Re-derive the distro target every render — engineGpu.distro may be stale
  // after a successful native essentia install switched the engine.
  const distro = engineGpu.distro ?? preflightDistro;
  // Cross-vendor guard: the CUDA-libs install path is meaningless for an
  // AMD/Intel/Apple card. The "missing CUDA libs" state only fires when TF
  // saw an NVIDIA card — so only offer the install button when the first
  // visible NVIDIA device is the subject of the message.
  const hasNvidiaDevice = engineGpu.devices.some((d) => d.vendor === "nvidia");
  const canInstall = engineGpu.engine === "wsl" && !!distro && hasNvidiaDevice;

  const handleCancel = async () => {
    try {
      await rpc("cancel_operation");
    } catch {
      /* user-cancel — sidecar handles its own logging */
    }
  };

  const handleInstall = async () => {
    if (!canInstall || !distro) return;
    setInstalling(true);
    setInstallError(null);
    setInstallResult(null);
    setLatestProgress("");
    opIdRef.current = newOpId();
    try {
      const result = await rpc<{
        ok: boolean;
        error?: string;
        packages_installed?: string[];
      }>("install_cuda_libs_in_wsl", {
        distro,
        missing_libs: engineGpu.missing_cuda_libs,
        op_id: opIdRef.current,
      });
      if (!mountedRef.current) return;
      if (result.ok) {
        setInstallResult(
          `Installed ${result.packages_installed?.length ?? 0} package(s). Re-probing…`,
        );
        // Trigger a re-probe — the engine cache was cleared server-side.
        setTimeout(() => {
          if (mountedRef.current) onRefresh();
        }, 500);
      } else {
        setInstallError(result.error ?? "Install failed");
      }
    } catch (e) {
      if (!mountedRef.current) return;
      if (isCancellation(e)) {
        setInstallError("Install cancelled.");
      } else {
        const msg =
          typeof e === "object" && e !== null && "message" in e
            ? String((e as { message: unknown }).message)
            : String(e);
        setInstallError(msg);
      }
    } finally {
      if (mountedRef.current) setInstalling(false);
    }
  };

  return (
    <div className="mt-3 flex items-start gap-2 text-xs">
      <AlertTriangle className="w-4 h-4 flex-none text-accent-yellow mt-0.5" />
      <div className="flex-1">
        <div className="text-accent-yellow/90">
          {engineGpu.devices.length > 0
            ? `${engineGpu.devices.map((g) => g.name).join(", ")} is visible to WSL`
            : "GPU hardware is visible to WSL"}
          , but TensorFlow can&apos;t use it — required CUDA libraries are missing.
        </div>
        {engineGpu.missing_cuda_libs.length > 0 && (
          <div className="text-white/40 mt-1 font-mono">
            Missing: {engineGpu.missing_cuda_libs.join(", ")}
          </div>
        )}
        <div className="text-white/40 mt-1">
          Analysis will fall back to CPU. Click below and Vibechek will install
          NVIDIA&apos;s CUDA runtime wheels from PyPI into the WSL venv
          (~200&nbsp;MB, ~30&nbsp;sec). Works on any Ubuntu, no apt repo
          configuration needed.
        </div>

        {installing && latestProgress && (
          <div className="text-white/60 mt-2 font-mono break-all">
            {latestProgress}
          </div>
        )}
        {installError && (
          <div className="text-accent-red mt-2 font-mono break-all">
            {installError}
          </div>
        )}
        {installResult && (
          <div className="text-accent-green mt-2">{installResult}</div>
        )}

        <div className="flex gap-2 mt-2">
          {canInstall && (
            <button
              className="btn-primary text-xs"
              onClick={handleInstall}
              disabled={installing}
            >
              {installing ? "Installing… (~30 sec)" : "Enable GPU (install CUDA wheels)"}
            </button>
          )}
          {installing && (
            <button
              className="btn-ghost text-xs text-accent-red"
              onClick={handleCancel}
              title="Stop the CUDA install"
            >
              <StopCircle className="w-3.5 h-3.5" />
              Cancel
            </button>
          )}
          <button className="btn-ghost text-xs" onClick={onRefresh} disabled={installing}>
            Re-probe
          </button>
        </div>
      </div>
    </div>
  );
}

/**
 * Shows the *engine-side* GPU truth: what TF actually sees (in WSL or native).
 * Falls back gracefully when the engine probe hasn't run yet or failed.
 */
function EngineGpuBlock({
  sysInfo,
  engineGpu,
  engineProbing,
  analyzeVia,
  preflightDistro,
  onRefresh,
}: {
  sysInfo: SystemResources;
  engineGpu: EngineGpuInfo | null;
  engineProbing: boolean;
  analyzeVia: "native" | "native_venv" | "wsl" | null;
  preflightDistro: string | null;
  onRefresh: () => void;
}) {
  // No engine probe yet — show host view but tell the user the truth.
  if (!engineGpu && !engineProbing) {
    if (sysInfo.gpu_available) {
      return (
        <div className="mt-3 flex items-start gap-2 text-xs">
          <AlertTriangle className="w-4 h-4 flex-none text-accent-yellow mt-0.5" />
          <div>
            <div className="text-white/80">
              {sysInfo.gpu_devices.map((g) => g.name).join(", ")} (host)
            </div>
            <div className="text-white/40">
              Host has a GPU, but whether the analyze engine can use it is not
              yet verified. Click below to check.
            </div>
            <button className="btn-ghost text-xs mt-1" onClick={onRefresh}>
              Check engine GPU
            </button>
          </div>
        </div>
      );
    }
    // Even when no accelerable (NVIDIA) GPU exists, the user may still have
    // an AMD/Intel/Apple card. The CrossVendorGpuInventory below this block
    // surfaces it; here we just say "CPU" without lying about lack of GPUs.
    return (
      <div className="mt-3 text-xs text-white/40">
        No NVIDIA GPU detected — analysis will run on CPU (still fast with
        enough workers).
      </div>
    );
  }

  if (engineProbing) {
    return (
      <div className="mt-3 flex items-start gap-2 text-xs text-white/60">
        <Zap className="w-4 h-4 flex-none mt-0.5 animate-pulse" />
        <div>
          Probing the analyze engine
          {analyzeVia === "wsl" ? " (inside WSL — first probe takes ~10s)" : ""}…
        </div>
      </div>
    );
  }

  if (!engineGpu) return null; // shouldn't happen

  // Probe failed
  if (!engineGpu.ok) {
    return (
      <div className="mt-3 flex items-start gap-2 text-xs text-accent-yellow">
        <AlertTriangle className="w-4 h-4 flex-none mt-0.5" />
        <div>
          <div>Engine GPU probe failed: {engineGpu.error ?? "unknown"}.</div>
          {sysInfo.gpu_available && (
            <div className="text-white/40 mt-0.5">
              Host has {sysInfo.gpu_devices.map((g) => g.name).join(", ")}{" "}
              but the analyze engine couldn&apos;t enumerate it. Analysis will
              fall back to CPU.
            </div>
          )}
          <button className="btn-ghost text-xs mt-1" onClick={onRefresh}>
            Re-probe
          </button>
        </div>
      </div>
    );
  }

  // GPU hardware is visible but TF skipped registering it (missing CUDA libs).
  // This is the most important state to surface: the user has a GPU, the
  // hardware is visible to WSL, but analyze won't actually use it. We offer
  // an Enable GPU button that installs the missing libs.
  //
  // Cross-vendor guard: the missing-CUDA-libs install path only makes sense
  // for an NVIDIA card. If the only visible "GPU" is AMD/Intel/Apple, fall
  // through to the unsupported-vendor branch below instead of showing the
  // install button that can never succeed for them.
  const visibleVendors = new Set(engineGpu.devices.map((d) => d.vendor || "nvidia"));
  const hasNvidiaVisible = visibleVendors.has("nvidia");
  // The "install CUDA libs" fix is the essentia-TENSORFLOW path. For the ONNX
  // engine (engineGpu.runtime set), the GPU build is provisioned by "Set up
  // ONNX engine", so don't show the TF fixer — fall through to the message.
  if (engineGpu.gpu_hardware_visible && !engineGpu.gpu_available && hasNvidiaVisible && !engineGpu.runtime) {
    return (
      <EngineGpuFixableBlock
        engineGpu={engineGpu}
        preflightDistro={preflightDistro}
        onRefresh={onRefresh}
      />
    );
  }

  // Probe succeeded with a GPU
  if (engineGpu.gpu_available) {
    return (
      <div className="mt-3 flex items-start gap-2 text-xs">
        <CheckCircle2 className="w-4 h-4 flex-none text-accent-green mt-0.5" />
        <div>
          <div className="text-white/80">
            {engineGpu.devices.map((g) => g.name).join(", ")}
          </div>
          <div className="text-white/40 font-mono">
            {engineGpu.provider ? (
              // ONNX engine: report the active onnxruntime ExecutionProvider.
              <>
                {engineGpu.engine === "wsl"
                  ? `via ONNX Runtime inside ${engineGpu.distro}`
                  : "via ONNX Runtime"}
                {` · ${engineGpu.provider.replace("ExecutionProvider", "")}`}
                {engineGpu.nvidia_driver ? ` · driver ${engineGpu.nvidia_driver}` : ""}
              </>
            ) : (
              <>
                {engineGpu.engine === "wsl"
                  ? `via TensorFlow inside ${engineGpu.distro}`
                  : "via native TensorFlow"}
                {engineGpu.tf_version ? ` · TF ${engineGpu.tf_version}` : ""}
                {engineGpu.nvidia_driver ? ` · driver ${engineGpu.nvidia_driver}` : ""}
              </>
            )}
          </div>
          <button className="btn-ghost text-xs mt-1" onClick={onRefresh}>
            Re-probe
          </button>
        </div>
      </div>
    );
  }

  // Probe succeeded but no GPU visible to the engine
  return (
    <div className="mt-3 flex items-start gap-2 text-xs">
      {engineGpu.nvidia_smi_available ? (
        <>
          <AlertTriangle className="w-4 h-4 flex-none text-accent-yellow mt-0.5" />
          <div>
            <div className="text-accent-yellow/90">
              NVIDIA driver {engineGpu.nvidia_driver} is present
              {engineGpu.engine === "wsl" ? ` inside ${engineGpu.distro}` : ""},
              but TensorFlow can&apos;t see the GPU.
            </div>
            <div className="text-white/40 mt-0.5">
              Usually means CUDA / cuDNN version mismatch with the bundled TF
              ({engineGpu.tf_version ?? "unknown"}). Analysis will fall back
              to CPU.
            </div>
            <button className="btn-ghost text-xs mt-1" onClick={onRefresh}>
              Re-probe
            </button>
          </div>
        </>
      ) : (
        <div className="text-white/40">
          No NVIDIA GPU visible to the analyze engine
          {engineGpu.engine === "wsl" ? ` (inside ${engineGpu.distro})` : ""}.
          Analysis will run on CPU.
        </div>
      )}
    </div>
  );
}

/**
 * Cross-vendor inventory of every detected GPU/APU, with an honesty callout
 * for non-NVIDIA cards plus an expandable "why isn't my GPU used?" explainer.
 *
 * Sourced from `sysInfo.gpu_devices` (the cross-vendor list populated by
 * `vibechek.gpu_detect`). We deliberately use sysInfo here rather than
 * engineGpu.devices because the host enumeration is fast/synchronous and
 * always reflects every vendor — the engine probe only attempts to surface
 * non-NVIDIA cards from inside WSL via lspci, which often misses them.
 *
 * Vendor icons are intentionally text glyphs (no logo files needed); the
 * intent is to be honest about which vendor a row belongs to, not to brand.
 */
function CrossVendorGpuInventory({ sysInfo }: { sysInfo: SystemResources }) {
  const [showExplainer, setShowExplainer] = useState(false);

  const devices = sysInfo.gpu_devices ?? [];
  if (devices.length === 0) return null;

  const hasUnsupported = (sysInfo.unsupported_gpu_count ?? 0) > 0;

  return (
    <div className="mt-4 border-t border-white/10 pt-3">
      <div className="text-[11px] uppercase tracking-wider text-white/40 mb-2">
        Detected GPUs ({devices.length})
      </div>
      <div className="space-y-1.5">
        {devices.map((g, i) => (
          <GpuInventoryRow key={`${g.vendor}-${g.name}-${i}`} device={g} />
        ))}
      </div>

      {/* Cross-vendor honesty callout. Only shows when there's at least one
          non-NVIDIA card the user might reasonably expect to be used. */}
      {hasUnsupported && (
        <div className="mt-3 flex items-start gap-2 text-xs rounded border border-white/10 bg-white/5 p-2.5">
          <AlertTriangle className="w-4 h-4 flex-none text-accent-yellow mt-0.5" />
          <div className="text-white/70">
            Vibechek&apos;s ML engine (essentia-tensorflow) only supports NVIDIA
            GPUs today. AMD/Intel/Apple GPU acceleration is on the roadmap via
            ONNX Runtime — see{" "}
            <span className="font-mono text-white/50">docs/ONNX_MIGRATION.md</span>.
          </div>
        </div>
      )}

      {/* Expandable explainer — only useful when at least one card is
          flagged unsupported. */}
      {hasUnsupported && (
        <div className="mt-2">
          <button
            type="button"
            onClick={() => setShowExplainer((v) => !v)}
            className="flex items-center gap-1.5 text-[11px] text-white/50 hover:text-white/80"
          >
            {showExplainer
              ? <ChevronDown className="w-3 h-3" />
              : <ChevronRight className="w-3 h-3" />}
            <HelpCircle className="w-3 h-3" />
            Why is my AMD/Intel GPU not used?
          </button>
          {showExplainer && (
            <div className="mt-2 text-[11px] text-white/60 leading-relaxed pl-5 max-w-3xl">
              <p>
                Vibechek runs all music analysis through{" "}
                <span className="font-mono">essentia-tensorflow</span>, which
                vendors TensorFlow 2.5 built only with CUDA support. There is
                no public TF 2.5 build that ships ROCm (AMD), oneDNN-GPU (Intel),
                or Metal (Apple) backends — so even though your card is
                detected, the inference graph has no way to dispatch to it.
              </p>
              <p className="mt-1.5">
                The migration to ONNX Runtime will fix this. ONNX Runtime
                supports DirectML on Windows (any DX12 GPU, including AMD &amp;
                Intel), CoreML on macOS (Apple Silicon &amp; AMD eGPUs), and
                ROCm/OpenVINO on Linux. Tracking issue and progress live in{" "}
                <span className="font-mono">docs/ONNX_MIGRATION.md</span>.
              </p>
              <p className="mt-1.5">
                Until then, analysis falls back to CPU — which is still fast
                on a modern multi-core machine. Bump the worker count in the
                Analysis section above to use all your cores.
              </p>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/**
 * One row in the cross-vendor inventory. Shows the vendor glyph, the marketing
 * name, VRAM (when known), and either an "accelerated" badge or a "CPU-only"
 * tag with the limitation reason.
 */
function GpuInventoryRow({ device }: { device: GpuDevice }) {
  const vendor = device.vendor || "unknown";
  // Vendor glyph: text-only to avoid shipping logos. Color coded so the eye
  // can scan a mixed-vendor list quickly.
  const vendorGlyph = (() => {
    switch (vendor) {
      case "nvidia":
        return <span className="font-mono text-[10px] text-accent-green w-12 inline-block">NVIDIA</span>;
      case "amd":
        return <span className="font-mono text-[10px] text-red-400 w-12 inline-block">AMD</span>;
      case "intel":
        return <span className="font-mono text-[10px] text-blue-400 w-12 inline-block">INTEL</span>;
      case "apple":
        return <span className="font-mono text-[10px] text-white/60 w-12 inline-block">APPLE</span>;
      default:
        return <span className="font-mono text-[10px] text-white/40 w-12 inline-block">GPU</span>;
    }
  })();

  const accelerated = device.accelerated_by_vibechek;
  return (
    <div className="flex items-start gap-2 text-xs" title={device.unsupported_reason ?? ""}>
      {vendorGlyph}
      <div className="flex-1 min-w-0">
        <div className="text-white/80 truncate">{device.name}</div>
        <div className="text-white/40 text-[10px]">
          {device.device_kind}
          {device.memory_mb ? ` · ${(device.memory_mb / 1024).toFixed(1)} GB VRAM` : ""}
        </div>
      </div>
      {accelerated ? (
        <span className="text-[10px] px-1.5 py-0.5 rounded bg-accent-green/20 text-accent-green font-mono uppercase tracking-wider">
          accelerated
        </span>
      ) : (
        <span className="text-[10px] px-1.5 py-0.5 rounded bg-white/10 text-white/50 font-mono uppercase tracking-wider">
          CPU-only (essentia limitation)
        </span>
      )}
    </div>
  );
}

function Stat({
  label,
  value,
  sub,
  accent,
}: {
  label: string;
  value: string | number;
  sub?: string;
  accent?: "green" | "neutral";
}) {
  return (
    <div>
      <div className="label">{label}</div>
      <div
        className={`text-xl font-display font-semibold tabular-nums ${
          accent === "green" ? "text-accent-green" : "text-white"
        }`}
      >
        {value}
      </div>
      {sub && <div className="text-[11px] text-white/40">{sub}</div>}
    </div>
  );
}

function Toggle({
  label,
  checked,
  onChange,
  hint,
  danger,
}: {
  label: string;
  checked: boolean;
  onChange: (v: boolean) => void;
  hint?: string;
  danger?: boolean;
}) {
  return (
    <div>
      <label className="flex items-start gap-3 cursor-pointer">
        <input
          type="checkbox"
          checked={checked}
          onChange={(e) => onChange(e.target.checked)}
          className="mt-0.5 accent-accent"
        />
        <div className="flex-1">
          {/* Plain text — JSX escapes correctly, so labels pass "BPM & key"
              literally. Previously used dangerouslySetInnerHTML to render
              &amp; entities, an XSS footgun if a caller ever passed dynamic
              (track/genre) text. */}
          <div className={danger ? "text-sm text-accent-red" : "text-sm text-white/90"}>
            {label}
          </div>
          {hint && (
            <div className={danger ? "text-xs text-accent-red/70 mt-0.5" : "text-xs text-white/40 mt-0.5"}>
              {hint}
            </div>
          )}
        </div>
      </label>
    </div>
  );
}
