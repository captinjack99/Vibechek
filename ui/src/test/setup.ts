/**
 * Vitest global setup.
 *
 * Two responsibilities:
 *   1. Wire @testing-library/jest-dom matchers into vitest's `expect`.
 *   2. Mock every Tauri API surface our components touch — they all throw at
 *      module-load time in a non-Tauri environment. Components that call
 *      `invoke()`, `listen()`, `convertFileSrc()`, etc. don't need to be
 *      Tauri-aware in tests; the stubs below return safe defaults.
 *
 * Zustand stores are global singletons. We reset them in beforeEach so tests
 * don't leak state between cases. (Simpler than refactoring every store to
 * accept an explicit instance.)
 */

import "@testing-library/jest-dom/vitest";
import { afterEach, beforeEach, vi } from "vitest";
import { cleanup } from "@testing-library/react";

// ---------------------------------------------------------------------------
// Tauri mocks — hoisted by vi.mock() so they're in place before any import
// triggers module evaluation of components that use them.
// ---------------------------------------------------------------------------

vi.mock("@tauri-apps/api/core", () => ({
  invoke: vi.fn(async () => ({})),
  convertFileSrc: (src: string) => src,
  // useUpdater() gates every plugin call behind isTauri(); in tests we're not
  // in the Tauri shell, so it must return false (and never lazy-load the
  // updater/process plugins).
  isTauri: () => false,
}));

vi.mock("@tauri-apps/api/event", () => ({
  listen: vi.fn(async () => () => {}),
  emit: vi.fn(async () => {}),
}));

vi.mock("@tauri-apps/plugin-dialog", () => ({
  open: vi.fn(async () => null),
  save: vi.fn(async () => null),
  message: vi.fn(async () => {}),
  ask: vi.fn(async () => false),
  confirm: vi.fn(async () => false),
}));

vi.mock("@tauri-apps/plugin-shell", () => ({
  open: vi.fn(async () => {}),
  Command: class {
    static create() {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any -- test mock: `this` is the anonymous Command class constructor
      return new (this as any)();
    }
    execute() {
      return Promise.resolve({ code: 0, stdout: "", stderr: "" });
    }
  },
}));

// ---------------------------------------------------------------------------
// Per-test cleanup + store reset
// ---------------------------------------------------------------------------

beforeEach(async () => {
  // Reset stores so each test starts from a known state. Imported lazily so
  // the vi.mock() calls above are evaluated before the store module loads.
  const stores = await import("../stores");

  stores.useLibraryStore.setState({
    libraryPath: null,
    tracks: [],
    selectedIds: new Set(),
    searchFilter: "",
  });

  stores.useOperationStore.setState({
    active: null,
    progress: null,
    startedAt: null,
    error: null,
    errorInfo: null,
    duplicateReport: null,
    organizePlan: null,
  });

  stores.useUIStore.setState({
    viewMode: "library",
    sidebarCollapsed: false,
    selectedTrackPath: null,
  });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});
