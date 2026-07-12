# Contracts: adding an RPC method end to end

Vibechek's UI talks to the Python sidecar over **JSON-RPC 2.0** (one JSON object per
line on stdin/stdout). The "contract" is the set of method names, their params, and
their result shapes — kept type-safe across the Python ↔ TypeScript boundary by a code
generator. This doc is the walkthrough for adding or changing a method.

See also: [CONTRIBUTING.md](../CONTRIBUTING.md) (the type bridge), [`vibechek/rpc.py`](../vibechek/rpc.py)
(the authoritative method list), and the "Sidecar protocol" section of
[`ui/README.md`](../ui/README.md).

> **The method surface is 50.** `worker_budget` computes the effective worker plan
> (slider max + per-worker RAM + which RAM pool was measured) for a given engine ×
> genre classifier, so the Settings slider binds its max to what actually fits and can
> never disagree with the run — both call the one pure `resources.compute_worker_budget`.
> For WSL-routed engines it measures the WSL VM's RAM (the pool workers draw from), not
> the host total. The one-click engine setups added three methods —
> `setup_onnx_engine`, `setup_clap_engine`, `setup_genre_resolver` (all cancellable,
> progress-emitting) — while FLAC → CDJ export stayed CLI-only (`vibechek cdj-export`).
> The trust-UX review queue added `resolve_genre_conflicts` (batch approve/revert of
> reviewed genre conflicts; persists to the saved analysis, never writes file tags).
> Trust-UX #3 added `import_tag_priors` (parse a Rekordbox collection XML into
> tag-tier priors: genre supersedes at the tag tier with
> `existing_tags.genre_origin="rekordbox"`, key/MIK-energy only fill; re-reconciles +
> persists the saved analysis and a `<analysis>.priors.json` sidecar that
> `analyze_directory` re-merges on every future run; never writes file tags).
> The genre setups route per platform behind the same wire shape: Windows → the WSL
> scripts, Linux/macOS → `native_install.setup_clap_native`/`setup_resolver_native`
> (same venv + artifact paths, so analyze-time consumers don't care which ran).
> Engine/genre selection also **added params to existing methods** (they cross the
> wire) — see "Engine-aware params" below.

## Engine-aware params (ONNX inference engine)

Selecting the inference engine (`essentia_tf` default, `onnx`, or `native` — the
experimental WSL-free Windows path: ONNX inference + a NumPy mel frontend + an
in-process native essentia wheel for decode/BPM/key) is plumbed through existing
methods as a param — the Python side validates it via `rpc._valid_engine` and
routes to the matching managed venv (`venv` vs `venv-onnx`) or, for `native`,
runs in-process when essentia imports locally:

| Method | Param | Notes |
|---|---|---|
| `analyze_directory` | `inference_engine` | The actual analyze routing; without it the ONNX toggle is inert and an onnx-only install fails on the wrong venv. |
| `analyze_directory` | `genre_classifier` | `discogs` (default) or `clap` — which audio model fills `ml_genre` (validated via `rpc._valid_genre_classifier`). |
| `analyze_directory` | `genre_web_lookup` / `genre_llm_backend` | Toggle + backend for the online web-synthesis genre lookup (`rpc._valid_llm_backend`). |
| `preflight` | `engine` | Probes the engine's venv for readiness. |
| `download_models` | `engine` | `onnx` fetches the converted-head bundle; `essentia_tf` the `.pb` set. |
| `install_vibechek_in_wsl` | `engine` | Picks the stack/venv to install (essentia-tensorflow vs plain essentia + onnxruntime). |
| `install_essentia_native` | `engine` | Native (Linux/macOS) counterpart. |
| `install_essentia_native`, `setup_onnx_engine` | `vibechek_source` | Optional, CI/dev only: an **existing local directory** that pip installs vibechek from, instead of the hard-coded GitHub default (`rpc._valid_vibechek_source`; anything else → `INVALID_PARAMS`). Honored by the native (Linux/macOS) install only — the WSL branch ignores it. The native-smoke full tier passes the checkout so CI tests the commit under test. The GUI never sends it. |
| `engine_gpu_status` | `engine` | The ONNX path probes onnxruntime's live ExecutionProviders in `venv-onnx`; the TF path probes TensorFlow. |
| `worker_budget` | `engine`, `genre_classifier`, `workers`, `distro` | Returns a `WorkerBudget` (max_workers, per_worker_mb, ram_pool, cap/refusal reasons). CLAP's ~4.5 GB/worker shrinks the max; pass the usable `distro` so the WSL VM's RAM is measured instead of the host total. |
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
Notification:  {"jsonrpc":"2.0","method":"progress","params":{"current":50,"total":100,"message":"...","kind":"analyze","op_id":"<uuid>"}}
```

- **stdout is protocol-only.** All logging goes to stderr. A stray `print()` in a handler
  corrupts the stream — use the logger.
- Long ops emit `progress` notifications (throttled ~20/sec) and, for analyze, per-track
  `track_analyzed` notifications. The Rust shell re-broadcasts both as Tauri events.
- **Operation ids.** A long-op request MAY carry a client-generated `op_id` string in
  params. The dispatcher strips it before the handler runs (handlers never see it) and
  echoes it — together with the op's `kind` — on every `progress` / `track_analyzed`
  notification emitted while that op runs. Both fields are omitted when unknown (CLI,
  legacy clients); consumers must treat absence as "match anything". The GUI generates
  one per `useOperationStore.begin()` and drops events on a positive mismatch
  (`progressMatches` in `ui/src/stores/operation.ts`), so the dialogs sharing the one
  event stream can't render a straggler from a cancelled/previous op.

## Optional fields on the wire (`None` is dropped)

Result dataclasses are serialized with `None` values **omitted**, not emitted as
`null` — `analyze_track` builds each record as `{k: v for k, v in asdict(ml).items()
if v is not None}`. So a field typed `X | null` in `generated.ts` may be **absent**
(`undefined`) on the wire, not `null`. **TS consumers must use truthy / `?.` checks,
never `=== null`.**

This is how new optional fields stay backwards-safe: declaring them on the dataclass
with a `None` default makes the generator emit them (type-safe for the UI) while keeping
them off the wire until something sets them — the raw record shape is unchanged.

Concretely, the genre/vocal/key **reconciliation-provenance** fields on `MLResult`
(`ml_genre_source`, `ml_genre_conflict`, `ml_genre_audio`, `ml_subgenre_audio`,
`ml_genre_web`, `ml_genre_web_grounded`, `ml_vocal_audio`, `ml_vocal_source`,
`ml_key_tag`, `ml_key_conflict`) are stamped only on the **final** report (in
`_reconcile_record_genre` / `_reconcile_record_vocal` / `_reconcile_record_key`), so
they're absent on the raw per-track `track_analyzed` notifications that stream during
analyze. The library UI's conflict surfacing keys off them with truthy checks accordingly.
The key pair is read-only surfacing: `ml_key` itself stays the audio read (embedded tag
keys measured 49% exact vs audio's 63% on the gold corpus, wrong 10:1 on disagreement —
`internal/bughunt/score_tag_priors.py`), so tags flag for review, never override.

`MLResult` also carries two **silent-degradation provenance** fields, `ml_genre_classifier`
(`"clap"` | `"discogs"` — which audio model actually produced this track's genre) and
`ml_degraded_heads` (the mood/vocal/danceability model heads that failed to *load* this
run, so the track's energy/mood/vocal ran on a fallback). Unlike the reconciliation fields
above, both are stamped inside `analyze_track`/`analyze_audio_features` during the initial
worker pass, so — unusually for this optional-field family — they ARE present on the raw
per-track `track_analyzed` notifications, not just the final report.

### Report-level warning fields

Separately, the **`AnalysisReport`** dict returned by the `analyze_directory` RPC (not
`scan_only`, which is a filename-hints-only pass with no ML and none of these fields)
carries transport-level warning fields attached by `rpc._analyze_directory` /
`analyzer._build_report` / `analyzer._analyze_via_wsl` after the run finishes. Every one
follows the same None-omitted contract — **absent when the run was clean**, present only
when something degraded:

| Field | Set when |
|---|---|
| `persist_error` / `persist_path` | The analyze succeeded but the auto-save to the analyses JSON failed (disk full, a cloud-sync/antivirus lock); `persist_path` is where the save was attempted. |
| `priors_warning` | A Rekordbox tag-priors import existed but was dropped this run — an unreadable sidecar, or (on a FULL run only) 0 tracks matched because the library moved. |
| `genre_fallback_warning` | CLAP was the selected genre classifier but N tracks silently fell back to Discogs (per-track embed failure, or CLAP never loaded for the worker). Derived by unioning every track's `ml_genre_classifier`. |
| `model_degradation_warning` | One or more mood/vocal/danceability heads failed to load, so energy/mood/vocal ran on a fallback for the whole run. Derived by unioning every track's `ml_degraded_heads`. |
| `runtime_healed` | The WSL engine environment was auto-repaired before this run (`wsl.ensure_engine_runtime`) — informational, the run then proceeded normally. |
| `runtime_heal_warning` | That auto-repair could not restore GPU libraries, so this run fell back to CPU. |
| `run_meta` | The resolved worker/GPU plan this run actually used (`engine`, `genre_classifier`, `requested_workers`, `effective_workers`, `gpu_workers`, `cpu_workers`, `gpu_reason`). Set on every non-trivial `analyze_directory` response (absent only on the early empty-library return, `total_files == 0`) — but it's **not declared in `generated.ts`/`index.ts`**, since the GUI doesn't read it live; only `rpc._record_run_history` reads it off the report to append a line to `logs/run_history.jsonl`, which `doctor`'s "last analyze run" section reads back. |

`persist_error`/`priors_warning`/`genre_fallback_warning`/`model_degradation_warning`/
`runtime_healed`/`runtime_heal_warning`/`run_meta` are exactly the set `_record_run_history`
copies into the durable run-history line (`vibechek/rpc.py`). All except `run_meta` are
declared on `AnalysisReport` in `ui/src/types/index.ts` (hand-written — analyze's wire
shape isn't code-generated) and surfaced by the GUI's post-analyze completion toast.

`DuplicateSummary` (part of `DuplicateReport.summary`, also hand-written in `index.ts`
since `DuplicateReport.to_dict()` renames/flattens the Python dataclass) similarly gained
`phases_run` (which detection phases actually ran — `"md5"`, `"chromaprint"`) and
`fpcalc_available` (false only when audio fingerprinting was requested but the `fpcalc`
binary wasn't found, so that phase silently never ran). Both have non-`None` dataclass
defaults (`[]` / `True`), so the Python side always emits them; `index.ts` still marks them
optional (`phases_run?`, `fpcalc_available?`) for defensive parsing of older cached reports.

`get_config`'s response gains a transport-level `config_warnings: string[]` field (not a
`VibechekConfig` field, so it never round-trips back to disk on save) when `VibechekConfig.load()`
had to snap one or more invalid/cross-platform saved values back to defaults — Settings
shows a one-time "some saved settings were invalid and were reset" toast off this list.

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
