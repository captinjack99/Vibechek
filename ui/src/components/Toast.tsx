/**
 * Stack of transient notifications at the bottom-right.
 *
 * Auto-dismisses (4 s, or 8 s when a detail line needs reading time), pauses
 * the timer on hover, and caps the visible stack (store-side). Reads from
 * `useNotificationStore`; components push via
 * `useNotificationStore.getState().notify(...)`.
 *
 * Different from `ErrorToast`: that one is sticky, top-centred, and styled for
 * failure. This one is small, cheerful, and stacks. Rendered at z-[70] — ABOVE
 * modal backdrops (z-[60]) so a toast fired while a dialog is open isn't dimmed
 * behind it.
 */

import { useEffect, useRef } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { AlertTriangle, CheckCircle2, Info, X } from "lucide-react";

import { useNotificationStore, type Notification } from "../stores";

const AUTO_DISMISS_MS = 4000;
const AUTO_DISMISS_DETAIL_MS = 8000;

export function Toast() {
  const items = useNotificationStore((s) => s.items);
  const dismiss = useNotificationStore((s) => s.dismiss);

  return (
    <div className="fixed bottom-4 right-4 z-[70] flex flex-col gap-2 items-end pointer-events-none">
      <AnimatePresence>
        {items.map((n) => (
          <ToastItem key={n.id} notification={n} onDismiss={() => dismiss(n.id)} />
        ))}
      </AnimatePresence>
    </div>
  );
}

const KIND_STYLE: Record<Notification["kind"], { icon: typeof Info; accent: string }> = {
  success: { icon: CheckCircle2, accent: "text-accent-green border-accent-green/30 bg-accent-green/10" },
  info: { icon: Info, accent: "text-accent-cyan border-accent-cyan/30 bg-accent-cyan/10" },
  warning: { icon: AlertTriangle, accent: "text-accent-yellow border-accent-yellow/30 bg-accent-yellow/10" },
};

function ToastItem({
  notification,
  onDismiss,
}: {
  notification: Notification;
  onDismiss: () => void;
}) {
  // Pause-on-hover: reading a multi-line detail used to race a fixed 4 s
  // timer. Hover clears the timeout; leaving re-arms it in full.
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const ttl = notification.detail ? AUTO_DISMISS_DETAIL_MS : AUTO_DISMISS_MS;

  const arm = () => {
    // A persistent notification (app-breaking condition) never auto-dismisses —
    // only a click removes it.
    if (notification.persistent) return;
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(onDismiss, ttl);
  };
  const pause = () => {
    if (timer.current) clearTimeout(timer.current);
    timer.current = null;
  };

  useEffect(() => {
    arm();
    return pause;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const { icon: Icon, accent } = KIND_STYLE[notification.kind] ?? KIND_STYLE.info;

  return (
    <motion.div
      initial={{ opacity: 0, x: 30, scale: 0.95 }}
      animate={{ opacity: 1, x: 0, scale: 1 }}
      exit={{ opacity: 0, x: 30, scale: 0.95 }}
      transition={{ duration: 0.18 }}
      className={`pointer-events-auto panel-pad min-w-[280px] max-w-md flex items-start gap-3 shadow-lg ${accent}`}
      role="status"
      aria-live="polite"
      onMouseEnter={pause}
      onMouseLeave={arm}
    >
      <Icon className="w-5 h-5 flex-none mt-0.5" />
      <div className="flex-1 min-w-0">
        <div className="text-sm font-medium text-white">{notification.message}</div>
        {notification.detail && (
          <div className="text-xs text-white/60 mt-0.5 whitespace-pre-line break-words">
            {notification.detail}
          </div>
        )}
        {notification.action && (
          <button
            onClick={notification.action.onClick}
            className="mt-2 text-xs font-medium text-white/90 hover:text-white underline underline-offset-2"
          >
            {notification.action.label}
          </button>
        )}
      </div>
      <button
        onClick={onDismiss}
        className="text-white/40 hover:text-white -m-1 p-1 flex-none"
        title="Dismiss"
        aria-label="Dismiss notification"
      >
        <X className="w-4 h-4" />
      </button>
    </motion.div>
  );
}
