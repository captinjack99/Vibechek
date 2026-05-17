import { clsx } from "clsx";

type BadgeColor = "purple" | "cyan" | "green" | "yellow" | "red" | "neutral";

const COLOR_CLASSES: Record<BadgeColor, string> = {
  purple: "bg-accent/15 text-accent",
  cyan: "bg-accent-cyan/15 text-accent-cyan",
  green: "bg-accent-green/15 text-accent-green",
  yellow: "bg-accent-yellow/15 text-accent-yellow",
  red: "bg-accent-red/15 text-accent-red",
  neutral: "bg-white/10 text-white/70",
};

export function TagBadge({
  color = "neutral",
  children,
}: {
  color?: BadgeColor;
  children: React.ReactNode;
}) {
  return (
    <span
      className={clsx(
        "inline-flex items-center px-2 py-0.5 rounded-md text-[11px] font-medium font-mono whitespace-nowrap",
        COLOR_CLASSES[color],
      )}
    >
      {children}
    </span>
  );
}

/** Compact 5-step energy bar (level 0-5). */
export function EnergyBar({ level }: { level: number }) {
  const safe = Math.max(0, Math.min(5, level));
  const colors = ["#10b981", "#84cc16", "#f59e0b", "#f97316", "#ef4444"];
  return (
    <div className="flex items-center gap-0.5">
      {Array.from({ length: 5 }, (_, i) => {
        const filled = i < safe;
        const color = colors[Math.min(safe - 1, colors.length - 1)] ?? "#444";
        return (
          <div
            key={i}
            className="h-1.5 w-3 rounded-sm transition-colors"
            style={{
              background: filled ? color : "rgba(255,255,255,0.08)",
            }}
          />
        );
      })}
    </div>
  );
}
