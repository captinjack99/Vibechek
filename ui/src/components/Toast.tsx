/**
 * Stack of transient notifications at the bottom-right.
 *
 * Each notification auto-dismisses after 4 seconds; the user can also click
 * the X to dismiss early. Reads from `useNotificationStore`. Components push
 * notifications via `useNotificationStore.getState().notify(...)`.
 *
 * Different from `ErrorToast`: that one is sticky, top-centred, and styled for
 * failure. This one is small, cheerful, and stacks.
 */

import { useEffect } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { CheckCircle2, Info, X } from "lucide-react";

import { useNotificationStore, type Notification } from "../stores";

const AUTO_DISMISS_MS = 4000;

export function Toast() {
  const items = useNotificationStore((s) => s.items);
  const dismiss = useNotificationStore((s) => s.dismiss);

  return (
    <div className="fixed bottom-4 right-4 z-50 flex flex-col gap-2 items-end pointer-events-none">
      <AnimatePresence>
        {items.map((n) => (
          <ToastItem key={n.id} notification={n} onDismiss={() => dismiss(n.id)} />
        ))}
      </AnimatePresence>
    </div>
  );
}

function ToastItem({
  notification,
  onDismiss,
}: {
  notification: Notification;
  onDismiss: () => void;
}) {
  useEffect(() => {
    const t = setTimeout(onDismiss, AUTO_DISMISS_MS);
    return () => clearTimeout(t);
  }, [onDismiss]);

  const Icon = notification.kind === "success" ? CheckCircle2 : Info;
  const accent =
    notification.kind === "success"
      ? "text-accent-green border-accent-green/30 bg-accent-green/10"
      : "text-accent-cyan border-accent-cyan/30 bg-accent-cyan/10";

  return (
    <motion.div
      initial={{ opacity: 0, x: 30, scale: 0.95 }}
      animate={{ opacity: 1, x: 0, scale: 1 }}
      exit={{ opacity: 0, x: 30, scale: 0.95 }}
      transition={{ duration: 0.18 }}
      className={`pointer-events-auto panel-pad min-w-[280px] max-w-md flex items-start gap-3 shadow-lg ${accent}`}
      role="status"
      aria-live="polite"
    >
      <Icon className="w-5 h-5 flex-none mt-0.5" />
      <div className="flex-1 min-w-0">
        <div className="text-sm font-medium text-white">{notification.message}</div>
        {notification.detail && (
          <div className="text-xs text-white/60 mt-0.5 whitespace-pre-line break-words">
            {notification.detail}
          </div>
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
