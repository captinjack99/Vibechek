/**
 * Shared leaf presentational primitives for the Settings view.
 *
 * Pure (props in, JSX out — no stores/rpc/state), extracted verbatim from
 * Settings.tsx so the page component and its section components share them
 * without the 2.3k-line god file. No behavior change.
 */
import type * as React from "react";
import { AlertTriangle, CheckCircle2 } from "lucide-react";

export function Section({
  icon,
  title,
  subtitle,
  children,
}: {
  icon?: React.ReactNode;
  title: string;
  subtitle: string;
  children: React.ReactNode;
}) {
  return (
    <div className="mb-8">
      <div className="flex items-baseline gap-3 mb-3">
        {icon && <div className="text-accent">{icon}</div>}
        <h2 className="font-display font-semibold text-lg">{title}</h2>
        {subtitle && <span className="text-xs text-white/40">{subtitle}</span>}
      </div>
      <div className="panel-pad space-y-4">{children}</div>
    </div>
  );
}

export function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <div className="label">{label}</div>
      {children}
    </div>
  );
}

export function Hint({ children }: { children: React.ReactNode }) {
  // /50 ≈ 5.2:1 on the dark surface (WCAG AA); the old /40 (~3.9:1) failed
  // for what is genuinely informational text.
  return <div className="text-xs text-white/50 mt-1">{children}</div>;
}

export function Row({ ok, label, detail }: { ok: boolean; label: string; detail: string }) {
  return (
    <div className="flex items-center gap-3">
      {ok ? (
        <CheckCircle2 className="w-4 h-4 text-accent-green flex-none" />
      ) : (
        <AlertTriangle className="w-4 h-4 text-accent-yellow flex-none" />
      )}
      <div className="text-sm text-white">{label}</div>
      <div className={`flex-1 text-xs ${ok ? "text-white/50" : "text-accent-yellow/90"} truncate`}>
        {detail}
      </div>
    </div>
  );
}

export function Stat({
  label,
  value,
  sub,
  accent,
}: {
  label: string;
  value: string | number;
  sub?: string;
  accent?: "green" | "neutral";
}) {
  return (
    <div>
      <div className="label">{label}</div>
      <div
        className={`text-xl font-display font-semibold tabular-nums ${
          accent === "green" ? "text-accent-green" : "text-white"
        }`}
      >
        {value}
      </div>
      {sub && <div className="text-[11px] text-white/40">{sub}</div>}
    </div>
  );
}

export function Toggle({
  label,
  checked,
  onChange,
  hint,
  danger,
}: {
  label: string;
  checked: boolean;
  onChange: (v: boolean) => void;
  hint?: string;
  danger?: boolean;
}) {
  return (
    <div>
      <label className="flex items-start gap-3 cursor-pointer">
        <input
          type="checkbox"
          checked={checked}
          onChange={(e) => onChange(e.target.checked)}
          className="mt-0.5 accent-accent"
        />
        <div className="flex-1">
          {/* Plain text — JSX escapes correctly, so labels pass "BPM & key"
              literally. Previously used dangerouslySetInnerHTML to render
              &amp; entities, an XSS footgun if a caller ever passed dynamic
              (track/genre) text. */}
          <div className={danger ? "text-sm text-accent-red" : "text-sm text-white/90"}>
            {label}
          </div>
          {hint && (
            <div className={danger ? "text-xs text-accent-red/70 mt-0.5" : "text-xs text-white/40 mt-0.5"}>
              {hint}
            </div>
          )}
        </div>
      </label>
    </div>
  );
}
