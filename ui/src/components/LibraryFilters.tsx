/**
 * Filter chips for the LibraryBrowser toolbar.
 *
 * Each filter is OR within itself, AND across filters: picking Genre="House"
 * + Energy=[3,4] shows tracks where genre is House AND energy is 3 or 4.
 *
 * Filters are kept in local state in LibraryBrowser — they're view-specific
 * (you don't want them persisting across reloads) and lightweight.
 */

import { useMemo } from "react";
import { Filter, X } from "lucide-react";

import type { TrackAnalysis } from "../types";

export interface LibraryFilters {
  genres: Set<string>;          // OR — match any
  energies: Set<number>;        // OR
  moods: Set<string>;           // OR — Dark | Neutral | Bright
  vocals: Set<string>;          // OR — Instrumental | Light Vocal | Vocal
}

export function emptyFilters(): LibraryFilters {
  return {
    genres: new Set(),
    energies: new Set(),
    moods: new Set(),
    vocals: new Set(),
  };
}

export function isEmpty(f: LibraryFilters): boolean {
  return f.genres.size === 0 && f.energies.size === 0 && f.moods.size === 0 && f.vocals.size === 0;
}

export function applyFilters(tracks: TrackAnalysis[], f: LibraryFilters): TrackAnalysis[] {
  if (isEmpty(f)) return tracks;
  return tracks.filter((t) => {
    const ml = t.ml_analysis;
    if (f.genres.size > 0) {
      const g = ml?.ml_genre ?? "";
      if (!f.genres.has(g)) return false;
    }
    if (f.energies.size > 0) {
      const e = ml?.ml_energy ?? -1;
      if (!f.energies.has(e)) return false;
    }
    if (f.moods.size > 0) {
      const m = ml?.ml_mood ?? "";
      if (!f.moods.has(m)) return false;
    }
    if (f.vocals.size > 0) {
      const v = ml?.ml_vocal ?? "";
      if (!f.vocals.has(v)) return false;
    }
    return true;
  });
}

interface ChipsProps {
  tracks: TrackAnalysis[];
  filters: LibraryFilters;
  setFilters: (f: LibraryFilters) => void;
}

/** Compact horizontal chips with popovers. Used inline in the toolbar. */
export function FilterChips({ tracks, filters, setFilters }: ChipsProps) {
  // Pre-compute the universe of values present in this library
  const { genres, energies, moods, vocals } = useMemo(() => {
    const g = new Set<string>();
    const e = new Set<number>();
    const m = new Set<string>();
    const v = new Set<string>();
    for (const t of tracks) {
      const ml = t.ml_analysis;
      if (ml?.ml_genre) g.add(ml.ml_genre);
      if (ml?.ml_energy != null) e.add(ml.ml_energy);
      if (ml?.ml_mood) m.add(ml.ml_mood);
      if (ml?.ml_vocal) v.add(ml.ml_vocal);
    }
    return {
      genres: Array.from(g).sort(),
      energies: Array.from(e).sort((a, b) => a - b),
      moods: Array.from(m).sort(),
      vocals: Array.from(v).sort(),
    };
  }, [tracks]);

  const empty = isEmpty(filters);
  const totalActive = filters.genres.size + filters.energies.size + filters.moods.size + filters.vocals.size;

  const toggle = <K extends keyof LibraryFilters>(key: K, value: LibraryFilters[K] extends Set<infer V> ? V : never) => {
    const next = { ...filters, [key]: new Set(filters[key] as Set<unknown>) };
    const set = next[key] as Set<unknown>;
    if (set.has(value)) set.delete(value);
    else set.add(value);
    setFilters(next);
  };

  return (
    <div className="flex items-center gap-2 flex-wrap">
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
        />
      )}

      {energies.length > 0 && (
        <FilterDropdown
          label="Energy"
          activeCount={filters.energies.size}
          options={energies.map((e) => ({ value: e, label: `Level ${e}` }))}
          isActive={(v) => filters.energies.has(v as number)}
          onToggle={(v) => toggle("energies", v as number)}
        />
      )}

      {moods.length > 0 && (
        <FilterDropdown
          label="Mood"
          activeCount={filters.moods.size}
          options={moods.map((m) => ({ value: m, label: m }))}
          isActive={(v) => filters.moods.has(v as string)}
          onToggle={(v) => toggle("moods", v as string)}
        />
      )}

      {vocals.length > 0 && (
        <FilterDropdown
          label="Vocal"
          activeCount={filters.vocals.size}
          options={vocals.map((v) => ({ value: v, label: v }))}
          isActive={(v) => filters.vocals.has(v as string)}
          onToggle={(v) => toggle("vocals", v as string)}
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
}

function FilterDropdown<V extends string | number>({
  label, activeCount, options, isActive, onToggle,
}: FilterDropdownProps<V>) {
  return (
    <details className="relative">
      <summary
        className={`
          cursor-pointer select-none list-none px-2.5 py-1 rounded-full text-xs
          ${activeCount > 0
            ? "bg-accent/20 text-accent border border-accent/30"
            : "bg-white/5 text-white/70 hover:bg-white/10 border border-transparent"}
        `}
      >
        {label}{activeCount > 0 ? ` · ${activeCount}` : ""}
      </summary>
      <div className="absolute top-full left-0 mt-1 z-40 min-w-[160px] panel max-h-64 overflow-auto p-1">
        {options.map((opt) => (
          <label
            key={String(opt.value)}
            className="flex items-center gap-2 px-2 py-1.5 rounded cursor-pointer hover:bg-white/5 text-sm"
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
    </details>
  );
}
