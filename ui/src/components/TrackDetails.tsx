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

import { useEffect, useMemo, useState } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { X, ChevronRight, FileAudio, AlertTriangle, CheckCircle2, Play } from "lucide-react";

import {
  useLibraryStore, useUIStore, useConfigStore, useOperationStore,
  useNotificationStore, usePlayerStore,
} from "../stores";
import { useApplyTags } from "../hooks/useApplyTags";
import type { TrackAnalysis, ExistingTags, MLResult } from "../types";
import { TagBadge, EnergyBar } from "./TagBadges";
import {
  compatibleCamelot,
  useLibraryFiltersStore,
  type CamelotMode,
} from "./LibraryFilters";
import {
  genreProvenance,
  hasInterestingProvenance,
  keyProvenance,
  reviewReason,
  type GenreProvenance,
} from "../lib/review";

export function TrackDetails() {
  const selectedPath = useUIStore((s) => s.selectedTrackPath);
  const setSelected = useUIStore((s) => s.setSelectedTrack);
  const tracks = useLibraryStore((s) => s.tracks);

  const track = useMemo(
    () => tracks.find((t) => t.path === selectedPath) ?? null,
    [tracks, selectedPath],
  );

  // If the selected path no longer exists in the tracks list (e.g. after a
  // re-scan dropped it, or a forget-and-reload swapped libraries), clear the
  // selection rather than leaving the panel in a zombie state where it
  // unmounts but the store still claims a path is selected.
  useEffect(() => {
    if (selectedPath && !track) {
      setSelected(null);
    }
  }, [selectedPath, track, setSelected]);

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
  const notify = useNotificationStore((s) => s.notify);
  const { apply: applyTags } = useApplyTags();

  // Local re-entrancy guard. The store-wide `active` flag flips once the
  // RPC actually starts, but useApplyTags has a tick of setup before that —
  // a fast double click would fire two RPCs before `active` updated. Track
  // it locally and disable the button immediately.
  const [applying, setApplying] = useState(false);

  const handleApplyOne = async () => {
    if (applying) return;
    setApplying(true);
    try {
      const result = await applyTags([track]);
      if (!result) return; // failure already surfaced via useOperationStore.error
      const wrote = result.applied + result.other;
      if (wrote === 0 && result.skipped > 0) {
        notify("Genre skipped — confidence below threshold", {
          detail: "Lower the threshold in Settings if you want it written anyway.",
          kind: "info",
        });
      } else {
        notify(`Tags applied to ${track.filename}`, {
          kind: result.errors.length > 0 ? "info" : "success",
        });
      }
    } finally {
      setApplying(false);
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

        <PreviewButton path={track.path} filename={track.filename} />

        <FileSection track={track} />

        {ml ? (
          <>
            <DiffSection existing={ext} ml={ml} confidenceThreshold={taggingCfg.genre_confidence_threshold} />
            <GenreSourcesSection existing={ext} ml={ml} />
            <CompatibleKeysSection mlKey={ml.ml_key} />
          </>
        ) : (
          <Notice kind="info">
            No ML analysis for this track yet. Run analyze on the library to populate.
          </Notice>
        )}

        {ml && (
          <button
            className="btn-primary w-full justify-center"
            onClick={handleApplyOne}
            disabled={active !== null || applying}
          >
            {applying ? "Applying…" : "Apply ML tags to this file"}
          </button>
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------

/**
 * Hands the track to the global player. We don't embed a player here anymore —
 * the single <GlobalAudioPlayer/> bar owns playback so navigating away or
 * picking another track can't leave audio running. Shows "Now playing" when
 * this track is the one loaded in the global player.
 */
function PreviewButton({ path, filename }: { path: string; filename: string }) {
  const play = usePlayerStore((s) => s.play);
  const currentPath = usePlayerStore((s) => s.path);
  const isCurrent = currentPath === path;

  return (
    <button
      className="btn-ghost w-full justify-center"
      onClick={() => play(path, filename)}
      title="Preview in the player bar"
    >
      <Play className="w-4 h-4" />
      {isCurrent ? "Restart preview" : "Preview"}
    </button>
  );
}

function FileSection({ track }: { track: TrackAnalysis }) {
  return (
    <Section title="File">
      <Row label="Path" value={track.path} mono small />
      <Row label="Size" value={`${(track.size_mb ?? 0).toFixed(1)} MB`} />
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
  /** Faint provenance hint shown after the value (e.g. how it was derived). */
  note?: string;
  /** Inline affordance rendered at the row's end regardless of diff state
   *  (unlike `note`, which only accompanies a changed value). */
  hint?: React.ReactNode;
}

function buildDiffRows(existing: ExistingTags, ml: MLResult): DiffRowData[] {
  // Annotate the literal so the union widens the badge color to the named
  // literal type (otherwise the energy row's extra `renderNext` shape causes
  // TS to widen `badgeColor: "purple"` to `string`).
  const rows: DiffRowData[] = [
    {
      label: "Genre",
      existing: existing.genre,
      next: ml.ml_subgenre ?? ml.ml_genre,
      badgeColor: "purple",
    },
    { label: "BPM", existing: existing.bpm, next: ml.ml_bpm, badgeColor: "neutral" },
    {
      label: "Key",
      existing: existing.key,
      next: ml.ml_key,
      badgeColor: "green",
      hint: <KeyTagHint ml={ml} />,
    },
    {
      label: "Energy",
      existing: existing.energy as number | string | null | undefined,
      next: ml.ml_energy,
      // `v` is whatever `next` resolved to. Number(undefined) is NaN which
      // EnergyBar's Math.max(0,…) coerces to 0 — explicit guard keeps
      // intent clear and avoids future drift if EnergyBar's input contract
      // tightens.
      renderNext: (v: string | number) => {
        const n = Number(v);
        return <EnergyBar level={Number.isFinite(n) ? n : 0} />;
      },
      // Mixed In Key's 1-10 energy, when the file's tags carry one. A different
      // scale from Vibechek's 0-5 — surfaced for reference, never merged.
      hint: existing.energy_mik ? (
        <span
          className="text-[10px] text-white/40 flex-none ml-auto"
          title="Mixed In Key energy from your file's tags (1-10 scale, separate from Vibechek's 0-5)"
        >
          MIK energy {existing.energy_mik}/10
        </span>
      ) : undefined,
    },
    { label: "Mood", existing: existing.mood, next: ml.ml_mood, badgeColor: "purple" },
    { label: "Timeslot", existing: existing.timeslot, next: ml.ml_timeslot },
    { label: "Direction", existing: existing.direction, next: ml.ml_direction },
    {
      label: "Vocal",
      existing: existing.vocal,
      next: ml.ml_vocal,
      // Augment-not-overwrite signal: a "feat."/"ft." credit upgraded an
      // instrumental-sounding read to Vocal. Surface WHY so it's not a black box.
      note: ml.ml_vocal_source === "feat_credit" ? "from “feat.” credit" : undefined,
    },
  ];
  return rows.filter((r) => r.existing != null || r.next != null);
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
          title={row.existing == null ? undefined : String(row.existing)}
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
            {row.note && (
              <span className="text-[10px] text-white/40 italic">{row.note}</span>
            )}
          </>
        )}
        {row.hint}
      </div>
    </div>
  );
}

/**
 * Key-tag provenance affordance (trust-UX #3) — read-only surfacing next to
 * the Key diff row. When the file carries a parseable key tag we say whether
 * the audio read agrees. The audio read is ALWAYS the effective key (measured
 * substantially more accurate than embedded tag keys), so unlike genre there's
 * nothing to approve or revert here — see `keyProvenance` in lib/review.
 */
function KeyTagHint({ ml }: { ml: MLResult }) {
  const prov = keyProvenance(ml);
  if (!prov) return null;
  if (!prov.audio) {
    // No audio key to agree WITH — claiming "matches your tag" here would be
    // a false confirmation. Show the tag value neutrally instead.
    return (
      <span
        className="text-[10px] text-white/40 flex-none ml-auto"
        title="Your file's key tag (no audio key was detected for this track)"
      >
        tag: {prov.tag}
      </span>
    );
  }
  if (!prov.conflict) {
    return (
      <span
        className="text-[10px] text-white/40 flex-none ml-auto"
        title="Your file's key tag agrees with the audio analysis"
      >
        matches your tag
      </span>
    );
  }
  return (
    <span
      className="text-[10px] text-accent-yellow flex-none ml-auto"
      title={
        `Your file's key tag reads ${prov.tag}, the audio analysis reads ${prov.audio ?? "(none)"}. ` +
        "Audio keys measured substantially more accurate than embedded tag keys, " +
        "so Vibechek keeps the audio read — trust your ears on conflicts."
      }
    >
      tag says {prov.tag}
    </span>
  );
}

/**
 * Compact "Compatible keys" panel under the diff section.
 *
 * Shows the harmonic-mix neighbours of `mlKey` per the current Camelot mode.
 * Clicking a pill writes the key into the library filter store, scoping the
 * library browser to just that key — same UX as picking it from the Camelot
 * chip grid. Renders nothing for tracks with no parseable key (which is
 * always the bulk of any DJ library).
 */
function CompatibleKeysSection({ mlKey }: { mlKey: string | null | undefined }) {
  const setFilters = useLibraryFiltersStore((s) => s.setFilters);
  const filters = useLibraryFiltersStore((s) => s.filters);
  const compatible = useMemo(
    () => compatibleCamelot(mlKey, filters.camelotMode as CamelotMode),
    [mlKey, filters.camelotMode],
  );
  if (compatible.length === 0) return null;

  const onPick = (key: string) => {
    // Replace the current camelot selection rather than appending — the user
    // clicked one pill to scope, not to layer onto an existing multi-select.
    setFilters({ ...filters, camelot: new Set([key]) });
  };

  return (
    <Section title="Compatible keys" subtitle={filters.camelotMode}>
      <div className="flex flex-wrap gap-1.5">
        {compatible.map((k) => (
          <button
            key={k}
            type="button"
            onClick={() => onPick(k)}
            className="text-[11px] font-mono px-2 py-1 rounded-sm bg-white/5 text-white/80 border border-white/10 hover:bg-accent/20 hover:text-accent hover:border-accent/30"
            title={`Filter library to ${k}`}
          >
            {k}
          </button>
        ))}
      </div>
    </Section>
  );
}

/**
 * Genre provenance — the trust-UX payoff. Shows the (up to) three signals the
 * analyzer reconciled (your tag, the audio model, an online source) side by
 * side, marks which one won, and explains in plain English when they disagreed.
 * Hidden for the boring case where everything agreed (the diff row already says
 * it) — see `hasInterestingProvenance`.
 */
function GenreSourcesSection({
  existing,
  ml,
}: {
  existing: ExistingTags;
  ml: MLResult;
}) {
  const prov = genreProvenance(ml, existing);
  if (!hasInterestingProvenance(prov)) return null;
  const p = prov as GenreProvenance;
  const reason = reviewReason(ml, existing);

  // After the user approves a reviewed conflict the source is "approved" with
  // no single winning signal — highlight whichever row matches the value they
  // vouched for so the green check still points somewhere meaningful.
  const userApproved = p.source === "approved";
  const isEffective = (v: string | null) =>
    !!v && !!p.effective && v.toLowerCase() === p.effective.toLowerCase();
  const winnerIsTag = p.source === "tag" || (userApproved && isEffective(p.tag));
  const winnerIsAudio =
    p.source === "ml" || p.source === "ml_override" || (userApproved && isEffective(p.audio));
  const winnerIsWeb =
    p.source === "web" || p.source === "web_override" || (userApproved && isEffective(p.web));

  return (
    <Section
      title="Genre sources"
      subtitle={userApproved ? "you approved this" : p.conflict ? "they disagree" : "for reference"}
    >
      {userApproved && (
        <div className="mb-2 flex gap-2 text-xs rounded-md px-2 py-1.5 bg-accent-green/10 text-accent-green">
          <CheckCircle2 className="w-3.5 h-3.5 flex-none mt-0.5" />
          <div>You approved “{p.effective}” — cleared from review.</div>
        </div>
      )}
      {reason && (
        <div
          className={`mb-2 flex gap-2 text-xs rounded-md px-2 py-1.5 ${
            p.severity === "override"
              ? "bg-accent-yellow/10 text-accent-yellow"
              : "bg-white/5 text-white/70"
          }`}
        >
          <AlertTriangle className="w-3.5 h-3.5 flex-none mt-0.5" />
          <div>{reason}</div>
        </div>
      )}
      <SourceRow
        // An XML-imported genre still sits at the tag tier, but it came from
        // the user's Rekordbox collection rather than the file — say so.
        label={existing.genre_origin === "rekordbox" ? "Your tag (Rekordbox)" : "Your tag"}
        value={p.tag}
        won={winnerIsTag}
      />
      <SourceRow label="Audio model" value={p.audio} won={winnerIsAudio} />
      {p.web && (
        <SourceRow
          label="Online"
          value={p.web}
          won={winnerIsWeb}
          hint={p.webGrounded ? "verified" : "single source"}
          hintTitle={
            p.webGrounded
              ? "Read off a store page that names this exact track, quoted from the page itself."
              : "One catalog said so, and we couldn't confirm it elsewhere — weight it accordingly."
          }
        />
      )}
    </Section>
  );
}

function SourceRow({
  label,
  value,
  won,
  hint,
  hintTitle,
}: {
  label: string;
  value: string | null;
  won: boolean;
  hint?: string;
  hintTitle?: string;
}) {
  return (
    <div className="flex items-center gap-2 py-1">
      <div className="w-20 text-xs text-white/50 flex-none">{label}</div>
      <div className="flex-1 min-w-0 flex items-center gap-2">
        {value ? (
          <TagBadge color={won ? "purple" : "neutral"}>{value}</TagBadge>
        ) : (
          <span className="text-xs text-white/30 italic">—</span>
        )}
        {hint && (
          <span className="text-[10px] text-white/40" title={hintTitle}>
            {hint}
          </span>
        )}
        {won && (
          <span title="Used as the genre" className="flex-none ml-auto">
            <CheckCircle2 className="w-3.5 h-3.5 text-accent-green" />
          </span>
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
