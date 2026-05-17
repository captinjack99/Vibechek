import { Music, Copy, FolderTree, Archive, Settings as SettingsIcon, Disc3 } from "lucide-react";
import { clsx } from "clsx";

import { useUIStore, useLibraryStore, useOperationStore } from "../stores";
import type { ViewMode } from "../types";

/** "12,466" → "12k" via Intl. Tooltip-friendly precise form is just toLocaleString. */
const compactFmt = new Intl.NumberFormat(undefined, {
  notation: "compact",
  maximumFractionDigits: 1,
});

interface Badge {
  /** Short label rendered in the sidebar: "12k tracks". */
  short: string;
  /** Exact count for the title/tooltip: "12,466 tracks". */
  precise: string;
}

interface NavItem {
  id: ViewMode;
  label: string;
  icon: React.ReactNode;
  badge?: () => Badge | null;
}

function fmtBadge(count: number, unit: string): Badge {
  return {
    short: `${compactFmt.format(count)} ${unit}`,
    precise: `${count.toLocaleString()} ${unit}`,
  };
}

export function Sidebar() {
  const viewMode = useUIStore((s) => s.viewMode);
  const setViewMode = useUIStore((s) => s.setViewMode);
  const trackCount = useLibraryStore((s) => s.tracks.length);
  const dupReport = useOperationStore((s) => s.duplicateReport);
  const organizePlan = useOperationStore((s) => s.organizePlan);

  const items: NavItem[] = [
    {
      id: "library",
      label: "Library",
      icon: <Music className="w-5 h-5" />,
      badge: () => (trackCount > 0 ? fmtBadge(trackCount, "tracks") : null),
    },
    {
      id: "duplicates",
      label: "Duplicates",
      icon: <Copy className="w-5 h-5" />,
      badge: () =>
        dupReport && dupReport.summary.total_duplicates > 0
          ? fmtBadge(dupReport.summary.total_duplicates, "dupes")
          : null,
    },
    {
      id: "organize",
      label: "Organize",
      icon: <FolderTree className="w-5 h-5" />,
      badge: () =>
        organizePlan && organizePlan.moves.length > 0
          ? {
              short: `${compactFmt.format(organizePlan.moves.length)} moves planned`,
              precise: `${organizePlan.moves.length.toLocaleString()} moves planned`,
            }
          : null,
    },
    {
      id: "tags",
      label: "Tags",
      icon: <Archive className="w-5 h-5" />,
    },
    {
      id: "settings",
      label: "Settings",
      icon: <SettingsIcon className="w-5 h-5" />,
    },
  ];

  return (
    <aside className="w-56 shrink-0 bg-surface-100 border-r border-white/5 flex flex-col">
      {/* Brand */}
      <div className="flex items-center gap-2 px-4 py-5">
        <div className="w-8 h-8 rounded-lg bg-accent/20 flex items-center justify-center">
          <Disc3 className="w-5 h-5 text-accent" />
        </div>
        <div>
          <div className="font-display font-semibold text-white">Vibechek</div>
          <div className="text-[10px] uppercase tracking-wider text-white/40">
            v0.3.0-dev
          </div>
        </div>
      </div>

      {/* Nav */}
      <nav className="flex-1 px-2 space-y-1">
        {items.map((item) => {
          const isActive = viewMode === item.id;
          const badge = item.badge?.();
          return (
            <button
              key={item.id}
              onClick={() => setViewMode(item.id)}
              className={clsx(
                "w-full flex items-center gap-3 px-3 py-2 rounded-lg text-sm transition-colors",
                isActive
                  ? "bg-accent/15 text-accent"
                  : "text-white/70 hover:text-white hover:bg-white/5",
              )}
            >
              {item.icon}
              <span className="flex-1 text-left font-medium">{item.label}</span>
              {badge && (
                <span
                  title={badge.precise}
                  className={clsx(
                    "text-[10px] px-2 py-0.5 rounded-full whitespace-nowrap",
                    isActive
                      ? "bg-accent/25 text-accent"
                      : "bg-white/10 text-white/60",
                  )}
                >
                  {badge.short}
                </span>
              )}
            </button>
          );
        })}
      </nav>

      {/* Footer */}
      <div className="px-4 py-3 text-[10px] text-white/30">
        AGPL-3.0 • open source
      </div>
    </aside>
  );
}
