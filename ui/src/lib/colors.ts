/**
 * The design-token hexes that must be consumed as raw values (canvas /
 * WaveSurfer / inline styles can't read Tailwind classes). Single source of
 * truth alongside the `@theme` block in styles/globals.css — if you change a
 * value there, change it here too (they're asserted equal in spirit, not
 * build-enforced).
 */

/** accent.DEFAULT — the brand purple. */
export const ACCENT = "#a855f7";

/** The 1-5 energy ramp (--color-energy-* in styles/globals.css @theme). */
export const ENERGY_RAMP = ["#10b981", "#84cc16", "#f59e0b", "#f97316", "#ef4444"] as const;
