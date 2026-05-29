/**
 * The one and only audio player in the app.
 *
 * Mounted once at the app root (outside the view switch), so it persists across
 * every tab/menu and there is always a visible stop control. Driven by
 * `usePlayerStore`: when `path` changes we `wavesurfer.load()` the new URL into
 * the SAME instance, which stops whatever was playing — so two previews can
 * never overlap (the bug that motivated this rewrite).
 *
 * Replaces the old per-track <AudioPreview> that created a fresh WaveSurfer
 * inside TrackDetails: those leaked playback when you navigated away or clicked
 * a different track.
 */

import { useEffect, useRef, useState } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { convertFileSrc } from "@tauri-apps/api/core";
import WaveSurfer from "wavesurfer.js";
import { Play, Pause, X, AlertCircle, Loader2, Music } from "lucide-react";

import { usePlayerStore } from "../stores";

function formatTime(seconds: number): string {
  if (!isFinite(seconds)) return "--:--";
  const mm = Math.floor(seconds / 60);
  const ss = Math.floor(seconds % 60);
  return `${mm}:${ss.toString().padStart(2, "0")}`;
}

export function GlobalAudioPlayer() {
  const path = usePlayerStore((s) => s.path);
  const title = usePlayerStore((s) => s.title);
  const playToken = usePlayerStore((s) => s.playToken);
  const stop = usePlayerStore((s) => s.stop);

  const containerRef = useRef<HTMLDivElement>(null);
  const wsRef = useRef<WaveSurfer | null>(null);

  const [ready, setReady] = useState(false);
  const [playing, setPlaying] = useState(false);
  const [duration, setDuration] = useState(0);
  const [position, setPosition] = useState(0);
  const [error, setError] = useState<string | null>(null);

  // Create the single WaveSurfer instance once, when the bar first mounts
  // (i.e. when something is first played). We keep it alive for the app's
  // lifetime and just .load() new URLs into it.
  const visible = path !== null;

  useEffect(() => {
    // Only build the instance once the container exists (bar is visible).
    if (!visible || !containerRef.current) return;
    if (wsRef.current) return; // already created

    const ws = WaveSurfer.create({
      container: containerRef.current,
      waveColor: "rgba(255,255,255,0.25)",
      progressColor: "#a855f7",
      cursorColor: "rgba(255,255,255,0.4)",
      barWidth: 2,
      barGap: 1,
      barRadius: 1,
      height: 36,
      normalize: true,
      backend: "WebAudio",
    });
    wsRef.current = ws;

    ws.on("ready", () => {
      setReady(true);
      setDuration(ws.getDuration());
      // Always start a freshly-loaded track from 0:00. Reusing one WaveSurfer
      // instance across tracks means the previous track's currentTime can
      // carry over into the new source — so a new preview would resume at the
      // old elapsed time (and, if the new track is shorter, seek past its end
      // and wedge). Explicitly rewind before autoplay.
      try { ws.seekTo(0); } catch { /* pre-decode edge */ }
      setPosition(0);
      void ws.play();
    });
    ws.on("timeupdate", (t: number) => setPosition(t));
    ws.on("play", () => setPlaying(true));
    ws.on("pause", () => setPlaying(false));
    ws.on("finish", () => setPlaying(false));
    ws.on("error", (e: Error) => setError(e?.message || "Could not load audio"));

    return () => {
      // App teardown only — the bar normally stays mounted.
      ws.destroy();
      wsRef.current = null;
    };
  }, [visible]);

  // Load a new source whenever the requested path (or replay token) changes.
  // .load() on the existing instance stops current playback first, guaranteeing
  // a single active source.
  useEffect(() => {
    const ws = wsRef.current;
    if (!ws || !path) return;
    setReady(false);
    setPlaying(false);
    setError(null);
    setDuration(0);
    setPosition(0);
    // Stop + rewind the currently-playing source BEFORE loading the next one,
    // so the old position can't bleed into the new track. `ready` also seeks
    // to 0 (belt + suspenders against WaveSurfer carrying currentTime over).
    try { ws.stop(); } catch { /* nothing loaded yet */ }
    try {
      void ws.load(convertFileSrc(path));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
    // playToken is in the deps so clicking the same track again restarts it.
  }, [path, playToken]);

  // Stop playback + tear down audio when the bar is closed. We fully destroy
  // the instance so a stale WebAudio source can't linger; it's recreated on
  // the next play().
  const handleClose = () => {
    const ws = wsRef.current;
    if (ws) {
      try { ws.stop(); } catch { /* ignore */ }
      try { ws.destroy(); } catch { /* ignore */ }
      wsRef.current = null;
    }
    setReady(false);
    setPlaying(false);
    setError(null);
    stop();
  };

  const togglePlay = () => {
    wsRef.current?.playPause();
  };

  return (
    <AnimatePresence>
      {visible && (
        <motion.div
          initial={{ y: 60, opacity: 0 }}
          animate={{ y: 0, opacity: 1 }}
          exit={{ y: 60, opacity: 0 }}
          transition={{ duration: 0.18 }}
          className="border-t border-white/10 bg-surface-100 px-4 py-2.5 flex items-center gap-4"
        >
          <button
            onClick={togglePlay}
            disabled={!ready || !!error}
            className={`flex-none w-9 h-9 rounded-full flex items-center justify-center transition-colors ${
              playing
                ? "bg-accent text-white hover:bg-accent/90"
                : "bg-white/10 hover:bg-white/20 text-white"
            } disabled:opacity-30 disabled:cursor-not-allowed`}
            aria-label={playing ? "Pause" : "Play"}
            title={playing ? "Pause" : "Play"}
          >
            {!ready && !error ? (
              <Loader2 className="w-4 h-4 animate-spin" />
            ) : playing ? (
              <Pause className="w-4 h-4" />
            ) : (
              <Play className="w-4 h-4 ml-0.5" />
            )}
          </button>

          <div className="flex-none min-w-0 max-w-[220px]">
            <div className="flex items-center gap-1.5 text-xs text-white/80 truncate">
              <Music className="w-3.5 h-3.5 flex-none text-accent" />
              <span className="truncate" title={title ?? undefined}>{title ?? "—"}</span>
            </div>
            <div className="text-[10px] font-mono text-white/40 tabular-nums">
              {formatTime(position)} / {ready ? formatTime(duration) : "--:--"}
            </div>
          </div>

          {/* Waveform — flexes to fill. Hidden on error so the message shows. */}
          <div className="flex-1 min-w-0 relative">
            <div ref={containerRef} className={error ? "hidden" : ""} />
            {!ready && !error && (
              <div className="absolute inset-0 flex items-center gap-2 text-[11px] text-white/40">
                <Loader2 className="w-3 h-3 animate-spin" /> Loading…
              </div>
            )}
            {error && (
              <div className="flex items-center gap-2 text-xs text-accent-red max-h-12 overflow-y-auto">
                <AlertCircle className="w-4 h-4 flex-none" />
                <span className="break-words whitespace-pre-wrap font-mono leading-snug">{error}</span>
              </div>
            )}
          </div>

          <button
            onClick={handleClose}
            className="flex-none text-white/40 hover:text-white p-1"
            title="Stop & close player"
            aria-label="Stop and close player"
          >
            <X className="w-4 h-4" />
          </button>
        </motion.div>
      )}
    </AnimatePresence>
  );
}
