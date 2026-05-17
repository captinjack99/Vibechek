/**
 * Reusable confirm modal. Replaces window.confirm / window.alert across the
 * app with something that matches the rest of the UI and supports rich
 * content (lists, code blocks, danger styling, side-effect checkboxes).
 *
 * Imperative API — caller passes a state object and renders <ConfirmModal />.
 * The alternative (programmatic invocation via a hook) is nicer but requires
 * a context provider; keep it simple for now.
 */

import { motion } from "framer-motion";
import { AlertTriangle, X, type LucideIcon } from "lucide-react";

export interface ConfirmModalProps {
  open: boolean;
  title: string;
  message: React.ReactNode;
  confirmLabel?: string;
  cancelLabel?: string;
  /** Visual severity. `danger` = red accent + scary copy. */
  variant?: "default" | "danger";
  icon?: LucideIcon;
  /** Optional side-effect (e.g. "back up tags first") rendered above the buttons. */
  extra?: React.ReactNode;
  onConfirm: () => void;
  onCancel: () => void;
}

export function ConfirmModal({
  open,
  title,
  message,
  confirmLabel = "Confirm",
  cancelLabel = "Cancel",
  variant = "default",
  icon: Icon = AlertTriangle,
  extra,
  onConfirm,
  onCancel,
}: ConfirmModalProps) {
  if (!open) return null;

  const isDanger = variant === "danger";

  return (
    <motion.div
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      exit={{ opacity: 0 }}
      className="fixed inset-0 z-[60] bg-black/60 flex items-center justify-center px-4"
      onClick={onCancel}
    >
      <motion.div
        initial={{ scale: 0.96, y: 10 }}
        animate={{ scale: 1, y: 0 }}
        className="panel max-w-lg w-full"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-start gap-3 px-5 py-4 border-b border-white/5">
          <Icon className={`w-6 h-6 flex-none mt-0.5 ${isDanger ? "text-accent-red" : "text-accent-yellow"}`} />
          <div className="flex-1">
            <h2 className="font-display font-semibold text-lg">{title}</h2>
          </div>
          <button onClick={onCancel} className="text-white/40 hover:text-white -m-1 p-1">
            <X className="w-5 h-5" />
          </button>
        </div>

        <div className="px-5 py-4 text-sm text-white/80 space-y-3 max-h-[60vh] overflow-auto">
          {message}
        </div>

        {extra && (
          <div className="px-5 py-3 border-t border-white/5 bg-white/[0.02]">
            {extra}
          </div>
        )}

        <div className="flex items-center justify-end gap-2 px-5 py-3 border-t border-white/5">
          <button className="btn-ghost" onClick={onCancel}>
            {cancelLabel}
          </button>
          <button
            className={isDanger ? "btn-danger" : "btn-primary"}
            onClick={onConfirm}
            autoFocus
          >
            {confirmLabel}
          </button>
        </div>
      </motion.div>
    </motion.div>
  );
}
