/**
 * Settings system / diagnostics sections — the read-only status, engine-GPU,
 * and updates blocks, extracted verbatim from Settings.tsx (which renders
 * UpdatesSection / PreflightSection / ResourcesSection). Pure relocation, no
 * behavior change.
 */
import { useEffect, useRef, useState } from "react";
import {
  AlertTriangle,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  Cpu,
  Download,
  HelpCircle,
  Loader2,
  RefreshCw,
  StopCircle,
  Wrench,
  Zap,
} from "lucide-react";
import { open as openUrl } from "@tauri-apps/plugin-shell";
import { useNotificationStore } from "../stores";
import { newOpId, progressMatches } from "../stores/operation";
import { isCancellation, rpc, useSidecarProgress } from "../hooks/useSidecar";
import { useUpdater } from "../hooks/useUpdater";
import type { EngineGpuInfo, GpuDevice, PreflightResult, SystemResources, WorkerBudget } from "../types";
import { Hint, Row, Section, Stat } from "./SettingsPrimitives";

/** Subtle animated placeholder shown while a section's data is still loading. */
function Skeleton({ rows = 2 }: { rows?: number }) {
  return (
    <div className="space-y-2" aria-hidden="true">
      {Array.from({ length: rows }, (_, i) => (
        <div
          key={i}
          className="h-3 rounded-sm bg-white/10 animate-pulse"
          style={{ width: `${72 - i * 16}%` }}
        />
      ))}
    </div>
  );
}

/**
 * In-app auto-update is DELIBERATELY OFF until release signing is funded:
 * tauri.conf.json ships `createUpdaterArtifacts: false` and a placeholder
 * updater pubkey, and no latest.json is published — so tauri-plugin-updater's
 * check() 404s on EVERY click, including when a newer release genuinely
 * exists. While that's the case, the live check UI is a lie; render an honest
 * "download from GitHub Releases" affordance instead. Flip this flag together
 * with the three enablement steps in docs/RELEASING.md ("Auto-update +
 * signing setup") — all of them are required for the live section to work.
 */
const UPDATER_ENABLED = false;

const RELEASES_URL = "https://github.com/captinjack99/Vibechek/releases";

/**
 * Software updates: a non-intrusive "Check for updates" affordance backed by
 * tauri-plugin-updater. No-ops cleanly outside the Tauri shell (dev browser /
 * tests) — the button simply reports "up to date".
 */
export function UpdatesSection() {
  if (!UPDATER_ENABLED) return <UpdatesSectionGated />;
  return <UpdatesSectionLive />;
}

/** The honest OFF state: no always-failing button, just where updates live. */
function UpdatesSectionGated() {
  return (
    <Section
      icon={<RefreshCw className="w-5 h-5" />}
      title="Software updates"
      subtitle=""
    >
      <div className="text-sm text-white/60">
        Automatic in-app updates aren't enabled for beta builds yet — new
        versions are published on GitHub Releases.
      </div>
      <div className="mt-2">
        <button
          className="btn-ghost"
          onClick={() => void openUrl(RELEASES_URL)}
          title={RELEASES_URL}
        >
          <Download className="w-4 h-4" />
          Open GitHub Releases
        </button>
      </div>
      <Hint>
        Auto-update ships once release signing is set up — the updater refuses
        anything it can't cryptographically verify, and beta builds aren't
        signed yet. Until then: download the new installer and run it over the
        old one. Your settings, saved analyses, and backups are kept.
      </Hint>
    </Section>
  );
}

function UpdatesSectionLive() {
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

/**
 * Live readiness banner: shows whether analyze can actually run right now.
 * Sits at the top of Settings so the user always knows if something's wrong.
 */
export function PreflightSection({
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
        <Skeleton rows={2} />
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
  // Gate the green "installed in the sidecar" row on `essentia_usable`, NOT the
  // raw `installed` flag: the bundled DSP-only Windows wheel imports fine
  // (installed=true) but can't serve essentia_tf/onnx in-process (needs a
  // TF-capable build), so essentia_usable is the honest "can actually run THIS
  // engine here" signal the backend already computes. Branching on `installed`
  // painted a green check for the very component blocking analyze, directly
  // under the section's own red "Not ready" title.
  const essentiaRow = (() => {
    if (preflight.essentia_usable) {
      return {
        ok: true,
        label: "Analysis engine",
        detail: `installed${preflight.essentia.version ? ` (${preflight.essentia.version})` : ""}`,
      };
    }
    if (isWindows && wslReady && usableDistro) {
      return {
        ok: true,
        label: "Analysis engine",
        detail: "available via the Linux analysis environment",
      };
    }
    if (nativeVenvReady) {
      return {
        ok: true,
        label: "Analysis engine",
        detail: `available via the managed analysis environment${nativeVenv?.essentia_version ? ` (${nativeVenv.essentia_version})` : ""}`,
      };
    }
    // Installed in the sidecar but that build can't serve the engine the banner
    // is evaluating (the DSP-only wheel serves only the native engine). Say so
    // explicitly instead of a green check — and point at the real fix.
    if (preflight.essentia.installed) {
      return {
        ok: false,
        label: "Analysis engine",
        detail: isWindows
          ? "installed, but this build can't run the analysis engine here — set up the Linux analysis environment below"
          : "installed, but this build can't run the analysis engine here — set up the managed analysis environment below",
      };
    }
    if (isWindows) {
      return {
        ok: false,
        label: "Analysis engine",
        detail: "not installed in the Linux analysis environment yet — click Set up below",
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
        ? "ready — using the Linux analysis environment"
        : preflight.analyze_via === "native_venv"
        ? "ready — using the managed analysis environment"
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

/**
 * Read-only summary of detected system resources, shown at the top of Settings
 * so users see what's available before choosing how much to spend.
 *
 * The GPU stat is the *ground truth* — what the actual analyze engine sees,
 * not just what the host has. On Windows with WSL routing, that's TF inside
 * the distro; on Linux/macOS or native, it's local TF / nvidia-smi.
 */
export function ResourcesSection({
  sysInfo,
  engineGpu,
  engineProbing,
  analyzeVia,
  engine,
  preflight,
  budget,
  onRefreshEngine,
  onRefreshPreflight,
  onOpenSetup,
}: {
  sysInfo: SystemResources | null;
  engineGpu: EngineGpuInfo | null;
  engineProbing: boolean;
  analyzeVia: "native" | "native_venv" | "wsl" | null;
  /** Selected inference engine ("essentia_tf" | "onnx" | "native") — drives the
   *  engine-aware GPU story in the cross-vendor inventory below. */
  engine?: string;
  preflight: PreflightResult | null;
  /** Backend worker budget — carries which RAM pool was measured so the Memory
   *  stat can show the WSL VM's RAM (the real ceiling), not just the host. */
  budget?: WorkerBudget | null;
  onRefreshEngine: () => void;
  onRefreshPreflight: () => void;
  /** Opens the setup walkthrough — passed to the engine-GPU probe-failed block
   *  so it can offer a real jump-link even when the top banner is green. */
  onOpenSetup?: () => void;
}) {
  if (!sysInfo) {
    return (
      <Section
        icon={<Cpu className="w-5 h-5" />}
        title="System"
        subtitle="Detecting resources..."
      >
        <div className="grid grid-cols-3 gap-4" aria-hidden="true">
          {Array.from({ length: 3 }, (_, i) => (
            <div key={i} className="h-14 rounded-lg bg-white/5 animate-pulse" />
          ))}
        </div>
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
      {(() => {
        // For WSL-routed engines the analyze workers draw from the VM's RAM
        // slice, not the host total — so show the VM figure the budget measured
        // ("WSL VM: 15.8 GB · of 31.7 GB host"), the pool the worker cap actually
        // uses. Falls back to the plain host total when native / not measured.
        const vmMeasured =
          budget?.ram_pool === "wsl_vm" && budget.ram_seen_mb > 0;
        const hostGb = sysInfo.memory_total_mb
          ? `${(sysInfo.memory_total_mb / 1024).toFixed(1)} GB`
          : "?";
        const memValue = vmMeasured
          ? `${(budget!.ram_seen_mb / 1024).toFixed(1)} GB`
          : hostGb;
        const memSub = vmMeasured
          ? `of ${hostGb} total on this PC`
          : memPct !== null
            ? `${memPct}% in use`
            : undefined;
        return (
          <div className="grid grid-cols-3 gap-4">
            <Stat label="CPU cores" value={sysInfo.cpu_count} />
            <Stat
              label={vmMeasured ? "Memory (Linux analysis environment)" : "Memory"}
              value={memValue}
              sub={memSub}
              subTitle={vmMeasured ? "Measured inside the Linux analysis environment (WSL), not the full host total" : undefined}
            />
            <Stat
              label="GPU"
              value={gpuStat.value}
              accent={gpuStat.accent}
            />
          </div>
        );
      })()}

      {/* Engine-side GPU truth block. Replaces the old host-only block so
          the UI never lies about whether analyze will actually use the GPU. */}
      <EngineGpuBlock
        sysInfo={sysInfo}
        engineGpu={engineGpu}
        engineProbing={engineProbing}
        analyzeVia={analyzeVia}
        preflightDistro={preflight?.wsl?.usable_distro ?? null}
        onRefresh={onRefreshEngine}
        onOpenSetup={onOpenSetup}
      />

      {/* Cross-vendor inventory + honesty callout. Surfaces every GPU the
          host has, not just the one the selected engine can use. Renders nothing
          (and shows nothing in the UI) when no GPUs of any kind exist. */}
      <CrossVendorGpuInventory sysInfo={sysInfo} engine={engine} />

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
      <span>Analysis crashing right when it starts?</span>
      <button
        onClick={handleClick}
        disabled={busy}
        title="Repairs a corrupted launch file (SyntaxError) in the Linux analysis environment"
        className="underline hover:text-white/70 disabled:opacity-50"
      >
        {busy ? "Repairing…" : "Repair the analysis environment"}
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
            ? `Your ${engineGpu.devices.map((g) => g.name).join(", ")} is visible, but the analysis engine can't use it yet`
            : "Your GPU is visible, but the analysis engine can't use it yet"}
          {" "}— some accelerator files are missing.
        </div>
        {engineGpu.missing_cuda_libs.length > 0 && (
          <div className="text-white/40 mt-1 font-mono">
            Missing: {engineGpu.missing_cuda_libs.join(", ")}
          </div>
        )}
        <div className="text-white/40 mt-1">
          Analysis will run on CPU until then. Click below and Vibechek installs
          NVIDIA&apos;s CUDA runtime wheels from PyPI into the analysis
          environment (~200&nbsp;MB, ~30&nbsp;sec). Works on any Ubuntu, no apt
          repo configuration needed.
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
              {installing ? "Installing… (~30 sec)" : "Enable GPU acceleration"}
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
  onOpenSetup,
}: {
  sysInfo: SystemResources;
  engineGpu: EngineGpuInfo | null;
  engineProbing: boolean;
  analyzeVia: "native" | "native_venv" | "wsl" | null;
  preflightDistro: string | null;
  onRefresh: () => void;
  /** Opens the setup walkthrough. A probe can fail while preflight is green
   *  (so the top banner shows no "Set up now"), so this block offers its own
   *  explicit jump-link to it (WP-B1). */
  onOpenSetup?: () => void;
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

  // Probe failed — plain headline; the raw probe string (exit code, stderr,
  // "TF probe JSON" parse error) is DEMOTED to a details toggle (WP-B1).
  if (!engineGpu.ok) {
    return (
      <div className="mt-3 flex items-start gap-2 text-xs text-accent-yellow">
        <AlertTriangle className="w-4 h-4 flex-none mt-0.5" />
        <div className="min-w-0">
          <div className="text-accent-yellow/90">
            Couldn&apos;t check whether your GPU is usable.
          </div>
          <div className="text-white/40 mt-0.5">
            {sysInfo.gpu_available
              ? `Your ${sysInfo.gpu_devices.map((g) => g.name).join(", ")} is here, but the analysis engine couldn't confirm it can use it — analysis will run on CPU until it can.`
              : "The analysis engine couldn't confirm GPU support — analysis will run on CPU."}
          </div>
          {engineGpu.error && (
            <details className="mt-1 text-[11px] text-white/40">
              <summary className="cursor-pointer hover:text-white/60">
                Technical details
              </summary>
              <pre className="mt-1 font-mono whitespace-pre-wrap break-all max-h-32 overflow-auto">
                {engineGpu.error}
              </pre>
            </details>
          )}
          <div className="flex gap-2 mt-1">
            {onOpenSetup && (
              <button className="btn-ghost text-xs" onClick={onOpenSetup}>
                Set up the analysis environment
              </button>
            )}
            <button className="btn-ghost text-xs" onClick={onRefresh}>
              Re-probe
            </button>
          </div>
        </div>
      </div>
    );
  }

  // Native (bundled ONNX Runtime) engine: the honest CPU-only story. That wheel
  // ships no GPU execution providers (roadmap), so it runs on CPU regardless of
  // host hardware. `note` is set ONLY by the native-bundled probe — the old code
  // fell through to a host-only nvidia-smi read rendered as a green "GPU
  // available … via native TensorFlow", doubly wrong (no TF, GPU never used).
  if (engineGpu.note) {
    return (
      <div className="mt-3 flex items-start gap-2 text-xs">
        <Cpu className="w-4 h-4 flex-none text-white/50 mt-0.5" />
        <div>
          <div className="text-white/70">{engineGpu.note}</div>
          {engineGpu.gpu_hardware_visible && sysInfo.gpu_available && (
            <div className="text-white/40 mt-0.5">
              Your {sysInfo.gpu_devices.map((g) => g.name).join(", ")} isn&apos;t
              used by this engine — GPU support for it is planned but not
              available yet, so analysis runs on CPU today.
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
          <div className="min-w-0">
            <div className="text-accent-yellow/90">
              Your graphics driver doesn&apos;t match what the analysis engine
              expects, so it can&apos;t use the GPU.
            </div>
            <div className="text-white/40 mt-0.5">Analysis will run on CPU.</div>
            {/* Driver / TensorFlow / CUDA-cuDNN specifics DEMOTED to detail (WP-B2). */}
            <details className="mt-1 text-[11px] text-white/40">
              <summary className="cursor-pointer hover:text-white/60">
                Technical details
              </summary>
              <div className="mt-1 font-mono break-all">
                NVIDIA driver {engineGpu.nvidia_driver ?? "?"} present
                {engineGpu.engine === "wsl" ? ` inside ${engineGpu.distro}` : ""},
                but TensorFlow {engineGpu.tf_version ?? "?"} can&apos;t register
                it — usually a CUDA / cuDNN version mismatch with the bundled TF.
              </div>
            </details>
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
 *
 * Engine-aware: the callout + explainer describe the GPU story of the engine
 * the user has SELECTED (`engine`, the config's `inference_engine`). The three
 * engines have different GPU capabilities today — essentia_tf (NVIDIA-CUDA
 * only), onnx (NVIDIA-CUDA today; DirectML/CoreML planned), native (CPU-only
 * today; DirectML planned) — so a single hardcoded "essentia-tensorflow /
 * CUDA-only" story was flat wrong for the ONNX and (Windows-default) native
 * engines. See vibechek/config.py + docs/ROADMAP.md "engines".
 */
export function CrossVendorGpuInventory({
  sysInfo,
  engine,
}: {
  sysInfo: SystemResources;
  /** Selected inference engine: "essentia_tf" | "onnx" | "native". */
  engine?: string;
}) {
  const [showExplainer, setShowExplainer] = useState(false);

  const devices = sysInfo.gpu_devices ?? [];
  if (devices.length === 0) return null;

  const hasUnsupported = (sysInfo.unsupported_gpu_count ?? 0) > 0;

  // Normalize to the three real engines; anything unknown behaves like the
  // historical default (essentia_tf).
  const eng: "essentia_tf" | "onnx" | "native" =
    engine === "onnx" ? "onnx" : engine === "native" ? "native" : "essentia_tf";

  // Always-visible callout — one line per engine, every claim true today.
  // native must NOT imply NVIDIA would work (it's CPU-only for every vendor) —
  // this reuses the WP-B native phrasing ("runs on CPU today — GPU support is
  // planned but not available yet") so the two blocks stay consistent.
  const callout =
    eng === "native"
      ? "This engine runs on CPU today — GPU support is planned but not available yet, so no GPU is used whatever the vendor."
      : eng === "onnx"
        ? "This engine (ONNX Runtime) accelerates NVIDIA GPUs today. On AMD, Intel, or Apple GPUs it runs on CPU — DirectML and CoreML support is planned but isn't available yet."
        : "This engine (Essentia · TensorFlow) can only use NVIDIA GPUs. On AMD, Intel, or Apple GPUs it runs on CPU. Support for those GPUs is planned through ONNX Runtime but isn't available yet.";

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
          non-NVIDIA card the user might reasonably expect to be used. (The
          native engine's NVIDIA-not-used story is carried by EngineGpuBlock's
          `note` branch above, so this callout doesn't duplicate it.) */}
      {hasUnsupported && (
        <div className="mt-3 flex items-start gap-2 text-xs rounded-sm border border-white/10 bg-white/5 p-2.5">
          <AlertTriangle className="w-4 h-4 flex-none text-accent-yellow mt-0.5" />
          <div className="text-white/70">{callout}</div>
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
              {/* Paragraph 1 — engine-specific "why your card isn't used".
                  This is the demoted/technical layer, so it can name the
                  runtime, but every claim must be true for THIS engine. */}
              {eng === "native" ? (
                <p>
                  This engine — the WSL-free Windows default — runs analysis
                  in-process on a CPU-only <span className="font-mono">ONNX
                  Runtime</span> build. That bundled runtime ships no GPU
                  execution providers, so every card (NVIDIA included) runs on
                  CPU today.
                </p>
              ) : eng === "onnx" ? (
                <p>
                  This engine runs analysis through{" "}
                  <span className="font-mono">ONNX Runtime</span>. The bundled
                  GPU build accelerates NVIDIA cards through CUDA today; the AMD
                  (DirectML/ROCm), Intel (DirectML), and Apple (CoreML) execution
                  providers are wired but not yet shipped and hardware-validated
                  — so your card is detected, but inference still dispatches to
                  CPU.
                </p>
              ) : (
                <p>
                  This engine runs analysis through{" "}
                  <span className="font-mono">essentia-tensorflow</span>, which
                  vendors TensorFlow 2.5 built only with CUDA support. There is
                  no public TF 2.5 build that ships ROCm (AMD), oneDNN-GPU (Intel),
                  or Metal (Apple) backends — so even though your card is
                  detected, the inference graph has no way to dispatch to it.
                </p>
              )}
              <p className="mt-1.5">
                Cross-vendor GPU support — DirectML on Windows (any DX12 GPU,
                including AMD &amp; Intel), CoreML on macOS (Apple Silicon &amp;
                AMD eGPUs), and ROCm/OpenVINO on Linux — is planned through ONNX
                Runtime, but it isn&apos;t available yet.
              </p>
              <p className="mt-1.5">
                Until then, analysis runs on CPU — still fast on a modern
                multi-core machine. The workers slider in the Analysis section
                above is already capped to what your memory can support, so it&apos;s
                set up to use your cores without manual tuning.
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
        <span className="text-[10px] px-1.5 py-0.5 rounded-sm bg-accent-green/20 text-accent-green font-mono uppercase tracking-wider">
          accelerated
        </span>
      ) : (
        <span className="text-[10px] px-1.5 py-0.5 rounded-sm bg-white/10 text-white/50 font-mono uppercase tracking-wider">
          CPU-only (not supported by this engine yet)
        </span>
      )}
    </div>
  );
}

