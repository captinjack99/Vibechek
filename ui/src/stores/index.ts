/**
 * Global UI state stores — re-export shim.
 *
 * The actual stores live in sibling files; this file exists so that legacy
 * `import { useLibraryStore } from "../stores"` imports across the component
 * tree keep working unchanged.
 *
 * Five stores keep concerns separate:
 *   - useLibraryStore       (./library)       analyzed tracks + selection
 *   - useOperationStore     (./operation)     running long ops + progress + error
 *   - useUIStore            (./ui)            view mode, sidebar, selected track
 *   - useConfigStore        (./config)        user-tunable settings
 *   - useNotificationStore  (./notification)  transient success/info toasts
 */

export { useLibraryStore } from "./library";
export { useOperationStore } from "./operation";
export { useUIStore } from "./ui";
export { useConfigStore } from "./config";
export { useNotificationStore } from "./notification";

// Public types previously declared inline in this file. Kept exported here
// so external imports like `import type { Notification } from "../stores"`
// continue to resolve.
export type { Notification, NotificationKind } from "./notification";
