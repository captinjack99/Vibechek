import { useEffect, useRef, useState } from "react";
import { open as openDialog } from "@tauri-apps/plugin-dialog";
import {
  Download, Cpu, FolderOpen, Settings as SettingsIcon, Shield,
  Zap, AlertTriangle, CheckCircle2, RotateCcw, ChevronDown, ChevronRight,
  FileText, Wrench, StopCircle, HelpCircle, Disc3, Loader2,
} from "lucide-react";
import { AnimatePresence } from "framer-motion";

import { useConfigStore, useNotificationStore, useOperationStore } from "../stores";
import { isCancellation, rpc, sidecarStatus, useSidecarProgress } from "../hooks/useSidecar";
import {
  listProfiles, loadProfile, doctor as runDoctor, verifyModels,
  upgradeVibechekInWSL,
} from "../api/rpc";
import type { ListProfilesResult } from "../api/methods";
import type { EngineGpuInfo, GpuDevice, PreflightResult, SystemResources, VibechekConfig } from "../types";
import { ConfirmModal } from "./ConfirmModal";
import { LogsViewer } from "./LogsViewer";
import { PreflightDialog } from "./PreflightDialog";

// AUDIT_SETTINGS_TAB #11: the workers slider used to cap at sysInfo.cpu_count,
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
  // AUDIT_SETTINGS_TAB #12: restoring all defaults is destructive — gate it
  // behind a confirm modal so a stray click can't wipe a tweaked setup.
  const [showRestoreConfirm, setShowRestoreConfirm] = useState(false);

  // AUDIT_SETTINGS_TAB #7: track mount so async preflight / sysInfo callbacks
  // don't call setState after the component unmounts (the user switched
  // tabs while the slow WSL probe was in flight).
  const isMounted = useRef(true);
  useEffect(() => {
    isMounted.current = true;
    return () => {
      isMounted.current = false;
    };
  }, []);

  // AUDIT_SETTINGS_TAB #15: handleDownloadModels used to close over the
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
  // AUDIT_SETTINGS_TAB #9: models_dir is a free-text input. Validate on blur
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
  const [diagBusy, setDiagBusy] = useState<null | "doctor" | "verify" | "upgrade">(null);

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
    begin("install-essentia");
    try {
      const res = await upgradeVibechekInWSL({ distro });
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

  /**
   * Ask the actual analyze engine what GPUs it sees.
   * - Native essentia: ~instant.
   * - WSL: ~10s (boots TF inside the distro). We show a spinner.
   * Cached server-side for 5 min — `force=true` bypasses.
   */
  const refreshEngineGpu = (distro: string | null, force = false) => {
    if (!isMounted.current) return;
    setEngineProbing(true);
    rpc<EngineGpuInfo>("engine_gpu_status", { distro, force })
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
        // AUDIT_SETTINGS_TAB #8: the full preflight can take up to ~10s
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

  const handleDownloadModels = async () => {
    begin("download-models");
    try {
      // AUDIT_SETTINGS_TAB #15: read the latest models_dir from the store
      // via cfgRef so we don't ship a stale value from the original render.
      await rpc("download_models", {
        models_dir: cfgRef.current.analysis.models_dir || undefined,
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
          // AUDIT_SETTINGS_TAB #20: `native_venv` is a real value but the
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
              // AUDIT_SETTINGS_TAB #11: static high ceiling instead of
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
              // AUDIT_SETTINGS_TAB #10: when no GPU is available "on" silently
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
          <Hint>Tracks below this ML confidence won't get their genre tag rewritten.</Hint>
        </Field>

        <Toggle
          label="Skip BPM & key"
          checked={cfg.tagging.skip_bpm_and_key}
          onChange={(v) => updateTagging({ skip_bpm_and_key: v })}
          hint="Rekordbox's BPM/key detection is more reliable than the ML's."
        />
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

        {/* AUDIT_TAGS_TAB #4: surface id3_text_encoding so users on legacy
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
  return <div className="text-xs text-white/40 mt-1">{children}</div>;
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

      {/* AUDIT_SETTINGS_TAB #4: troubleshooting affordance for the broken
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
 * AUDIT_SETTINGS_TAB #4: explicit RPC button for `repair_wsl_shim`.
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
 * AUDIT_SETTINGS_TAB #5 + #16: shows live progress + Cancel while installing.
 * The install timeout is one hour (sidecar.rs MEDIUM tier) but most installs
 * complete in 30-60 sec; we still want a Cancel button so a user on a flaky
 * mirror isn't stuck.
 *
 * AUDIT_SETTINGS_TAB #22: re-derive distro from the latest preflight result
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

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  useSidecarProgress((evt) => {
    if (!installing) return;
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
    try {
      const result = await rpc<{
        ok: boolean;
        error?: string;
        packages_installed?: string[];
      }>("install_cuda_libs_in_wsl", {
        distro,
        missing_libs: engineGpu.missing_cuda_libs,
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
  if (engineGpu.gpu_hardware_visible && !engineGpu.gpu_available && hasNvidiaVisible) {
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
            {engineGpu.engine === "wsl"
              ? `via TensorFlow inside ${engineGpu.distro}`
              : "via native TensorFlow"}
            {engineGpu.tf_version ? ` · TF ${engineGpu.tf_version}` : ""}
            {engineGpu.nvidia_driver ? ` · driver ${engineGpu.nvidia_driver}` : ""}
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
