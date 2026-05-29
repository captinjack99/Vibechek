/**
 * Global audio-player store — the single source of truth for "what's playing".
 *
 * There is exactly ONE audio engine in the app (the <GlobalAudioPlayer /> bar
 * mounted at the app root). Components don't own audio elements anymore; they
 * call `play(path, title)` to hand a track to the global player. Playing a new
 * track replaces the current one, so two previews can never sound at once —
 * the bug that motivated this design (per-track WaveSurfer instances that kept
 * playing after navigation / when a new track was clicked).
 *
 * The player bar persists across tab/view changes because it lives outside the
 * view switch, so there's always a visible stop control.
 */

import { create } from "zustand";

interface PlayerState {
  /** Absolute path of the track loaded into the global player, or null. */
  path: string | null;
  /** Friendly label shown in the player bar (filename). */
  title: string | null;
  /** A token bumped every time `play` is called with the SAME path, so the
   * player can distinguish "re-play this track" from "no change". */
  playToken: number;

  /** Load a track into the global player and start it. */
  play: (path: string, title: string) => void;
  /** Stop + clear the player (closes the bar). */
  stop: () => void;
}

let token = 0;

export const usePlayerStore = create<PlayerState>((set) => ({
  path: null,
  title: null,
  playToken: 0,

  play: (path, title) => set({ path, title, playToken: ++token }),
  stop: () => set({ path: null, title: null }),
}));
