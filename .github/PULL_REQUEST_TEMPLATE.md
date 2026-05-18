<!-- One- to two-sentence summary at the top. Make it the line that ends up in the changelog. -->

## What

## Why

## Testing
<!-- Manual steps you ran. Screenshots for UI changes. -->

## Checklist
- [ ] Tests added or updated (`pytest tests/` + `cd ui && npm test`)
- [ ] Existing tests still pass
- [ ] If I added a Python dataclass field: ran `scripts/generate_ts_types.py`
- [ ] If I added an RPC method: registered in `vibechek/rpc.py:METHODS`, added the typed wrapper in `ui/src/api/rpc.ts`, added the cancellation kind in `_CANCELLABLE_METHODS` if it's long-running, wrote a success-path test AND a cancellation test (see `docs/CONTRACTS.md`)
- [ ] If I touched the README stats line: ran `scripts/update_readme_stats.py`
- [ ] If I added a new dependency: justified it in the PR description (we prefer stdlib + existing deps)
- [ ] No `String(e)` in catch handlers (use `fail(e)` and let the operation store unwrap `RpcError`)
- [ ] No bare `Path.write_text(json.dumps(...))` for user data (use `vibechek.io.atomic_write_json`)

## Related issues
<!-- "Closes #123" or "Refs #456" -->
