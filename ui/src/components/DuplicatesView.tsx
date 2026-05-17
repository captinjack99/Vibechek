import { useState } from "react";
import { Copy, Search, ArrowRight, FileAudio, AlertCircle } from "lucide-react";
import { open as openDialog } from "@tauri-apps/plugin-dialog";

import { useOperationStore, useConfigStore, useLibraryStore } from "../stores";
import { rpc } from "../hooks/useSidecar";
import type { DuplicateGroup, DuplicateReport } from "../types";
import { TagBadge } from "./TagBadges";

export function DuplicatesView() {
  const libraryPath = useLibraryStore((s) => s.libraryPath);

  const active = useOperationStore((s) => s.active);
  const begin = useOperationStore((s) => s.begin);
  const finish = useOperationStore((s) => s.finish);
  const fail = useOperationStore((s) => s.fail);
  const errorMsg = useOperationStore((s) => s.error);

  const report = useOperationStore((s) => s.duplicateReport);
  const setReport = useOperationStore((s) => s.setDuplicateReport);

  const dupCfg = useConfigStore((s) => s.config.duplicates);

  const [scanPath, setScanPath] = useState<string | null>(libraryPath);

  const handleChooseFolder = async () => {
    const selected = await openDialog({ directory: true, multiple: false });
    if (typeof selected === "string") setScanPath(selected);
  };

  const handleScan = async () => {
    if (!scanPath) return;
    begin("dedupe");
    try {
      const r = await rpc<DuplicateReport>("find_duplicates", {
        path: scanPath,
        use_md5: dupCfg.use_md5,
        use_chromaprint: dupCfg.use_chromaprint,
      });
      setReport(r);
      finish();
    } catch (e) {
      fail(String(e));
    }
  };

  return (
    <div className="h-full flex flex-col">
      {/* Header */}
      <div className="flex items-center justify-between px-4 py-3 border-b border-white/5">
        <div>
          <h1 className="font-display font-semibold text-white">Duplicates</h1>
          <p className="text-xs text-white/40 truncate max-w-md">
            {scanPath ?? "no folder selected"}
          </p>
        </div>
        <div className="flex items-center gap-2">
          <button className="btn-ghost" onClick={handleChooseFolder}>
            Choose folder
          </button>
          <button
            className="btn-primary"
            onClick={handleScan}
            disabled={!scanPath || active !== null}
          >
            <Search className="w-4 h-4" />
            Scan
          </button>
        </div>
      </div>

      {errorMsg && (
        <div className="m-4 panel-pad text-sm text-accent-red flex gap-2">
          <AlertCircle className="w-4 h-4 flex-none mt-0.5" />
          <div className="break-words">{errorMsg}</div>
        </div>
      )}

      {!report ? <EmptyState scanPath={scanPath} /> : <ReportView report={report} />}
    </div>
  );
}

function EmptyState({ scanPath }: { scanPath: string | null }) {
  return (
    <div className="flex-1 flex items-center justify-center">
      <div className="text-center max-w-md">
        <div className="w-16 h-16 mx-auto mb-4 rounded-2xl bg-white/5 flex items-center justify-center">
          <Copy className="w-8 h-8 text-white/30" />
        </div>
        <h2 className="text-xl font-display font-semibold mb-2">
          {scanPath ? "Ready to scan" : "Pick a folder"}
        </h2>
        <p className="text-white/50">
          {scanPath
            ? "Vibechek will find byte-identical (MD5) and acoustically identical (Chromaprint) duplicates. Nothing is moved or deleted without your confirmation."
            : "Choose a folder above to scan for duplicates."}
        </p>
      </div>
    </div>
  );
}

function ReportView({ report }: { report: DuplicateReport }) {
  const { summary, exact_duplicates, audio_duplicates } = report;
  return (
    <div className="flex-1 overflow-auto px-4 py-3">
      {/* Summary */}
      <div className="grid grid-cols-4 gap-3 mb-4">
        <Stat label="Total files" value={summary.total_files} />
        <Stat label="Exact dupes" value={summary.exact_duplicate_files} />
        <Stat label="Audio dupes" value={summary.audio_duplicate_files} />
        <Stat
          label="Recoverable"
          value={`${summary.space_recoverable_mb.toFixed(1)} MB`}
        />
      </div>

      {summary.total_duplicates === 0 && (
        <div className="panel-pad text-center text-white/50">
          No duplicates found.
        </div>
      )}

      {exact_duplicates.length > 0 && (
        <Section title="Exact duplicates" subtitle="Byte-identical files (MD5)">
          {exact_duplicates.map((g) => (
            <GroupCard key={g.key} group={g} />
          ))}
        </Section>
      )}

      {audio_duplicates.length > 0 && (
        <Section title="Audio duplicates" subtitle="Same audio, different encoding (Chromaprint)">
          {audio_duplicates.map((g) => (
            <GroupCard key={g.key} group={g} />
          ))}
        </Section>
      )}
    </div>
  );
}

function Stat({ label, value }: { label: string; value: number | string }) {
  return (
    <div className="panel-pad">
      <div className="label">{label}</div>
      <div className="text-2xl font-display font-semibold tabular-nums">{value}</div>
    </div>
  );
}

function Section({
  title,
  subtitle,
  children,
}: {
  title: string;
  subtitle: string;
  children: React.ReactNode;
}) {
  return (
    <div className="mb-6">
      <div className="flex items-baseline gap-3 mb-2">
        <h2 className="font-display font-semibold text-white">{title}</h2>
        <p className="text-xs text-white/40">{subtitle}</p>
      </div>
      <div className="space-y-2">{children}</div>
    </div>
  );
}

function GroupCard({ group }: { group: DuplicateGroup }) {
  return (
    <div className="panel-pad">
      <div className="flex items-center gap-2 mb-3">
        <FileAudio className="w-4 h-4 text-accent-green" />
        <div className="flex-1 text-sm font-medium truncate">{group.keep.filename}</div>
        <TagBadge color="green">keep</TagBadge>
      </div>
      <div className="text-xs text-white/40 font-mono break-all mb-3 pl-6">
        {group.keep.path}
      </div>

      {group.duplicates.map((d) => (
        <div key={d.path} className="pl-6 flex items-center gap-2 text-xs text-white/60 py-1">
          <ArrowRight className="w-3 h-3 text-white/30" />
          <span className="flex-1 truncate font-mono break-all">{d.path}</span>
          <span className="text-white/40">{d.size_mb.toFixed(1)}M</span>
        </div>
      ))}
    </div>
  );
}
