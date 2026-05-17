/**
 * Audio preview with waveform.
 *
 * Uses WaveSurfer.js to render an actual waveform from the decoded audio.
 * The audio is loaded via Tauri's asset protocol — `convertFileSrc` turns
 * `C:\foo.mp3` into an `asset.localhost` URL the webview can fetch.
 *
 * The waveform is generated client-side from the decoded buffer, which means
 * the first render of a long FLAC can take a second or two. That's a fair
 * trade-off for not needing to pre-compute peaks in the sidecar.
 *
 * On failure (corrupt file, format not supported by the webview), we show
 * a clean error state with the underlying message.
 */

import { useEffect, useRef, useState } from "react";
import { convertFileSrc } from "@tauri-apps/api/core";
import WaveSurfer from "wavesurfer.js";
import { Play, Pause, AlertCircle, Loader2 } from "lucide-react";

export function AudioPreview({ path }: { path: string }) {
  const containerRef = useRef<HTMLDivElement>(null);
  const wsRef = useRef<WaveSurfer | null>(null);

  const [ready, setReady] = useState(false);
  const [playing, setPlaying] = useState(false);
  const [duration, setDuration] = useState(0);
  const [position, setPosition] = useState(0);
  const [error, setError] = useState<string | null>(null);

  // Create / re-create WaveSurfer when the path changes.
  useEffect(() => {
    if (!containerRef.current) return;

    setReady(false);
    setPlaying(false);
    setError(null);
    setDuration(0);
    setPosition(0);

    const ws = WaveSurfer.create({
      container: containerRef.current,
      url: convertFileSrc(path),
      waveColor: "rgba(255,255,255,0.25)",
      progressColor: "#a855f7",
      cursorColor: "rgba(255,255,255,0.4)",
      barWidth: 2,
      barGap: 1,
      barRadius: 1,
      height: 60,
      normalize: true,
      backend: "WebAudio",
    });
    wsRef.current = ws;

    ws.on("ready", () => {
      setReady(true);
      setDuration(ws.getDuration());
    });
    ws.on("timeupdate", (t) => setPosition(t));
    ws.on("play", () => setPlaying(true));
    ws.on("pause", () => setPlaying(false));
    ws.on("finish", () => setPlaying(false));
    ws.on("error", (e: Error) => {
      setError(e.message || "Could not load audio");
    });

    return () => {
      ws.destroy();
      wsRef.current = null;
    };
  }, [path]);

  const togglePlay = () => {
    wsRef.current?.playPause();
  };

  return (
    <div className="panel-pad space-y-3">
      <div className="flex items-center gap-3">
        <button
          onClick={togglePlay}
          disabled={!ready || !!error}
          className={`flex-none w-10 h-10 rounded-full flex items-center justify-center transition-colors ${
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

        <div className="flex-1 text-[11px] font-mono text-white/50 tabular-nums">
          {formatTime(position)} / {ready ? formatTime(duration) : "--:--"}
        </div>
      </div>

      {/* Waveform container — WaveSurfer renders into this div. The min-height
          reserves room for the loading overlay before the waveform paints. */}
      <div className="rounded overflow-hidden bg-surface-300/40 relative min-h-[60px]">
        <div ref={containerRef} className={error ? "hidden" : ""} />

        {/* Decoding overlay — the waveform area sits empty for 1-3s on long
            FLACs while WaveSurfer pulls peaks. Tell the user something is
            happening so they don't think the click did nothing. */}
        {!ready && !error && (
          <div
            className="absolute inset-0 flex items-center justify-center gap-2 text-[11px] text-white/50 animate-pulse pointer-events-none"
            aria-live="polite"
          >
            <Loader2 className="w-3.5 h-3.5 animate-spin" />
            Loading waveform...
          </div>
        )}

        {error && (
          <div className="px-3 py-6 flex items-center justify-center gap-2 text-xs text-accent-red">
            <AlertCircle className="w-4 h-4 flex-none" />
            <span className="truncate" title={error}>{error}</span>
          </div>
        )}
      </div>
    </div>
  );
}

function formatTime(seconds: number): string {
  if (!isFinite(seconds)) return "--:--";
  const mm = Math.floor(seconds / 60);
  const ss = Math.floor(seconds % 60);
  return `${mm}:${ss.toString().padStart(2, "0")}`;
}
