/**
 * Side panel that opens when a track row is clicked.
 *
 * Shows:
 *   - File metadata (path, size, parsed-from-filename hints)
 *   - Existing tags vs ML analysis side-by-side, with diff arrows
 *   - "Apply ML tags" button (writes just this one track)
 *
 * Layout is a 320px-wide right-rail panel; collapses when no track is
 * selected.
 */

import { useMemo } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { X, ChevronRight, FileAudio, AlertTriangle, CheckCircle2 } from "lucide-react";

import { useLibraryStore, useUIStore, useConfigStore, useOperationStore } from "../stores";
import { rpc } from "../hooks/useSidecar";
import type { TrackAnalysis, ExistingTags, MLResult } from "../types";
import { TagBadge, EnergyBar } from "./TagBadges";

export function TrackDetails() {
  const selectedPath = useUIStore((s) => s.selectedTrackPath);
  const setSelected = useUIStore((s) => s.setSelectedTrack);
  const tracks = useLibraryStore((s) => s.tracks);

  const track = useMemo(
    () => tracks.find((t) => t.path === selectedPath) ?? null,
    [tracks, selectedPath],
  );

  return (
    <AnimatePresence>
      {track && (
        <motion.aside
          initial={{ x: 320, opacity: 0 }}
          animate={{ x: 0, opacity: 1 }}
          exit={{ x: 320, opacity: 0 }}
          transition={{ duration: 0.2, ease: "easeOut" }}
          className="w-80 shrink-0 border-l border-white/5 bg-surface-100 overflow-y-auto"
        >
          <DetailContent track={track} onClose={() => setSelected(null)} />
        </motion.aside>
      )}
    </AnimatePresence>
  );
}

function DetailContent({
  track,
  onClose,
}: {
  track: TrackAnalysis;
  onClose: () => void;
}) {
  const taggingCfg = useConfigStore((s) => s.config.tagging);
  const active = useOperationStore((s) => s.active);
  const begin = useOperationStore((s) => s.begin);
  const finish = useOperationStore((s) => s.finish);
  const fail = useOperationStore((s) => s.fail);

  const handleApplyOne = async () => {
    begin("tag");
    try {
      await rpc("apply_ml_tags", {
        analysis: { tracks: [track] },
        confidence: taggingCfg.genre_confidence_threshold,
        skip_bpm_and_key: taggingCfg.skip_bpm_and_key,
        preserve_rekordbox_frames: taggingCfg.preserve_rekordbox_frames,
      });
      finish();
    } catch (e) {
      fail(String(e));
    }
  };

  const ml = track.ml_analysis ?? null;
  const ext = track.existing_tags ?? {};

  return (
    <div>
      {/* Header */}
      <div className="sticky top-0 z-10 bg-surface-100 border-b border-white/5 px-4 py-3 flex items-start gap-2">
        <FileAudio className="w-5 h-5 text-accent flex-none mt-0.5" />
        <div className="flex-1 min-w-0">
          <div className="text-sm font-medium text-white truncate">
            {track.filename_title ?? track.filename}
          </div>
          {track.filename_artist && (
            <div className="text-xs text-white/50 truncate">
              {track.filename_artist}
            </div>
          )}
        </div>
        <button
          onClick={onClose}
          className="text-white/40 hover:text-white p-1 -m-1"
          title="Close (Esc)"
        >
          <X className="w-4 h-4" />
        </button>
      </div>

      {/* Body */}
      <div className="p-4 space-y-5">
        {track.error && (
          <Notice kind="error">{track.error}</Notice>
        )}
        {ml?.ml_error && (
          <Notice kind="error">ML failed: {ml.ml_error}</Notice>
        )}

        <FileSection track={track} />

        {ml ? (
          <DiffSection existing={ext} ml={ml} confidenceThreshold={taggingCfg.genre_confidence_threshold} />
        ) : (
          <Notice kind="info">
            No ML analysis for this track yet. Run analyze on the library to populate.
          </Notice>
        )}

        {ml && (
          <button
            className="btn-primary w-full justify-center"
            onClick={handleApplyOne}
            disabled={active !== null}
          >
            Apply ML tags to this file
          </button>
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------

function FileSection({ track }: { track: TrackAnalysis }) {
  return (
    <Section title="File">
      <Row label="Path" value={track.path} mono small />
      <Row label="Size" value={`${track.size_mb.toFixed(1)} MB`} />
      {track.filename_bpm && <Row label="BPM (from filename)" value={String(track.filename_bpm)} />}
      {track.filename_key && <Row label="Key (from filename)" value={track.filename_key} />}
      {track.filename_mix && <Row label="Mix type" value={track.filename_mix} />}
    </Section>
  );
}

/**
 * Diff section — for each tag we know how to write, show existing vs ML
 * with an arrow when they differ. Greys out unchanged values.
 */
function DiffSection({
  existing,
  ml,
  confidenceThreshold,
}: {
  existing: ExistingTags;
  ml: MLResult;
  confidenceThreshold: number;
}) {
  const rows = buildDiffRows(existing, ml);
  const willApplyGenre =
    (ml.ml_genre_confidence ?? 0) >= confidenceThreshold && !!ml.ml_subgenre;

  return (
    <Section title="Tags" subtitle="existing → ML">
      {rows.map((r) => (
        <DiffRow key={r.label} row={r} />
      ))}

      <div className="mt-3 pt-3 border-t border-white/5 text-xs">
        {ml.ml_genre_confidence != null && (
          <div className="flex items-center gap-2 text-white/60">
            <span>Genre confidence:</span>
            <ConfidenceBadge
              confidence={ml.ml_genre_confidence}
              threshold={confidenceThreshold}
            />
            <span className="font-mono">
              {Math.round(ml.ml_genre_confidence * 100)}%
            </span>
          </div>
        )}
        <div className="mt-1 text-[11px] text-white/40">
          {willApplyGenre
            ? "Genre will be written when Apply is clicked."
            : `Genre below ${Math.round(confidenceThreshold * 100)}% threshold — won't be written.`}
        </div>
      </div>
    </Section>
  );
}

interface DiffRowData {
  label: string;
  existing: string | number | null | undefined;
  next: string | number | null | undefined;
  badgeColor?: "purple" | "cyan" | "green" | "yellow" | "red" | "neutral";
  renderNext?: (v: string | number) => React.ReactNode;
}

function buildDiffRows(existing: ExistingTags, ml: MLResult): DiffRowData[] {
  return [
    {
      label: "Genre",
      existing: existing.genre,
      next: ml.ml_subgenre ?? ml.ml_genre,
      badgeColor: "purple",
    },
    { label: "BPM", existing: existing.bpm, next: ml.ml_bpm, badgeColor: "cyan" },
    { label: "Key", existing: existing.key, next: ml.ml_key, badgeColor: "green" },
    {
      label: "Energy",
      existing: existing.energy as number | string | null | undefined,
      next: ml.ml_energy,
      renderNext: (v) => <EnergyBar level={Number(v)} />,
    },
    { label: "Mood", existing: existing.mood, next: ml.ml_mood, badgeColor: "purple" },
    { label: "Timeslot", existing: existing.timeslot, next: ml.ml_timeslot },
    { label: "Direction", existing: existing.direction, next: ml.ml_direction },
    { label: "Vocal", existing: existing.vocal, next: ml.ml_vocal },
  ].filter((r) => r.existing != null || r.next != null);
}

function DiffRow({ row }: { row: DiffRowData }) {
  const changed =
    row.existing != null &&
    row.next != null &&
    String(row.existing).toLowerCase() !== String(row.next).toLowerCase();
  const newOnly = row.existing == null && row.next != null;

  return (
    <div className="flex items-center gap-2 py-1.5 border-b border-white/[0.04] last:border-0">
      <div className="w-20 text-xs text-white/50">{row.label}</div>
      <div className="flex-1 flex items-center gap-2 min-w-0">
        <span
          className={`text-xs truncate ${
            row.existing == null ? "text-white/30 italic" : "text-white/70"
          }`}
        >
          {row.existing == null ? "(empty)" : String(row.existing)}
        </span>
        {(changed || newOnly) && (
          <>
            <ChevronRight className="w-3 h-3 text-white/30 flex-none" />
            {row.renderNext && row.next != null ? (
              row.renderNext(row.next)
            ) : (
              <TagBadge color={changed || newOnly ? row.badgeColor ?? "neutral" : "neutral"}>
                {String(row.next)}
              </TagBadge>
            )}
          </>
        )}
      </div>
    </div>
  );
}

function ConfidenceBadge({
  confidence,
  threshold,
}: {
  confidence: number;
  threshold: number;
}) {
  return confidence >= threshold ? (
    <CheckCircle2 className="w-3.5 h-3.5 text-accent-green" />
  ) : (
    <AlertTriangle className="w-3.5 h-3.5 text-accent-yellow" />
  );
}

// ---------------------------------------------------------------------------

function Section({
  title,
  subtitle,
  children,
}: {
  title: string;
  subtitle?: string;
  children: React.ReactNode;
}) {
  return (
    <div>
      <div className="flex items-baseline gap-2 mb-2">
        <h3 className="text-[11px] uppercase tracking-wider text-white/40 font-medium">
          {title}
        </h3>
        {subtitle && (
          <span className="text-[10px] text-white/30">{subtitle}</span>
        )}
      </div>
      <div className="panel-pad">{children}</div>
    </div>
  );
}

function Row({
  label,
  value,
  mono,
  small,
}: {
  label: string;
  value: string;
  mono?: boolean;
  small?: boolean;
}) {
  return (
    <div className="flex items-baseline gap-2 py-1">
      <div className="w-20 text-xs text-white/50 flex-none">{label}</div>
      <div
        className={`flex-1 min-w-0 text-white/80 ${mono ? "font-mono" : ""} ${
          small ? "text-[11px]" : "text-xs"
        } break-all`}
      >
        {value}
      </div>
    </div>
  );
}

function Notice({
  kind,
  children,
}: {
  kind: "info" | "error";
  children: React.ReactNode;
}) {
  return (
    <div
      className={`panel-pad flex gap-2 text-xs ${
        kind === "error" ? "text-accent-red" : "text-white/60"
      }`}
    >
      <AlertTriangle className="w-4 h-4 flex-none mt-0.5" />
      <div>{children}</div>
    </div>
  );
}
