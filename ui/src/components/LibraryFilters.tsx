/**
 * Filter chips for the LibraryBrowser toolbar.
 *
 * Each filter is OR within itself, AND across filters: picking Genre="House"
 * + Energy=[3,4] shows tracks where genre is House AND energy is 3 or 4.
 *
 * Filters are kept in local state in LibraryBrowser — they're view-specific
 * (you don't want them persisting across reloads) and lightweight.
 *
 * Popover behaviour: at most one chip is open at a time. The parent
 * (`FilterChips`) holds the `openChip` state and a single document-level
 * listener handles outside-click + Escape — much lighter than one listener
 * per chip and avoids stale-closure foot-guns.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import { Filter, X } from "lucide-react";
import { create } from "zustand";

import type { TrackAnalysis } from "../types";

export interface LibraryFilters {
  genres: Set<string>;          // OR — match any
  energies: Set<number>;        // OR
  moods: Set<string>;           // OR — Dark | Neutral | Bright
  vocals: Set<string>;          // OR — Instrumental | Light Vocal | Vocal
  // Direction is the DJ's "where is this track taking the room?" axis —
  // Up (build), Steady (cruise), Down (cooldown). Lets you instantly carve
  // out the build set vs. the comedown set when prepping a setlist.
  directions: Set<string>;      // OR — Up | Steady | Down
  // Camelot wheel keys (e.g. "8A", "12B"). Selecting "8A" filters to tracks
  // whose ml_key is exactly 8A; the chip ALSO highlights compatible keys
  // (per `camelotMode`) so the DJ can see what to load next. The filter
  // itself is OR across the selected set.
  camelot: Set<string>;
  // Which compatibility rule the visual highlight uses. Doesn't affect the
  // filter match — only the dim-outline neighbour styling on the chips.
  camelotMode: CamelotMode;
}

/** Mirrors `vibechek.keys.COMPATIBLE_MODES`. */
export type CamelotMode = "energy" | "harmonic" | "energy-boost" | "strict";

export function emptyFilters(): LibraryFilters {
  return {
    genres: new Set(),
    energies: new Set(),
    moods: new Set(),
    vocals: new Set(),
    directions: new Set(),
    camelot: new Set(),
    camelotMode: "energy",
  };
}

export function isEmpty(f: LibraryFilters): boolean {
  return (
    f.genres.size === 0 &&
    f.energies.size === 0 &&
    f.moods.size === 0 &&
    f.vocals.size === 0 &&
    f.directions.size === 0 &&
    f.camelot.size === 0
  );
}

// ---------------------------------------------------------------------------
// Camelot wheel helpers
// ---------------------------------------------------------------------------
//
// JS port of `vibechek.keys.compatible_camelot` so the UI doesn't have to
// round-trip to the sidecar for every render. Kept self-contained (no
// external lookup tables) — the Camelot wheel is small and the rules are
// fixed. If the Python helper grows new modes, update this in lockstep.

const CAMELOT_RE = /^([1-9]|1[0-2])([AB])$/i;

function parseCamelot(value: string | null | undefined): [number, "A" | "B"] | null {
  if (!value) return null;
  const m = CAMELOT_RE.exec(String(value).trim());
  if (!m) return null;
  return [parseInt(m[1], 10), m[2].toUpperCase() as "A" | "B"];
}

function wrap(number: number, letter: "A" | "B"): string {
  // Camelot wraps 12 → 1 and 0 → 12. Negative inputs wrap correctly too.
  const n = ((number - 1 + 12 * 100) % 12) + 1;
  return `${n}${letter}`;
}

const flip = (l: "A" | "B"): "A" | "B" => (l === "A" ? "B" : "A");

/** Compatible keys for `key` under `mode`. Returns [] for invalid input. */
export function compatibleCamelot(
  key: string | null | undefined,
  mode: CamelotMode = "energy",
): string[] {
  const parsed = parseCamelot(key);
  if (!parsed) return [];
  const [n, letter] = parsed;
  const out: string[] = [];
  const add = (s: string) => { if (!out.includes(s)) out.push(s); };

  if (mode === "strict") {
    add(wrap(n, flip(letter)));
    return out;
  }
  if (mode === "harmonic") {
    add(wrap(n - 1, letter));
    add(wrap(n + 1, letter));
    return out;
  }
  // energy / energy-boost
  add(wrap(n - 1, letter));
  add(wrap(n + 1, letter));
  add(wrap(n, flip(letter)));
  if (mode === "energy-boost") {
    add(wrap(n + 7, letter)); // +1 semitone modulation
  }
  return out;
}

/** Symmetric compatibility — used by the TrackDetails pill click handler. */
export function isCompatibleWith(
  a: string | null | undefined,
  b: string | null | undefined,
  mode: CamelotMode = "energy",
): boolean {
  const ap = parseCamelot(a);
  const bp = parseCamelot(b);
  if (!ap || !bp) return false;
  const aw = wrap(ap[0], ap[1]);
  const bw = wrap(bp[0], bp[1]);
  if (aw === bw) return true;
  return compatibleCamelot(aw, mode).includes(bw);
}

/**
 * Whether a track passes an individual filter dimension. Tracks with no ML
 * data (or a `null` value) never match an active dimension — using a `-1`
 * sentinel for energy turned out to be a leaky abstraction (it appeared as a
 * legal value in some downstream code). Treat missing as "doesn't match".
 */
export function applyFilters(tracks: TrackAnalysis[], f: LibraryFilters): TrackAnalysis[] {
  if (isEmpty(f)) return tracks;
  return tracks.filter((t) => {
    const ml = t.ml_analysis;
    if (f.genres.size > 0) {
      const g = ml?.ml_genre;
      if (g == null || !f.genres.has(g)) return false;
    }
    if (f.energies.size > 0) {
      const e = ml?.ml_energy;
      if (e == null || !f.energies.has(e)) return false;
    }
    if (f.moods.size > 0) {
      const m = ml?.ml_mood;
      if (m == null || !f.moods.has(m)) return false;
    }
    if (f.vocals.size > 0) {
      const v = ml?.ml_vocal;
      if (v == null || !f.vocals.has(v)) return false;
    }
    if (f.directions.size > 0) {
      const d = ml?.ml_direction;
      if (d == null || !f.directions.has(d)) return false;
    }
    if (f.camelot.size > 0) {
      const k = ml?.ml_key;
      // Normalize the track's key through parseCamelot so dirty inputs
      // ("8a", " 12B ") match cleanly. Tracks with no key never match an
      // active camelot filter — same policy as the other dimensions.
      const parsed = parseCamelot(k);
      if (!parsed) return false;
      const normalized = `${parsed[0]}${parsed[1]}`;
      if (!f.camelot.has(normalized)) return false;
    }
    return true;
  });
}

// ---------------------------------------------------------------------------
// Zustand store — filters were local state in LibraryBrowser, which meant
// switching to another tab and back wiped the user's filter selection. The
// store lifts them above the component tree so a tab round-trip preserves
// state. A library SWITCH, however, clears them — LibraryBrowser's
// handleOpenRecent/handleOpenFolder call clearFilters() so one library's
// selections don't silently narrow the next.
// ---------------------------------------------------------------------------

interface LibraryFiltersState {
  filters: LibraryFilters;
  setFilters: (f: LibraryFilters) => void;
  clearFilters: () => void;
}

export const useLibraryFiltersStore = create<LibraryFiltersState>((set) => ({
  filters: emptyFilters(),
  setFilters: (filters) => set({ filters }),
  clearFilters: () => set({ filters: emptyFilters() }),
}));

/** Identifies which chip's dropdown is open. */
type ChipKey = "genre" | "energy" | "mood" | "vocal" | "direction" | "camelot";

interface ChipsProps {
  tracks: TrackAnalysis[];
  filters: LibraryFilters;
  setFilters: (f: LibraryFilters) => void;
}

/** Compact horizontal chips with popovers. Used inline in the toolbar. */
export function FilterChips({ tracks, filters, setFilters }: ChipsProps) {
  // Pre-compute the universe of values present in this library
  const { genres, energies, moods, vocals, directions, camelotPresent } = useMemo(() => {
    const g = new Set<string>();
    const e = new Set<number>();
    const m = new Set<string>();
    const v = new Set<string>();
    const d = new Set<string>();
    // Set of normalized camelot wheels actually present in the library —
    // used to dim chips that have no matching tracks ("9A" vs all 24 keys).
    const c = new Set<string>();
    for (const t of tracks) {
      const ml = t.ml_analysis;
      if (ml?.ml_genre) g.add(ml.ml_genre);
      if (ml?.ml_energy != null) e.add(ml.ml_energy);
      if (ml?.ml_mood) m.add(ml.ml_mood);
      if (ml?.ml_vocal) v.add(ml.ml_vocal);
      if (ml?.ml_direction) d.add(ml.ml_direction);
      const kp = parseCamelot(ml?.ml_key);
      if (kp) c.add(`${kp[0]}${kp[1]}`);
    }
    // Direction has a meaningful natural order (build → cruise → cooldown)
    // that alphabetical "Down/Steady/Up" loses. Sort by the canonical order
    // and tack any unrecognised value on the end.
    const directionOrder = ["Up", "Steady", "Down"];
    return {
      genres: Array.from(g).sort(),
      energies: Array.from(e).sort((a, b) => a - b),
      moods: Array.from(m).sort(),
      vocals: Array.from(v).sort(),
      directions: Array.from(d).sort((a, b) => {
        const ai = directionOrder.indexOf(a);
        const bi = directionOrder.indexOf(b);
        if (ai === -1 && bi === -1) return a.localeCompare(b);
        if (ai === -1) return 1;
        if (bi === -1) return -1;
        return ai - bi;
      }),
      camelotPresent: c,
    };
  }, [tracks]);

  const empty = isEmpty(filters);
  const totalActive =
    filters.genres.size +
    filters.energies.size +
    filters.moods.size +
    filters.vocals.size +
    filters.directions.size +
    filters.camelot.size;

  const [openChip, setOpenChip] = useState<ChipKey | null>(null);
  const containerRef = useRef<HTMLDivElement>(null);

  // Outside-click and Escape close the open dropdown. One listener, one
  // source of truth — chips below just receive isOpen + onOpen.
  useEffect(() => {
    if (!openChip) return;
    const onMouseDown = (e: MouseEvent) => {
      const target = e.target as Node | null;
      if (!target) return;
      if (containerRef.current && !containerRef.current.contains(target)) {
        setOpenChip(null);
      }
    };
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpenChip(null);
    };
    document.addEventListener("mousedown", onMouseDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("mousedown", onMouseDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [openChip]);

  const toggle = <K extends keyof LibraryFilters>(key: K, value: LibraryFilters[K] extends Set<infer V> ? V : never) => {
    const next = { ...filters, [key]: new Set(filters[key] as Set<unknown>) };
    const set = next[key] as Set<unknown>;
    if (set.has(value)) set.delete(value);
    else set.add(value);
    setFilters(next);
  };

  return (
    <div ref={containerRef} className="flex items-center gap-2 flex-wrap">
      <div className="text-white/40 flex items-center gap-1.5 text-xs">
        <Filter className="w-3.5 h-3.5" />
        Filter:
      </div>

      {genres.length > 0 && (
        <FilterDropdown
          label="Genre"
          activeCount={filters.genres.size}
          options={genres.map((g) => ({ value: g, label: g }))}
          isActive={(v) => filters.genres.has(v as string)}
          onToggle={(v) => toggle("genres", v as string)}
          isOpen={openChip === "genre"}
          onOpen={() => setOpenChip(openChip === "genre" ? null : "genre")}
        />
      )}

      {energies.length > 0 && (
        <FilterDropdown
          label="Energy"
          activeCount={filters.energies.size}
          options={energies.map((e) => ({ value: e, label: `Level ${e}` }))}
          isActive={(v) => filters.energies.has(v as number)}
          onToggle={(v) => toggle("energies", v as number)}
          isOpen={openChip === "energy"}
          onOpen={() => setOpenChip(openChip === "energy" ? null : "energy")}
        />
      )}

      {moods.length > 0 && (
        <FilterDropdown
          label="Mood"
          activeCount={filters.moods.size}
          options={moods.map((m) => ({ value: m, label: m }))}
          isActive={(v) => filters.moods.has(v as string)}
          onToggle={(v) => toggle("moods", v as string)}
          isOpen={openChip === "mood"}
          onOpen={() => setOpenChip(openChip === "mood" ? null : "mood")}
        />
      )}

      {vocals.length > 0 && (
        <FilterDropdown
          label="Vocal"
          activeCount={filters.vocals.size}
          options={vocals.map((v) => ({ value: v, label: v }))}
          isActive={(v) => filters.vocals.has(v as string)}
          onToggle={(v) => toggle("vocals", v as string)}
          isOpen={openChip === "vocal"}
          onOpen={() => setOpenChip(openChip === "vocal" ? null : "vocal")}
        />
      )}

      {directions.length > 0 && (
        <FilterDropdown
          label="Direction"
          activeCount={filters.directions.size}
          options={directions.map((d) => ({ value: d, label: d }))}
          isActive={(v) => filters.directions.has(v as string)}
          onToggle={(v) => toggle("directions", v as string)}
          isOpen={openChip === "direction"}
          onOpen={() => setOpenChip(openChip === "direction" ? null : "direction")}
        />
      )}

      {camelotPresent.size > 0 && (
        <CamelotChip
          filters={filters}
          setFilters={setFilters}
          camelotPresent={camelotPresent}
          isOpen={openChip === "camelot"}
          onOpen={() => setOpenChip(openChip === "camelot" ? null : "camelot")}
        />
      )}

      {!empty && (
        <button
          onClick={() => setFilters(emptyFilters())}
          className="text-xs text-white/40 hover:text-white flex items-center gap-1"
          title="Clear all filters"
        >
          <X className="w-3 h-3" />
          Clear ({totalActive})
        </button>
      )}
    </div>
  );
}

interface FilterDropdownProps<V> {
  label: string;
  activeCount: number;
  options: { value: V; label: string }[];
  isActive: (v: V) => boolean;
  onToggle: (v: V) => void;
  isOpen: boolean;
  onOpen: () => void;
}

interface CamelotChipProps {
  filters: LibraryFilters;
  setFilters: (f: LibraryFilters) => void;
  camelotPresent: Set<string>;
  isOpen: boolean;
  onOpen: () => void;
}

/**
 * Bespoke chip + popover for the Camelot wheel. Different shape from the
 * generic FilterDropdown: a 2-row grid (12 minors + 12 majors) instead of
 * a vertical checklist, plus a mode toggle and neighbour-highlight rules.
 */
function CamelotChip({
  filters, setFilters, camelotPresent, isOpen, onOpen,
}: CamelotChipProps) {
  const activeCount = filters.camelot.size;
  // The dim "compatible" highlight is the union of compatible-key sets for
  // every selected key. So picking 8A + 1A both light up *their* neighbours.
  const compatibleSet = useMemo(() => {
    const acc = new Set<string>();
    for (const k of filters.camelot) {
      for (const c of compatibleCamelot(k, filters.camelotMode)) acc.add(c);
    }
    return acc;
  }, [filters.camelot, filters.camelotMode]);

  const toggleKey = (key: string) => {
    const next = { ...filters, camelot: new Set(filters.camelot) };
    if (next.camelot.has(key)) next.camelot.delete(key);
    else next.camelot.add(key);
    setFilters(next);
  };

  const setMode = (mode: CamelotMode) => {
    setFilters({ ...filters, camelotMode: mode });
  };

  // Stable grid; built once.
  const minors: string[] = [];
  const majors: string[] = [];
  for (let i = 1; i <= 12; i++) {
    minors.push(`${i}A`);
    majors.push(`${i}B`);
  }

  return (
    <div className="relative">
      <button
        type="button"
        onClick={onOpen}
        aria-haspopup="true"
        aria-expanded={isOpen}
        className={`
          cursor-pointer select-none px-2.5 py-1 rounded-full text-xs
          ${activeCount > 0
            ? "bg-accent/20 text-accent border border-accent/30"
            : "bg-white/5 text-white/70 hover:bg-white/10 border border-transparent"}
        `}
      >
        Key{activeCount > 0 ? ` · ${activeCount}` : ""}
      </button>
      {isOpen && (
        <div className="absolute top-full left-0 mt-1 z-40 panel p-3 space-y-2 min-w-[420px]">
          <div className="flex items-center gap-1 text-[11px]">
            <span className="text-white/40 mr-1">Mode:</span>
            {(["energy", "harmonic", "energy-boost", "strict"] as CamelotMode[]).map((m) => (
              <button
                key={m}
                type="button"
                onClick={() => setMode(m)}
                className={`px-2 py-0.5 rounded-full ${
                  filters.camelotMode === m
                    ? "bg-accent/20 text-accent border border-accent/30"
                    : "bg-white/5 text-white/60 hover:bg-white/10 border border-transparent"
                }`}
              >
                {m}
              </button>
            ))}
          </div>
          <div className="grid grid-cols-12 gap-1">
            {minors.map((k) => (
              <CamelotKeyButton
                key={k}
                label={k}
                selected={filters.camelot.has(k)}
                compatible={compatibleSet.has(k)}
                present={camelotPresent.has(k)}
                onClick={() => toggleKey(k)}
              />
            ))}
          </div>
          <div className="grid grid-cols-12 gap-1">
            {majors.map((k) => (
              <CamelotKeyButton
                key={k}
                label={k}
                selected={filters.camelot.has(k)}
                compatible={compatibleSet.has(k)}
                present={camelotPresent.has(k)}
                onClick={() => toggleKey(k)}
              />
            ))}
          </div>
          {activeCount > 0 && (
            <div className="text-[11px] text-white/40 pt-1">
              Click a key to toggle. Compatible keys are outlined.
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function CamelotKeyButton({
  label, selected, compatible, present, onClick,
}: {
  label: string;
  selected: boolean;
  compatible: boolean;
  present: boolean;
  onClick: () => void;
}) {
  // Visual hierarchy, strongest → faintest:
  //   selected         → solid accent
  //   compatible       → dim outline (DJ's "next track" hint)
  //   present in lib   → normal white-on-grey
  //   absent           → muted, still clickable (user may want to plan ahead)
  const cls = selected
    ? "bg-accent text-black border border-accent"
    : compatible
      ? "bg-white/5 text-white/90 border border-accent/40"
      : present
        ? "bg-white/5 text-white/80 border border-transparent hover:bg-white/10"
        : "bg-white/[0.02] text-white/30 border border-transparent hover:bg-white/5";
  return (
    <button
      type="button"
      onClick={onClick}
      className={`text-[11px] font-mono py-1 rounded-sm ${cls}`}
      title={label}
    >
      {label}
    </button>
  );
}

function FilterDropdown<V extends string | number>({
  label, activeCount, options, isActive, onToggle, isOpen, onOpen,
}: FilterDropdownProps<V>) {
  return (
    <div className="relative">
      <button
        type="button"
        onClick={onOpen}
        aria-haspopup="true"
        aria-expanded={isOpen}
        className={`
          cursor-pointer select-none px-2.5 py-1 rounded-full text-xs
          ${activeCount > 0
            ? "bg-accent/20 text-accent border border-accent/30"
            : "bg-white/5 text-white/70 hover:bg-white/10 border border-transparent"}
        `}
      >
        {label}{activeCount > 0 ? ` · ${activeCount}` : ""}
      </button>
      {isOpen && (
        <div className="absolute top-full left-0 mt-1 z-40 min-w-[160px] panel max-h-64 overflow-auto p-1">
          {options.map((opt) => (
            <label
              key={String(opt.value)}
              className="flex items-center gap-2 px-2 py-1.5 rounded-sm cursor-pointer hover:bg-white/5 text-sm"
            >
              <input
                type="checkbox"
                checked={isActive(opt.value)}
                onChange={() => onToggle(opt.value)}
                className="accent-accent"
              />
              <span className="text-white/90">{opt.label}</span>
            </label>
          ))}
        </div>
      )}
    </div>
  );
}
