import { useEffect, useState } from "react";
import { open as openDialog } from "@tauri-apps/plugin-dialog";
import {
  Download, Cpu, FolderOpen, Settings as SettingsIcon, Shield,
  Zap, AlertTriangle, CheckCircle2, RotateCcw, ChevronDown, ChevronRight,
  FileText, Wrench,
} from "lucide-react";
import { AnimatePresence } from "framer-motion";

import { useConfigStore, useOperationStore } from "../stores";
import { rpc, sidecarStatus } from "../hooks/useSidecar";
import type { EngineGpuInfo, PreflightResult, SystemResources, VibechekConfig } from "../types";
import { LogsViewer } from "./LogsViewer";
import { PreflightDialog } from "./PreflightDialog";

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

  const handleRestoreAll = async () => {
    try {
      const result = await rpc<{ config: VibechekConfig }>("restore_default_config");
      setConfig(result.config, true);
    } catch (e) {
      fail(String(e));
    }
  };

  const [sidecarBinary, setSidecarBinary] = useState<string | null>(null);
  const [sysInfo, setSysInfo] = useState<SystemResources | null>(null);
  const [preflightResult, setPreflightResult] = useState<PreflightResult | null>(null);
  // Engine-side GPU truth — what TF inside WSL (or native) actually sees.
  // Probed lazily after preflight resolves; null means "haven't asked yet",
  // engineProbing=true means "asking, ~10s wait".
  const [engineGpu, setEngineGpu] = useState<EngineGpuInfo | null>(null);
  const [engineProbing, setEngineProbing] = useState(false);

  /**
   * Ask the actual analyze engine what GPUs it sees.
   * - Native essentia: ~instant.
   * - WSL: ~10s (boots TF inside the distro). We show a spinner.
   * Cached server-side for 5 min — `force=true` bypasses.
   */
  const refreshEngineGpu = (distro: string | null, force = false) => {
    setEngineProbing(true);
    rpc<EngineGpuInfo>("engine_gpu_status", { distro, force })
      .then((info) => setEngineGpu(info))
      .catch((e) => setEngineGpu({
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
        error: String(e),
        probed_at: Date.now() / 1000,
      }))
      .finally(() => setEngineProbing(false));
  };

  const refreshPreflight = () => {
    // Two-phase load: fast preflight first (returns in <1s), then trigger the
    // slower per-distro WSL probe in the background and merge results.
    rpc<PreflightResult>("preflight", {})
      .then((quick) => {
        setPreflightResult(quick);
        // Kick off the slow probe only on Windows where it matters
        if (quick.wsl?.is_windows) {
          rpc<PreflightResult["wsl"]>("wsl_status", { quick: false })
            .then((wsl) => {
              setPreflightResult((prev) => {
                if (!prev) return prev;
                // Recompute the readiness fields now that we know the real
                // WSL state (quick mode couldn't tell us if essentia is in
                // a distro). Mirrors the logic in vibechek/preflight.py.
                const wslReady = wsl?.can_run_vibechek ?? false;
                const ready =
                  (prev.essentia.installed || wslReady) &&
                  prev.models.missing.length === 0;
                const analyze_via: "native" | "wsl" | null = prev.essentia.installed
                  ? "native"
                  : wslReady
                  ? "wsl"
                  : null;
                // Now that we know where analyze will run, ask the actual
                // engine what GPUs it sees. The cached probe is cheap;
                // first call inside WSL takes ~10s while TF loads.
                if (analyze_via === "wsl" && wsl?.usable_distro) {
                  refreshEngineGpu(wsl.usable_distro);
                } else if (analyze_via === "native") {
                  refreshEngineGpu(null);
                }
                return { ...prev, wsl, ready, analyze_via };
              });
            })
            .catch(() => {});
        } else if (quick.essentia.installed) {
          // Non-Windows: probe the native engine directly.
          refreshEngineGpu(null);
        }
      })
      .catch(() => {});
  };

  useEffect(() => {
    sidecarStatus().then((s) => setSidecarBinary(s.binary)).catch(() => {});
    rpc<SystemResources>("system_info")
      .then((info) => {
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
      await rpc("download_models", {
        models_dir: cfg.analysis.models_dir || undefined,
      });
      finish();
      refreshPreflight();
    } catch (e) {
      fail(String(e));
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
          preflightResult?.analyze_via === "wsl" ||
          preflightResult?.analyze_via === "native"
            ? preflightResult.analyze_via
            : null
        }
        onRefreshEngine={() => {
          const distro = preflightResult?.wsl?.usable_distro ?? null;
          refreshEngineGpu(distro, true);
        }}
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
              max={sysInfo?.cpu_count ?? 32}
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
        </Field>

        <Field label="GPU acceleration">
          <div className="flex gap-2">
            {(["auto", "on", "off"] as const).map((mode) => (
              <button
                key={mode}
                onClick={() => updateAnalysis({ use_gpu: mode })}
                className={`btn ${
                  cfg.analysis.use_gpu === mode
                    ? "bg-accent text-white"
                    : "bg-white/5 text-white/70 hover:bg-white/10"
                }`}
              >
                {mode === "auto" && <Zap className="w-3.5 h-3.5" />}
                <span className="capitalize">{mode}</span>
              </button>
            ))}
          </div>
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
            />
            <button
              className="btn-ghost"
              onClick={() => handlePickDir((p) => updateAnalysis({ models_dir: p }))}
            >
              <FolderOpen className="w-4 h-4" />
            </button>
          </div>
          <Hint>~800 MB total when downloaded.</Hint>
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
          label="Skip BPM &amp; key"
          checked={cfg.tagging.skip_bpm_and_key}
          onChange={(v) => updateTagging({ skip_bpm_and_key: v })}
          hint="Rekordbox's BPM/key detection is more reliable than the ML's."
        />
        <Toggle
          label="Preserve Rekordbox cue points &amp; beat grids"
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
            className="input w-24"
            value={cfg.organization.min_genre_size}
            onChange={(e) =>
              updateOrganization({ min_genre_size: Number(e.target.value) || 1 })
            }
          />
          <Hint>Genres with fewer tracks get bucketed into Other/.</Hint>
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
          onClick={handleRestoreAll}
          className="btn-ghost text-sm"
        >
          <RotateCcw className="w-4 h-4" />
          Restore all settings to defaults
        </button>
      </div>

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

  // On Windows, "Essentia missing natively" is the expected state. What
  // matters is whether it's available via WSL.
  const essentiaRow = (() => {
    if (preflight.essentia.installed) {
      return {
        ok: true,
        label: "Essentia",
        detail: `installed${preflight.essentia.version ? ` (${preflight.essentia.version})` : ""}`,
      };
    }
    if (isWindows && wslReady && usableDistro) {
      return {
        ok: true,
        label: "Essentia",
        detail: `available via WSL (${usableDistro})`,
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
      detail: preflight.essentia.error ?? "not installed (pip install essentia-tensorflow)",
    };
  })();

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
      subtitle={ready
        ? (preflight.analyze_via === "wsl" ? "analyze will route through WSL" : "all prerequisites satisfied")
        : "see below"}
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
  onRefreshEngine,
}: {
  sysInfo: SystemResources | null;
  engineGpu: EngineGpuInfo | null;
  engineProbing: boolean;
  analyzeVia: "native" | "wsl" | null;
  onRefreshEngine: () => void;
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
        onRefresh={onRefreshEngine}
      />

      <div className="mt-3 text-[11px] text-white/30 font-mono break-all">
        {sysInfo.platform}
      </div>
    </Section>
  );
}

/**
 * State: GPU hardware visible to TF but TF refuses to register it (missing
 * CUDA libs inside the engine). We tell the user what's missing and offer
 * an "Enable GPU" button that installs the libs.
 */
function EngineGpuFixableBlock({
  engineGpu,
  onRefresh,
}: {
  engineGpu: EngineGpuInfo;
  onRefresh: () => void;
}) {
  const [installing, setInstalling] = useState(false);
  const [installError, setInstallError] = useState<string | null>(null);
  const [installResult, setInstallResult] = useState<string | null>(null);

  const handleInstall = async () => {
    if (engineGpu.engine !== "wsl" || !engineGpu.distro) return;
    setInstalling(true);
    setInstallError(null);
    setInstallResult(null);
    try {
      const result = await rpc<{
        ok: boolean;
        error?: string;
        packages_installed?: string[];
      }>("install_cuda_libs_in_wsl", {
        distro: engineGpu.distro,
        missing_libs: engineGpu.missing_cuda_libs,
      });
      if (result.ok) {
        setInstallResult(
          `Installed ${result.packages_installed?.length ?? 0} package(s). Re-probing…`,
        );
        // Trigger a re-probe — the engine cache was cleared server-side.
        setTimeout(onRefresh, 500);
      } else {
        setInstallError(result.error ?? "Install failed");
      }
    } catch (e) {
      setInstallError(String(e));
    } finally {
      setInstalling(false);
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
          Analysis will fall back to CPU. Install the missing CUDA libraries
          to enable GPU acceleration.
        </div>

        {installError && (
          <div className="text-accent-red mt-2 font-mono break-all">
            {installError}
          </div>
        )}
        {installResult && (
          <div className="text-accent-green mt-2">{installResult}</div>
        )}

        <div className="flex gap-2 mt-2">
          {engineGpu.engine === "wsl" && engineGpu.distro && (
            <button
              className="btn-primary text-xs"
              onClick={handleInstall}
              disabled={installing}
            >
              {installing ? "Installing… (~5 min)" : "Enable GPU (install CUDA libs)"}
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
  onRefresh,
}: {
  sysInfo: SystemResources;
  engineGpu: EngineGpuInfo | null;
  engineProbing: boolean;
  analyzeVia: "native" | "wsl" | null;
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
    return (
      <div className="mt-3 text-xs text-white/40">
        No NVIDIA GPU detected on the host. Analysis will run on CPU — still
        fast with enough workers.
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
  if (engineGpu.gpu_hardware_visible && !engineGpu.gpu_available) {
    return (
      <EngineGpuFixableBlock
        engineGpu={engineGpu}
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
          <div
            className={danger ? "text-sm text-accent-red" : "text-sm text-white/90"}
            dangerouslySetInnerHTML={{ __html: label }}
          />
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
