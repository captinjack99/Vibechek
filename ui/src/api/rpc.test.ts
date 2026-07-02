/**
 * Sync-check between the Python METHODS dict and the TS api/rpc wrappers.
 *
 * The authoritative cross-language check lives in PYTHON, not here:
 * `tests/test_rpc_method_sync.py` reads BOTH `vibechek/rpc.py:METHODS` and
 * this directory's `methods.ts:RPC_METHODS` and asserts they're identical.
 * That's the guardrail that catches "Python added a method, the TS side
 * forgot it" — it can't go self-referential because it reads both real files.
 * (The previous version of THIS file mirrored the list by hand and compared
 * it to itself, so 7 real methods drifted out of sync while the test stayed
 * green.)
 *
 * The checks below stay as a TS-side smoke test: RPC_METHODS is well-formed,
 * every name has a wrapper, and the hand mirror (`PYTHON_METHODS`) lines up
 * with RPC_METHODS. The Python test is what guarantees the mirror itself is
 * honest.
 *
 * Adding a new RPC method requires:
 *   1. vibechek/rpc.py        — register the handler in METHODS.
 *   2. ui/src/api/methods.ts   — append to RPC_METHODS + add a param type.
 *   3. ui/src/api/rpc.ts       — add the typed wrapper.
 *   4. THIS FILE              — add the name to PYTHON_METHODS.
 *   (tests/test_rpc_method_sync.py fails loudly if you miss step 1 or 2.)
 */

import { describe, expect, it } from "vitest";

import api from "./rpc";
import * as rpcModule from "./rpc";
import { RPC_METHODS, isKnownRpcMethod } from "./methods";

/**
 * Mirror of `vibechek/rpc.py:METHODS` keys. KEEP IN SYNC — the Python test
 * `tests/test_rpc_method_sync.py` enforces this against the real source.
 */
const PYTHON_METHODS = [
  "ping",
  "version",
  "system_info",
  "engine_gpu_status",
  "preflight",
  "wsl_status",
  "install_wsl",
  "install_vibechek_in_wsl",
  "upgrade_vibechek_in_wsl",
  "install_cuda_libs_in_wsl",
  "install_essentia_native",
  "native_venv_status",
  "repair_wsl_shim",
  "scan_directory",
  "scan_only",
  "analyze_directory",
  "find_duplicates",
  "handle_duplicates",
  "plan_organization",
  "organize",
  "apply_ml_tags",
  "backup_tags",
  "restore_tags",
  "restore_tags_with_remap",
  "download_models",
  "setup_onnx_engine",
  "setup_clap_engine",
  "setup_genre_resolver",
  "get_config",
  "save_config",
  "restore_default_config",
  "cancel_operation",
  "library_state",
  "forget_library",
  "load_recent_analysis",
  "resolve_genre_conflicts",
  "import_tag_priors",
  "rename_library",
  "tag_library",
  "count_new_tracks",
  "get_log_tail",
  "backup_history",
  "forget_backup",
  "list_journals",
  "revert_journal",
  "doctor",
  "verify_models",
  "list_profiles",
  "load_profile",
] as const;

/** The list the sync tests assert against (the hand mirror; the Python test
 * `tests/test_rpc_method_sync.py` keeps this honest against the real source). */
const AUTHORITATIVE_METHODS: readonly string[] = PYTHON_METHODS;

// snake_case (Python METHODS key) -> camelCase (TS wrapper name).
// Handles the WSL/CUDA edge cases where we keep the acronyms uppercase
// after the underscore boundary (installWSL, not installWsl).
function toCamel(s: string): string {
  const special: Record<string, string> = {
    install_wsl: "installWSL",
    install_vibechek_in_wsl: "installVibechekInWSL",
    upgrade_vibechek_in_wsl: "upgradeVibechekInWSL",
    install_cuda_libs_in_wsl: "installCudaLibsInWSL",
    repair_wsl_shim: "repairWSLShim",
    wsl_status: "wslStatus",
  };
  if (s in special) return special[s];
  return s.replace(/_([a-z])/g, (_, c: string) => c.toUpperCase());
}

describe("api/rpc.ts foundation", () => {
  it("imports cleanly and exposes a default namespace", () => {
    expect(api).toBeTruthy();
    expect(typeof api).toBe("object");
    // Spot-check that a few key wrappers are functions.
    expect(typeof api.analyzeDirectory).toBe("function");
    expect(typeof api.findDuplicates).toBe("function");
    expect(typeof api.preflight).toBe("function");
    expect(typeof api.installWSL).toBe("function");
  });

  it("RPC_METHODS is the canonical, deduplicated list", () => {
    expect(RPC_METHODS.length).toBeGreaterThan(0);
    expect(new Set(RPC_METHODS).size).toBe(RPC_METHODS.length);
  });

  it("isKnownRpcMethod recognizes registered names and rejects typos", () => {
    expect(isKnownRpcMethod("analyze_directory")).toBe(true);
    expect(isKnownRpcMethod("ping")).toBe(true);
    expect(isKnownRpcMethod("nope_not_a_method")).toBe(false);
  });
});

describe("api/rpc <-> vibechek/rpc.py sync", () => {
  it("every Python METHODS key appears in RPC_METHODS", () => {
    const tsSet = new Set<string>(RPC_METHODS);
    const missing = AUTHORITATIVE_METHODS.filter((n) => !tsSet.has(n));
    expect(missing, `Python methods missing from RPC_METHODS: ${missing.join(", ")}`).toEqual([]);
  });

  it("RPC_METHODS contains no entries that don't exist in Python METHODS", () => {
    const pySet = new Set<string>(AUTHORITATIVE_METHODS);
    const stale = RPC_METHODS.filter((n) => !pySet.has(n));
    expect(stale, `RPC_METHODS contains names not in vibechek/rpc.py: ${stale.join(", ")}`).toEqual([]);
  });

  it("every Python METHODS key has a corresponding wrapper function", () => {
    const apiAny = api as unknown as Record<string, unknown>;
    const moduleAny = rpcModule as unknown as Record<string, unknown>;
    const missing: string[] = [];
    for (const name of AUTHORITATIVE_METHODS) {
      const camel = toCamel(name);
      const fromDefault = typeof apiAny[camel] === "function";
      const fromNamed = typeof moduleAny[camel] === "function";
      if (!fromDefault || !fromNamed) {
        missing.push(`${name} -> ${camel}`);
      }
    }
    expect(missing, `Methods missing wrappers in api/rpc.ts: ${missing.join(", ")}`).toEqual([]);
  });
});
