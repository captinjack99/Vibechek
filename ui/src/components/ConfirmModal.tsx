/**
 * Reusable confirm modal. Replaces window.confirm / window.alert across the
 * app with something that matches the rest of the UI and supports rich
 * content (lists, code blocks, danger styling, side-effect checkboxes).
 *
 * Imperative API — caller passes a state object and renders <ConfirmModal />.
 * The alternative (programmatic invocation via a hook) is nicer but requires
 * a context provider; keep it simple for now.
 */

import { useEffect, useRef } from "react";
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
  /**
   * When true, treat this as a destructive action: backdrop clicks become
   * no-ops so the user can't accidentally bump the modal closed while reading
   * the preview. They must explicitly press Cancel or Confirm. The close (X)
   * button still works.
   */
  destructive?: boolean;
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
  destructive = false,
  onConfirm,
  onCancel,
}: ConfirmModalProps) {
  const isDanger = variant === "danger";

  const dialogRef = useRef<HTMLDivElement>(null);
  const cancelRef = useRef<HTMLButtonElement>(null);
  const confirmRef = useRef<HTMLButtonElement>(null);

  // Initial focus: for a destructive (danger) confirm, land on Cancel — never
  // pre-arm the dangerous action. For a non-danger confirm, focus the primary
  // action (the old autoFocus behaviour). Runs whenever the modal opens.
  useEffect(() => {
    if (!open) return;
    const target = isDanger ? cancelRef.current : confirmRef.current;
    // rAF so focus lands after the element is actually in the DOM/animating.
    const id = requestAnimationFrame(() => target?.focus());
    return () => cancelAnimationFrame(id);
  }, [open, isDanger]);

  // Escape cancels (matches the close button) + Tab focus trap that keeps
  // keyboard focus inside the dialog. Without the trap, Tab walks into the
  // dimmed page behind the modal — bad for an app whose value prop is "don't
  // lose the user's music."
  useEffect(() => {
    if (!open) return;
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        onCancel();
        return;
      }
      if (e.key !== "Tab") return;
      const root = dialogRef.current;
      if (!root) return;
      const focusable = root.querySelectorAll<HTMLElement>(
        'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
      );
      if (focusable.length === 0) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      const activeEl = document.activeElement as HTMLElement | null;
      // Wrap focus at the ends, and pull stray focus (e.g. on the body) back
      // into the dialog.
      if (e.shiftKey) {
        if (activeEl === first || !root.contains(activeEl)) {
          e.preventDefault();
          last.focus();
        }
      } else if (activeEl === last || !root.contains(activeEl)) {
        e.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [open, onCancel]);

  if (!open) return null;

  // Backdrop click dismisses by default. For destructive confirms we ignore
  // it — the user has to actively choose Cancel or Confirm.
  const handleBackdropClick = destructive ? undefined : onCancel;

  return (
    <motion.div
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      exit={{ opacity: 0 }}
      className="fixed inset-0 z-[60] bg-black/60 flex items-center justify-center px-4"
      onClick={handleBackdropClick}
    >
      <motion.div
        ref={dialogRef}
        initial={{ scale: 0.96, y: 10 }}
        animate={{ scale: 1, y: 0 }}
        className="panel max-w-lg w-full"
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-label={title}
      >
        <div className="flex items-start gap-3 px-5 py-4 border-b border-white/5">
          <Icon className={`w-6 h-6 flex-none mt-0.5 ${isDanger ? "text-accent-red" : "text-accent-yellow"}`} />
          <div className="flex-1">
            <h2 className="font-display font-semibold text-lg">{title}</h2>
          </div>
          <button
            onClick={onCancel}
            className="text-white/40 hover:text-white -m-1 p-1"
            aria-label="Close"
          >
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
          <button ref={cancelRef} className="btn-ghost" onClick={onCancel}>
            {cancelLabel}
          </button>
          <button
            ref={confirmRef}
            className={isDanger ? "btn-danger" : "btn-primary"}
            onClick={onConfirm}
          >
            {confirmLabel}
          </button>
        </div>
      </motion.div>
    </motion.div>
  );
}
