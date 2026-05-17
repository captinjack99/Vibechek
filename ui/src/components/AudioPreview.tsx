/**
 * Audio preview — listen to a few seconds of a track without leaving Vibechek.
 *
 * Uses Tauri's asset protocol to serve the file directly to the webview's
 * <audio> element. Falls back to a clear "can't load" state if the file is
 * gone or the asset protocol isn't enabled.
 *
 * The element only loads metadata until the user clicks play — important
 * for the Library view where a track might be selected just to read tags.
 */

import { useEffect, useRef, useState } from "react";
import { convertFileSrc } from "@tauri-apps/api/core";
import { Play, Pause, AlertCircle } from "lucide-react";

export function AudioPreview({ path }: { path: string }) {
  const ref = useRef<HTMLAudioElement>(null);
  const [playing, setPlaying] = useState(false);
  const [duration, setDuration] = useState<number | null>(null);
  const [position, setPosition] = useState(0);
  const [error, setError] = useState<string | null>(null);

  // Convert Windows / Unix path → asset.localhost URL the webview can fetch
  const src = convertFileSrc(path);

  // Reset state when the selected track changes
  useEffect(() => {
    setPlaying(false);
    setPosition(0);
    setDuration(null);
    setError(null);
    if (ref.current) {
      ref.current.pause();
      ref.current.currentTime = 0;
    }
  }, [path]);

  const togglePlay = () => {
    if (!ref.current) return;
    if (ref.current.paused) {
      ref.current.play().catch((e) => setError(String(e)));
    } else {
      ref.current.pause();
    }
  };

  const seek = (e: React.MouseEvent<HTMLDivElement>) => {
    if (!ref.current || !duration) return;
    const rect = e.currentTarget.getBoundingClientRect();
    const pct = (e.clientX - rect.left) / rect.width;
    ref.current.currentTime = pct * duration;
  };

  return (
    <div className="panel-pad space-y-2">
      <audio
        ref={ref}
        src={src}
        preload="metadata"
        onLoadedMetadata={(e) => setDuration(e.currentTarget.duration)}
        onTimeUpdate={(e) => setPosition(e.currentTarget.currentTime)}
        onPlay={() => setPlaying(true)}
        onPause={() => setPlaying(false)}
        onEnded={() => setPlaying(false)}
        onError={() => setError("Could not load audio. Format may be unsupported by the webview.")}
      />

      <div className="flex items-center gap-3">
        <button
          onClick={togglePlay}
          disabled={!!error}
          className={`flex-none w-10 h-10 rounded-full flex items-center justify-center transition-colors ${
            playing
              ? "bg-accent text-white hover:bg-accent/90"
              : "bg-white/10 hover:bg-white/20 text-white"
          } disabled:opacity-30 disabled:cursor-not-allowed`}
          aria-label={playing ? "Pause" : "Play"}
        >
          {playing ? <Pause className="w-4 h-4" /> : <Play className="w-4 h-4 ml-0.5" />}
        </button>

        <div className="flex-1 min-w-0">
          {error ? (
            <div className="flex items-center gap-2 text-xs text-accent-red">
              <AlertCircle className="w-3.5 h-3.5 flex-none" />
              <span className="truncate">{error}</span>
            </div>
          ) : (
            <>
              {/* Scrub bar */}
              <div
                className="h-2 bg-white/5 rounded-full overflow-hidden cursor-pointer"
                onClick={seek}
                role="progressbar"
                aria-valuenow={duration ? (position / duration) * 100 : 0}
              >
                <div
                  className="h-full bg-accent transition-[width] duration-100"
                  style={{ width: duration ? `${(position / duration) * 100}%` : "0%" }}
                />
              </div>
              {/* Time readout */}
              <div className="flex justify-between text-[11px] font-mono text-white/40 mt-1 tabular-nums">
                <span>{formatTime(position)}</span>
                <span>{duration ? formatTime(duration) : "--:--"}</span>
              </div>
            </>
          )}
        </div>
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
