import { useEffect, useRef, useState } from "react";
import { open as openDialog } from "@tauri-apps/plugin-dialog";
import {
  Download, FolderOpen, Settings as SettingsIcon, Shield,
  Zap, AlertTriangle, RotateCcw, ChevronDown, ChevronRight,
  FileText, Wrench, HelpCircle, Disc3, Loader2, Gauge, Copy,
} from "lucide-react";
import { AnimatePresence } from "framer-motion";

import { useConfigStore, useNotificationStore, useOperationStore } from "../stores";
import { isCancellation, rpc, sidecarStatus } from "../hooks/useSidecar";
import { setupOnnxEngine, setupClapEngine, setupGenreResolver } from "../api/rpc";
import {
  listProfiles, loadProfile, doctor as runDoctor, verifyModels,
  upgradeVibechekInWSL, workerBudget as fetchWorkerBudget,
} from "../api/rpc";
import type { ListProfilesResult } from "../api/methods";
import type {
  EngineGpuInfo, PreflightResult, SystemResources, VibechekConfig, WorkerBudget,
} from "../types";
import { ConfirmModal } from "./ConfirmModal";
import { LogsViewer } from "./LogsViewer";
import { PreflightDialog } from "./PreflightDialog";
import { OnnxSetupDialog, type OnnxSetupState } from "./OnnxSetupDialog";
import { GenreSetupDialog, type GenreSetupState } from "./GenreSetupDialog";
import { Field, Hint, Section, Toggle } from "./SettingsPrimitives";
import { PreflightSection, ResourcesSection, UpdatesSection } from "./SettingsSystem";
import { MemoryRefusalActions } from "./MemoryRefusalActions";

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
  // Backend-computed worker plan for the CURRENT engine × genre_classifier ×
  // measured RAM pool. Drives the slider MAX + hint so the slider can never let
  // the user pick more workers than actually fit (the 16→2 CLAP/WSL bug). Re-
  // fetched when engine or genre_classifier changes (see the effect below).
  const [budget, setBudget] = useState<WorkerBudget | null>(null);

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
        // WP-C1: the fix (the "Download models now" button) lives ~800 lines
        // down the same page; point at it AND give a one-click action so the
        // failure isn't a dead end.
        notify(`${bad.length} model file(s) failed verification`, {
          kind: "info",
          detail:
            bad.map((b) => `${b.name}.${b.suffix}: ${b.reason ?? "mismatch"}`).join("\n") +
            "\n\nUse “Download models now” in the Analysis section above to re-fetch them.",
          action: {
            label: "Download models",
            onClick: () => void handleDownloadModels(),
          },
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
      notify("No Linux analysis environment (WSL) is set up.", { kind: "info" });
      return;
    }
    setDiagBusy("upgrade");
    const opId = begin("install-essentia");
    try {
      const engine = useConfigStore.getState().config.analysis.inference_engine;
      const res = await upgradeVibechekInWSL({ distro, inference_engine: engine }, opId);
      finish();
      if (res.ok) {
        notify("Analysis environment updated", {
          kind: "success",
          detail: "The Linux analysis environment now matches the app version.",
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
          note: null,
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

  /**
   * Ask the backend for the worker plan (slider max + per-worker RAM) for the
   * live engine × genre_classifier. Reads both from the store at CALL time (not
   * the captured closure) so the effect that fires on a change sends the NEW
   * values. Passes the usable WSL distro so the VM's RAM is measured, not the
   * host total. Non-fatal — the slider falls back to the static ceiling.
   */
  const refreshWorkerBudget = () => {
    if (!isMounted.current) return;
    const st = useConfigStore.getState().config.analysis;
    fetchWorkerBudget({
      engine: st.inference_engine,
      genre_classifier: st.genre_classifier,
      workers: st.workers,
      distro: preflightResult?.wsl?.usable_distro ?? null,
    })
      .then((b) => {
        if (isMounted.current) setBudget(b);
      })
      .catch(() => {
        /* non-fatal — slider keeps the static WORKERS_MAX ceiling */
      });
  };

  const refreshPreflight = () => {
    // Evaluate readiness for the engine the user is ACTUALLY on. Read it at call
    // time from the store (not the captured `cfg` closure): this fn runs from a
    // mount-time effect whose closure can hold the pre-disk-load default, and
    // the RPC defaults a missing engine to essentia_tf — so without this the
    // banner always judged essentia_tf (green sidecar row) even for a native/
    // onnx user. Mirrors the analyze path, which already sends the engine.
    const engine = useConfigStore.getState().config.analysis.inference_engine;
    // Two-phase load: fast quick=true preflight for instant UI feedback,
    // then upgrade with a full quick=false call so we know the real WSL
    // state and can pick the right engine GPU probe target.
    rpc<PreflightResult>("preflight", { quick: true, engine })
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
        rpc<PreflightResult>("preflight", { quick: false, engine })
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
      // Map the raw OS error to a plain reason; the raw text ([Errno 2]…) is a
      // debugging detail, not a headline the user should read.
      const plain = /errno 2|no such file|cannot find|not exist/i.test(msg)
        ? "it doesn't exist yet"
        : /errno 13|permission|denied|access is denied/i.test(msg)
          ? "you don't have permission to open it"
          : "it couldn't be opened";
      setModelsDirWarning(
        `Couldn't read this folder — ${plain}. Vibechek will try to create it on download.`,
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
    refreshWorkerBudget();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Re-compute the worker budget whenever the engine or the genre classifier
  // changes — CLAP's ~4.5 GB/worker drops the max dramatically vs Discogs, and
  // onnx/native measure a different RAM pool than essentia_tf routes to. The
  // doctrine: "the slider should be based on the model we're using — when CLAP
  // is selected, it should slide to the max workers supported."
  useEffect(() => {
    refreshWorkerBudget();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cfg.analysis.inference_engine, cfg.analysis.genre_classifier]);

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
        // The active engine decides WHICH model set to fetch (onnx/native →
        // the ONNX backbone + heads; essentia_tf → the .pb set). Omitting it
        // downloaded the essentia_tf set regardless, so this button could
        // never remediate a missing-models preflight on the other engines.
        engine: cfgRef.current.analysis.inference_engine || undefined,
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
        budget={budget}
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
        engine={cfg.analysis.inference_engine}
        preflight={preflightResult}
        onRefreshEngine={() => {
          const distro = preflightResult?.wsl?.usable_distro ?? null;
          refreshEngineGpu(distro, true);
        }}
        onRefreshPreflight={refreshPreflight}
        onOpenSetup={() => setShowPreflightDialog(true)}
      />

      <Section
        icon={<Gauge className="w-5 h-5" />}
        title="Analysis"
        subtitle="How much of your machine to use"
      >
        {(() => {
          // Slider MAX is the backend-computed budget (RAM + cores for THIS
          // engine × classifier), falling back to the static ceiling until it
          // loads. When CLAP is selected the max drops to what its ~4.5 GB/
          // worker allows on the measured pool — "the slider slides to the max
          // workers supported" per the doctrine.
          const sliderMax =
            budget && budget.max_workers > 0
              ? budget.max_workers
              : WORKERS_MAX;
          const saved = Math.max(1, cfg.analysis.workers);
          // Clamp the DISPLAY to the budget, but keep the SAVED value untouched —
          // the backend re-clamps every run to the same budget anyway, so we
          // never silently rewrite the user's number here.
          const shown = Math.min(saved, sliderMax);
          const perWorkerGb = budget
            ? (budget.per_worker_mb / 1024).toFixed(1)
            : null;
          const poolGb = budget
            ? (budget.ram_seen_mb / 1024).toFixed(1)
            : null;
          const poolLabel =
            budget?.ram_pool === "wsl_vm"
              ? "the Linux analysis environment"
              : "this computer";
          const classifierLabel =
            cfg.analysis.genre_classifier === "clap" ? "CLAP" : "Discogs";
          const refused = budget != null && budget.max_workers === 0;
          return (
            <Field label={`Worker processes ${sysInfo ? `(of ${sysInfo.cpu_count} cores)` : ""}`}>
              <div className="flex items-center gap-3">
                <input
                  type="range"
                  min={1}
                  // Bound to the live budget max (RAM/core aware), not a static
                  // ceiling — so the slider can't offer more workers than fit.
                  max={sliderMax}
                  step={1}
                  value={shown}
                  onChange={(e) => updateAnalysis({ workers: Number(e.target.value) })}
                  className="flex-1 accent-accent"
                  disabled={refused}
                />
                <span className="text-sm font-mono w-12 text-right tabular-nums">
                  {shown}
                </span>
                {budget && budget.max_workers > 0 && (
                  <button
                    className="btn-ghost text-xs"
                    onClick={() => updateAnalysis({ workers: budget.max_workers })}
                    title={`Max that fits: ${budget.max_workers}`}
                  >
                    max
                  </button>
                )}
              </div>
              <Hint>
                {budget && perWorkerGb && poolGb ? (
                  <>
                    Each {classifierLabel} worker uses ~{perWorkerGb} GB in RAM.
                    Up to <code>{budget.max_workers}</code> fit on {poolLabel}
                    {" "}({poolGb} GB measured).
                  </>
                ) : (
                  <>
                    Each worker holds the model weights in RAM. Best:{" "}
                    <code>cpu_count − 1</code> for a responsive system,{" "}
                    <code>cpu_count</code> for max throughput.
                  </>
                )}
              </Hint>
              {refused && budget?.refusal_reason && (
                <div className="mt-1">
                  <div className="text-xs text-accent-red/90 flex items-start gap-1">
                    <AlertTriangle className="w-3 h-3 flex-none mt-0.5" />
                    <span>{budget.refusal_reason}</span>
                  </div>
                  {/* Same two recovery buttons the ErrorToast shows for this
                      refusal, from the shared component. The flags mirror the
                      backend's memory_refusal_options(): switch is offered when
                      CLAP is selected, more-memory when the WSL VM is the pool. */}
                  <MemoryRefusalActions
                    canSwitchClassifier={cfg.analysis.genre_classifier === "clap"}
                    canIncreaseMemory={budget?.ram_pool === "wsl_vm"}
                  />
                </div>
              )}
              {!refused && saved > sliderMax && (
                <div className="text-xs text-accent-yellow/90 mt-1 flex items-start gap-1">
                  <AlertTriangle className="w-3 h-3 flex-none mt-0.5" />
                  <span>
                    Your saved {saved} workers won&apos;t all fit — the run will
                    use {sliderMax}
                    {budget?.ram_pool === "wsl_vm" ? " (Linux analysis environment memory limit)" : ""}.
                  </span>
                </div>
              )}
            </Field>
          );
        })()}

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
              if (engineProbing && !engineGpu) return "Asking the analysis engine what GPUs it sees…";
              if (engineGpu?.ok) {
                if (engineGpu.gpu_available) {
                  const dev = engineGpu.devices[0]?.name ?? "GPU";
                  const where = engineGpu.engine === "wsl"
                    ? " (visible to the analysis engine)"
                    : "";
                  return `${dev}${where} — auto will use it.`;
                }
                if (engineGpu.nvidia_smi_available) {
                  return `NVIDIA driver ${engineGpu.nvidia_driver ?? "?"} is present, but the analysis engine can't use the GPU — analysis will run on CPU.`;
                }
                return "No GPU visible to the analysis engine. Stays on CPU.";
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
        <Field label="Analysis engine">
          <div className="flex gap-2">
            {([
              { id: "essentia_tf", label: "Essentia · TensorFlow" },
              { id: "onnx", label: "ONNX Runtime" },
              { id: "native", label: "Native · no WSL" },
            ] as const)
              // "Native" is the Windows-installer engine (bundled essentia
              // wheel, in-process). On Linux/macOS the onnx engine IS the
              // in-process path and the backend snaps a saved "native" back
              // to the platform default — offering the button there let users
              // select an engine whose venvs don't exist. UA check answers
              // immediately; the preflight result confirms it once loaded.
              .filter(
                ({ id }) =>
                  id !== "native" ||
                  (preflightResult?.wsl?.is_windows ??
                    navigator.userAgent.includes("Windows")),
              )
              .map(({ id, label }) => (
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
          {cfg.analysis.inference_engine === "native" && (
            <div className="text-xs text-accent-yellow/90 mt-1 flex items-start gap-1">
              <AlertTriangle className="w-3 h-3 flex-none mt-0.5" />
              <span>
                Experimental (Windows). Runs the whole ML pipeline <strong>in-process
                — no WSL</strong> (ONNX inference + a NumPy mel frontend + a native
                Essentia build for decode/BPM/key). Requires the native engine bundled
                with the app; if analyze reports it's not ready, this build doesn't ship
                it yet — use Essentia · TensorFlow or ONNX in the meantime.
              </span>
            </div>
          )}
          <Hint>
            <strong>Essentia · TensorFlow</strong> uses NVIDIA GPUs only (the
            default on macOS/Linux).{" "}
            <strong>ONNX Runtime</strong> runs the same models and drops the
            end-of-life TensorFlow runtime — NVIDIA-accelerated today, with
            cross-vendor GPU (AMD, Intel, and Apple Silicon via DirectML/CoreML)
            planned but not available yet.{" "}
            <strong>Native · no WSL</strong> is the zero-setup Windows default:
            fully in-process, CPU today with GPU support planned. All three are
            validated to match the same reference output.
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
          {/* CLAP setup targets the ONNX/Essentia·TF engine venv; the native
              (in-process, no-WSL) engine has no venv to install it into, and the
              setup RPC guard rejects it with an error that BLAMES native. Gate
              the button on the engine — mirroring how "Set up ONNX engine" only
              shows for onnx — and explain the requirement inline instead. */}
          {cfg.analysis.genre_classifier === "clap" && (
            cfg.analysis.inference_engine === "native" ? (
              <div className="text-xs text-accent-yellow/90 mt-2 flex items-start gap-1">
                <AlertTriangle className="w-3 h-3 flex-none mt-0.5" />
                <span>
                  CLAP requires the ONNX or Essentia·TF engine on Windows. Switch
                  the analysis engine above to set it up.
                </span>
              </div>
            ) : (
              <button
                className="btn-primary mt-2"
                onClick={handleSetupClap}
                disabled={diagBusy !== null || active !== null}
              >
                <Download className="w-4 h-4" />
                {diagBusy === "clap-setup" ? "Setting up CLAP…" : "Set up CLAP genre engine"}
              </button>
            )
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
            label="Resolve genre online (small AI model + web)"
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
            Looks up each track's genre online (a small AI model reads the web
            results for the artist + title), then layers it into reconciliation
            (tag › web › audio) — the most accurate option (~60%) on tagged
            libraries. Needs network + a small AI model (one-time ~4.7 GB setup,
            fully private). Adds time per track; off by default.
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
        icon={<Copy className="w-5 h-5" />}
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
              title="Re-installs the Vibechek package in the Linux analysis environment so it matches this app version (fast — skips apt + essentia)"
            >
              {diagBusy === "upgrade"
                ? <Loader2 className="w-4 h-4 animate-spin" />
                : <Download className="w-4 h-4" />}
              Update Linux analysis environment
            </button>
          )}
        </div>
        <Hint>
          Use “Update Linux analysis environment” if analyze fails with an “out
          of date” message — it brings the Linux analysis environment up to this
          app’s version without a full re-install.
        </Hint>
      </Section>

      <UpdatesSection />

      <Section title="About" subtitle="">
        <div className="text-xs text-white/40 font-mono break-all">
          Analysis service: {sidecarBinary ?? "?"}
        </div>
        <div className="text-xs text-white/40 mt-1">
          Settings are saved automatically.
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

