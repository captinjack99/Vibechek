/**
 * Pre-flight dialog — shown when the user tries to start `analyze` but the
 * sidecar reports it can't (essentia missing, models missing, etc.).
 *
 * Each missing prerequisite has its own row with:
 *   - red/green status icon
 *   - one-line explanation
 *   - a fix button (download models) OR copy-pasteable install command (essentia)
 *
 * Once everything is green, the dialog auto-dismisses and the original
 * Analyze action proceeds.
 */

import { useState } from "react";
import { motion } from "framer-motion";
import {
  X, CheckCircle2, AlertCircle, Download, Copy, ExternalLink, Loader2,
} from "lucide-react";

import { rpc } from "../hooks/useSidecar";
import { useOperationStore } from "../stores";
import type { PreflightResult } from "../types";

interface Props {
  preflight: PreflightResult;
  onRefresh: (next: PreflightResult) => void;
  onClose: () => void;
  onReady: () => void;
}

export function PreflightDialog({ preflight, onRefresh, onClose, onReady }: Props) {
  const begin = useOperationStore((s) => s.begin);
  const finish = useOperationStore((s) => s.finish);
  const fail = useOperationStore((s) => s.fail);

  const [downloading, setDownloading] = useState(false);

  const handleDownloadModels = async () => {
    setDownloading(true);
    begin("download-models");
    try {
      await rpc("download_models", {
        models_dir: preflight.models.models_dir,
      });
      finish();
      // Re-check after download completes
      const next = await rpc<PreflightResult>("preflight", {
        models_dir: preflight.models.models_dir,
      });
      onRefresh(next);
      if (next.ready) onReady();
    } catch (e) {
      fail(String(e));
    } finally {
      setDownloading(false);
    }
  };

  const handleRecheck = async () => {
    const next = await rpc<PreflightResult>("preflight", {
      models_dir: preflight.models.models_dir,
    });
    onRefresh(next);
    if (next.ready) onReady();
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
        className="panel max-w-xl w-full max-h-[80vh] flex flex-col"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex items-start gap-3 px-5 py-4 border-b border-white/5">
          <AlertCircle className="w-6 h-6 text-accent-yellow flex-none mt-0.5" />
          <div className="flex-1">
            <h2 className="font-display font-semibold text-lg">
              Vibechek isn&apos;t ready to analyze
            </h2>
            <p className="text-sm text-white/60 mt-0.5">
              Fix the items below, then click Re-check.
            </p>
          </div>
          <button onClick={onClose} className="text-white/40 hover:text-white -m-1 p-1">
            <X className="w-5 h-5" />
          </button>
        </div>

        {/* Body */}
        <div className="flex-1 overflow-auto px-5 py-4 space-y-4">
          <EssentiaRow check={preflight.essentia} platform={preflight.platform} />
          <ModelsRow
            check={preflight.models}
            downloading={downloading}
            onDownload={handleDownloadModels}
          />
        </div>

        {/* Footer */}
        <div className="flex items-center justify-end gap-2 px-5 py-3 border-t border-white/5">
          <button className="btn-ghost" onClick={onClose}>
            Cancel
          </button>
          <button
            className="btn-primary"
            onClick={handleRecheck}
            disabled={downloading}
          >
            Re-check
          </button>
        </div>
      </motion.div>
    </motion.div>
  );
}

// ---------------------------------------------------------------------------

function EssentiaRow({
  check,
  platform,
}: {
  check: PreflightResult["essentia"];
  platform: string;
}) {
  const isWindows = /win/i.test(platform);
  return (
    <Row
      ok={check.installed}
      title="Essentia (Python ML library)"
      okSubtitle={check.version ? `Installed (version ${check.version})` : "Installed"}
      failSubtitle={check.error ?? "Not installed"}
    >
      {!check.installed && (
        <>
          {isWindows ? (
            <div className="space-y-2">
              <p className="text-sm text-white/70">
                <strong>Essentia doesn&apos;t publish a Windows wheel.</strong>{" "}
                Two options:
              </p>
              <ol className="list-decimal list-inside text-sm text-white/70 space-y-1 ml-2">
                <li>
                  Run Vibechek inside WSL Ubuntu (recommended for analyze).
                  Inside WSL:
                  <CodeBlock>{`sudo apt install python3-pip libchromaprint-tools\npip install essentia-tensorflow vibechek`}</CodeBlock>
                </li>
                <li>
                  Skip analyze entirely — dedupe / organize / tag / backup all
                  work on native Windows without Essentia.
                </li>
              </ol>
              <a
                href="https://github.com/papapew/Vibechek/blob/main/docs/INSTALL.md"
                target="_blank"
                rel="noreferrer"
                className="inline-flex items-center gap-1 text-sm text-accent hover:underline"
              >
                Full Windows install guide
                <ExternalLink className="w-3 h-3" />
              </a>
            </div>
          ) : (
            <div className="space-y-2">
              <p className="text-sm text-white/70">
                Install Essentia with pip:
              </p>
              <CodeBlock>pip install essentia-tensorflow</CodeBlock>
              <p className="text-xs text-white/40">
                Then restart Vibechek.
              </p>
            </div>
          )}
        </>
      )}
    </Row>
  );
}

function ModelsRow({
  check,
  downloading,
  onDownload,
}: {
  check: PreflightResult["models"];
  downloading: boolean;
  onDownload: () => void;
}) {
  const allOk = check.missing.length === 0;
  return (
    <Row
      ok={allOk}
      title="ML model files (~800 MB)"
      okSubtitle={`${check.found.length} models, ${check.total_size_mb.toFixed(0)} MB in ${check.models_dir}`}
      failSubtitle={`${check.missing.length} of ${check.found.length + check.missing.length} missing`}
    >
      {!allOk && (
        <div className="space-y-2">
          <p className="text-sm text-white/70">
            Vibechek will download these from the Essentia model index:
          </p>
          <ul className="text-xs font-mono text-white/50 space-y-0.5 ml-4 list-disc">
            {check.missing.slice(0, 6).map((m) => (
              <li key={m}>{m}.pb</li>
            ))}
            {check.missing.length > 6 && (
              <li>...and {check.missing.length - 6} more</li>
            )}
          </ul>
          <button
            className="btn-primary"
            onClick={onDownload}
            disabled={downloading}
          >
            {downloading ? (
              <>
                <Loader2 className="w-4 h-4 animate-spin" />
                Downloading...
              </>
            ) : (
              <>
                <Download className="w-4 h-4" />
                Download models now
              </>
            )}
          </button>
        </div>
      )}
    </Row>
  );
}

// ---------------------------------------------------------------------------

function Row({
  ok,
  title,
  okSubtitle,
  failSubtitle,
  children,
}: {
  ok: boolean;
  title: string;
  okSubtitle: string;
  failSubtitle: string;
  children?: React.ReactNode;
}) {
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
            {ok ? okSubtitle : failSubtitle}
          </div>
        </div>
      </div>
      {!ok && children && <div className="mt-3 ml-8">{children}</div>}
    </div>
  );
}

function CodeBlock({ children }: { children: string }) {
  const [copied, setCopied] = useState(false);
  const handleCopy = () => {
    void navigator.clipboard.writeText(children);
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  };
  return (
    <div className="relative">
      <pre className="bg-surface-300 border border-white/10 rounded-md p-3 text-xs font-mono text-white/80 overflow-x-auto whitespace-pre">
        {children}
      </pre>
      <button
        onClick={handleCopy}
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
