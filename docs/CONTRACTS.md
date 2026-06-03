# Contracts: adding an RPC method end to end

Vibechek's UI talks to the Python sidecar over **JSON-RPC 2.0** (one JSON object per
line on stdin/stdout). The "contract" is the set of method names, their params, and
their result shapes — kept type-safe across the Python ↔ TypeScript boundary by a code
generator. This doc is the walkthrough for adding or changing a method.

See also: [CONTRIBUTING.md](../CONTRIBUTING.md) (the type bridge), [`vibechek/rpc.py`](../vibechek/rpc.py)
(the authoritative method list), and the "Sidecar protocol" section of
[`ui/README.md`](../ui/README.md).

> **The method surface stays at 44** — no new RPC *methods* through beta.10. FLAC → CDJ
> export is a CLI-only command (`vibechek cdj-export`). The ONNX inference engine did not
> add a method either, but it **did add params to existing methods** (the engine selection
> crosses the wire) — see "Engine-aware params" below.

## Engine-aware params (ONNX inference engine)

Selecting the inference engine (`essentia_tf` default, or `onnx`) is plumbed through
existing methods as a param — the Python side validates it via `rpc._valid_engine` and
routes to the matching managed venv (`venv` vs `venv-onnx`):

| Method | Param | Notes |
|---|---|---|
| `analyze_directory` | `inference_engine` | The actual analyze routing; without it the ONNX toggle is inert and an onnx-only install fails on the wrong venv. |
| `preflight` | `engine` | Probes the engine's venv for readiness. |
| `download_models` | `engine` | `onnx` fetches the converted-head bundle; `essentia_tf` the `.pb` set. |
| `install_vibechek_in_wsl` | `engine` | Picks the stack/venv to install (essentia-tensorflow vs plain essentia + onnxruntime). |
| `install_essentia_native` | `engine` | Native (Linux/macOS) counterpart. |
| `engine_gpu_status` | `engine` | The ONNX path probes onnxruntime's live ExecutionProviders in `venv-onnx`; the TF path probes TensorFlow. |
| `native_venv_status` | `engine` | Reports what's installed in the engine's managed venv. |
| `wsl_status` | `engine` | Selects which venv subdir (`venv` / `venv-onnx`) the per-distro probe inspects. |

The `EngineGpuInfo` result (returned by `engine_gpu_status`) gained two ONNX-specific
fields: **`provider`** (the onnxruntime ExecutionProvider that actually initialized, e.g.
`"CUDAExecutionProvider"`, or `null`) and **`runtime`** (e.g. `"onnxruntime 1.19.2"`).
Both are `null` for the TF engine, which uses `tf_version` / `missing_cuda_libs` instead.

Unknown/invalid engine values are coerced to the `essentia_tf` default at the boundary, so
older UIs that omit the param keep working.

## The wire format

```
Request:       {"jsonrpc":"2.0","id":1,"method":"dedupe","params":{"path":"..."}}
Response:      {"jsonrpc":"2.0","id":1,"result":{...}}
Error:         {"jsonrpc":"2.0","id":1,"error":{"code":-32000,"message":"..."}}
Notification:  {"jsonrpc":"2.0","method":"progress","params":{"current":50,"total":100,"message":"..."}}
```

- **stdout is protocol-only.** All logging goes to stderr. A stray `print()` in a handler
  corrupts the stream — use the logger.
- Long ops emit `progress` notifications (throttled ~20/sec) and, for analyze, per-track
  `track_analyzed` notifications. The Rust shell re-broadcasts both as Tauri events.

## The five steps

### 1. Implement the handler

In [`vibechek/rpc.py`](../vibechek/rpc.py):

```python
def _my_method(params: dict) -> dict:
    """One-line summary. Raise on failure — the dispatcher converts exceptions
    into JSON-RPC error responses."""
    path = Path(params["path"])
    # ... do the work ...
    return {"ok": True, "count": n}
```

Handlers take a single `params: dict` and return a JSON-serializable value. `Path`,
dataclasses, and Enums serialize automatically (see `_json_default`). Read params
defensively and coerce types — the wire is untyped JSON.

### 2. Register it in `METHODS`

Add an entry to the `METHODS` dict so the dispatcher can find it. If the method is
**long-running**, route it through the cancellation singleton (see how `analyze_directory`,
`organize`, `find_duplicates` do it) so:

- a Cancel click actually stops it, and
- quick reads (`get_config`, `system_info`, `preflight`) still interleave while it runs.

Only one cancellable long op runs at a time (the cancellation token is a singleton);
quick reads are unrestricted.

### 3. Define + regenerate types (if it carries a dataclass)

If the method accepts or returns a structured object, define it as a `@dataclass` in the
relevant `vibechek/` module, then regenerate the TypeScript mirror:

```bash
python scripts/generate_ts_types.py
```

This rewrites `ui/src/types/generated.ts` and `ui/src/lib/keeperConstants.ts`. **Commit
the generated files** — CI fails if they're stale. Use `__ts_overrides__` on the dataclass
when the JSON wire form is narrower than the Python storage form.

### 4. Add the typed UI wrapper

In [`ui/src/api/rpc.ts`](../ui/src/api/rpc.ts), add a wrapper so every UI call is typed:

```ts
export function myMethod(path: string): Promise<MyResult> {
  return rpc<MyResult>("my_method", { path });
}
```

UI code should call the wrapper, never `rpc("my_method", ...)` directly.

### 5. Test it

Add a unit test for the handler. The cross-check test
[`tests/test_rpc_method_sync.py`](../tests/test_rpc_method_sync.py) verifies that every
`METHODS` entry has a matching wrapper in `rpc.ts` — a missing wrapper fails CI, so step 4
is enforced.

## Error codes

| Code | Meaning |
|---|---|
| `-32700` / `-32600` / `-32601` / `-32602` | Standard JSON-RPC (parse / invalid request / method not found / bad params) |
| `-32000` | Generic handler error (the exception message is surfaced) |
| cancellation | A cancelled long op resolves to a structured "cancelled" result, not a hard error — the UI relies on `RpcError.cancelled` rather than message-matching |

## Checklist

- [ ] Handler implemented in `vibechek/rpc.py`, raises on failure
- [ ] Registered in `METHODS`; cancellation wired if long-running
- [ ] Dataclasses defined + `generate_ts_types.py` run, generated files committed
- [ ] Typed wrapper added in `ui/src/api/rpc.ts`
- [ ] Unit test added; `tests/test_rpc_method_sync.py` passes
- [ ] CHANGELOG entry under `[Unreleased]` if user-visible
