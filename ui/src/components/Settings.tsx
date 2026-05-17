import { useEffect, useState } from "react";
import { open as openDialog } from "@tauri-apps/plugin-dialog";
import {
  Download, Cpu, FolderOpen, Settings as SettingsIcon, Shield,
  Zap, AlertTriangle, CheckCircle2,
} from "lucide-react";

import { useConfigStore, useOperationStore } from "../stores";
import { rpc, sidecarStatus } from "../hooks/useSidecar";
import type { SystemResources } from "../types";

export function Settings() {
  const cfg = useConfigStore((s) => s.config);
  const updateAnalysis = useConfigStore((s) => s.updateAnalysis);
  const updateTagging = useConfigStore((s) => s.updateTagging);
  const updateDuplicates = useConfigStore((s) => s.updateDuplicates);
  const updateOrganization = useConfigStore((s) => s.updateOrganization);

  const active = useOperationStore((s) => s.active);
  const begin = useOperationStore((s) => s.begin);
  const finish = useOperationStore((s) => s.finish);
  const fail = useOperationStore((s) => s.fail);

  const [sidecarBinary, setSidecarBinary] = useState<string | null>(null);
  const [sysInfo, setSysInfo] = useState<SystemResources | null>(null);

  useEffect(() => {
    sidecarStatus().then((s) => setSidecarBinary(s.binary)).catch(() => {});
    rpc<SystemResources>("system_info")
      .then((info) => {
        setSysInfo(info);
        // If workers is still 0 (unconfigured), bump it to the recommended count
        if (cfg.analysis.workers === 0) {
          updateAnalysis({ workers: info.recommended_workers });
        }
      })
      .catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const handleDownloadModels = async () => {
    begin("download-models");
    try {
      await rpc("download_models", {
        models_dir: cfg.analysis.models_dir || undefined,
      });
      finish();
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

      <ResourcesSection sysInfo={sysInfo} />

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
            {!sysInfo ? "Detecting GPU…" :
              sysInfo.gpu_available
                ? `${sysInfo.gpu_devices[0]?.name ?? "GPU"} available — auto will use it. Requires Essentia + CUDA setup.`
                : "No GPU detected. Stays on CPU regardless of choice."}
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

      <Section title="About" subtitle="">
        <div className="text-xs text-white/40 font-mono break-all">
          Sidecar: {sidecarBinary ?? "?"}
        </div>
        <div className="text-xs text-white/40 mt-1">
          Settings live in-memory for now; TOML persistence comes in a later release.
        </div>
      </Section>
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
 * Read-only summary of detected system resources, shown at the top of Settings
 * so users see what's available before choosing how much to spend.
 */
function ResourcesSection({ sysInfo }: { sysInfo: SystemResources | null }) {
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
          value={sysInfo.gpu_available ? "available" : "none"}
          accent={sysInfo.gpu_available ? "green" : "neutral"}
        />
      </div>

      {sysInfo.gpu_available ? (
        <div className="mt-3 flex items-start gap-2 text-xs">
          <CheckCircle2 className="w-4 h-4 flex-none text-accent-green mt-0.5" />
          <div>
            <div className="text-white/80">
              {sysInfo.gpu_devices.map((g) => g.name).join(", ")}
            </div>
            {sysInfo.cuda_runtime && (
              <div className="text-white/40 font-mono">
                CUDA driver {sysInfo.cuda_runtime}
              </div>
            )}
          </div>
        </div>
      ) : sysInfo.cuda_runtime ? (
        <div className="mt-3 flex items-start gap-2 text-xs text-accent-yellow">
          <AlertTriangle className="w-4 h-4 flex-none mt-0.5" />
          <div>
            NVIDIA driver {sysInfo.cuda_runtime} is installed but TensorFlow
            can&apos;t see the GPU. Check that your CUDA / cuDNN versions match
            what TensorFlow expects (usually CUDA 11.2 + cuDNN 8.1 for the
            Essentia-bundled TF).
          </div>
        </div>
      ) : (
        <div className="mt-3 text-xs text-white/40">
          No NVIDIA GPU detected. Analysis will run on CPU — still fast with
          enough workers.
        </div>
      )}

      <div className="mt-3 text-[11px] text-white/30 font-mono break-all">
        {sysInfo.platform}
      </div>
    </Section>
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
