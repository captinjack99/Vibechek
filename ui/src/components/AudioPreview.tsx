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
  //
  // The `aborted` flag closes over the *current* effect run only — if the
  // user clicks rapidly through tracks (or switches selection mid-decode),
  // the previous run's async callbacks should be ignored rather than
  // race-clobbering the state for the new track. WaveSurfer's destroy()
  // call also stops any in-flight fetch, but its event handlers can still
  // fire one tick later; the flag is the second line of defence.
  useEffect(() => {
    if (!containerRef.current) return;

    let aborted = false;

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
      if (aborted) return;
      setReady(true);
      setDuration(ws.getDuration());
    });
    ws.on("timeupdate", (t) => {
      if (aborted) return;
      setPosition(t);
    });
    ws.on("play", () => {
      if (aborted) return;
      setPlaying(true);
    });
    ws.on("pause", () => {
      if (aborted) return;
      setPlaying(false);
    });
    ws.on("finish", () => {
      if (aborted) return;
      setPlaying(false);
    });
    ws.on("error", (e: Error) => {
      if (aborted) return;
      setError(e.message || "Could not load audio");
    });

    return () => {
      aborted = true;
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
          // WaveSurfer / asset-protocol errors are often long (full file
          // paths, decode-failure detail). The previous `truncate` class
          // hid everything past the first ~30 chars — useless when the user
          // is trying to figure out which codec is unsupported. We allow
          // wrap, preserve internal whitespace (some error strings have
          // newlines), and cap with a scrollable max-height so a runaway
          // multi-line stack trace doesn't blow the side panel out.
          <div className="px-3 py-3 flex items-start gap-2 text-xs text-accent-red max-h-40 overflow-y-auto">
            <AlertCircle className="w-4 h-4 flex-none mt-0.5" />
            <span className="break-words whitespace-pre-wrap font-mono leading-snug">
              {error}
            </span>
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
