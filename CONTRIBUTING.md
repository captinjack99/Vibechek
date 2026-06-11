# Contributing to Vibechek

Thanks for thinking about contributing. Vibechek is open-source because DJs-who-code know what DJs need better than anyone else. Bug reports, feature ideas, doc fixes, and code patches are all welcome.

Before you start anything large, please open an issue or a draft PR so we can talk through the approach. Nothing worse than spending a weekend on a refactor that conflicts with what's already in flight.

---

## Quick start (developer setup)

```bash
git clone https://github.com/captinjack99/Vibechek.git
cd Vibechek

# Python core
python -m venv .venv
. .venv/Scripts/activate          # Windows
# source .venv/bin/activate       # macOS / Linux
pip install -e ".[dev]"

# Frontend
cd ui && npm install
cd ..

# Build the sidecar binary once so the desktop app can find it
./packaging/build-windows.bat     # Windows
# ./packaging/build-linux.sh      # Linux
# ./packaging/build-macos.sh      # macOS

# Point Tauri at the sidecar and start the dev server
$env:VIBECHEK_SIDECAR = "$pwd\.venv\Scripts\vibechek.exe"   # Windows
# export VIBECHEK_SIDECAR=$PWD/.venv/bin/vibechek            # macOS / Linux
cd ui && npm run tauri:dev
```

The first dev launch will be slow (Cargo cold build of the Tauri shell). Subsequent launches are fast.

If you only plan to work on the Python core, you can skip the Tauri pieces entirely and drive everything through `pytest` + the `vibechek` CLI.

---

## Running tests and lint

```bash
# Python
pytest -v                          # all tests
pytest tests/test_organizer.py -v  # single file
pytest --cov=vibechek              # with coverage

# Lint + format check
ruff check vibechek tests
ruff format --check vibechek tests

# Frontend
cd ui
npm test                           # vitest
npm run typecheck                  # tsc --noEmit
npm run build                      # production bundle
```

CI runs all of the above. A PR with red CI will not be reviewed until it's green or you've explained why a failure is unrelated.

---

## Branch naming

Use a prefix so the GitHub branch list groups sensibly:

- `fix/<short-slug>` — bug fix (e.g. `fix/wsl-path-translation`)
- `feat/<short-slug>` — new feature (e.g. `feat/spotify-export`)
- `docs/<short-slug>` — docs only
- `chore/<short-slug>` — tooling, deps, CI
- `refactor/<short-slug>` — no behavior change

Keep slugs short and kebab-cased. The branch name is going to show up in the merge commit, so make it readable.

---

## Commit messages

[Conventional Commits](https://www.conventionalcommits.org/) is the suggested style — it makes the CHANGELOG easier to backfill and lets the release pipeline group entries automatically:

```
feat(organizer): bucket rare genres into Other/ at < 5 tracks
fix(wsl): handle drive letters with mixed case in path translation
docs(readme): freshen stats line
chore(deps): bump mutagen to 1.48
```

Scopes are optional but help. Keep the subject under 72 characters; put the rationale in the body if needed.

---

## Pull request checklist

The PR template will ask you to confirm these. Doing them ahead of time makes review fast:

- [ ] Tests added or updated for the change
- [ ] `pytest` and `npm test` are green locally
- [ ] `ruff check` and `tsc --noEmit` are clean
- [ ] Docs updated if behavior or CLI surface changed
- [ ] If you touched a Python dataclass: regenerated TS types via `python scripts/generate_ts_types.py`
- [ ] If you added an RPC method: registered it in `METHODS`, added cancellation if it's long-running, and used the typed wrapper in `ui/src/api/rpc.ts`
- [ ] For UI changes: screenshot or short clip in the PR description
- [ ] CHANGELOG entry under `[Unreleased]` for user-visible changes

---

## The Python ↔ TypeScript type bridge

Vibechek's UI talks to the Python sidecar over JSON-RPC. To keep the wire type-safe, Python dataclasses are mirrored as TypeScript interfaces. The bridge lives in:

- `scripts/generate_ts_types.py` — the generator
- `ui/src/types/generated.ts` — generated TS interfaces (do not hand-edit)
- `ui/src/lib/keeperConstants.ts` — generated shared constants
- `ui/src/api/rpc.ts` — the typed RPC wrapper that every UI call should go through

**To add a field to an existing dataclass:** add it in Python, run `python scripts/generate_ts_types.py`, commit both files. CI will fail if the generated files are stale.

**To extend the mapping (a new Python type kind, a new shared constant):** edit `scripts/generate_ts_types.py` — the docstring at the top walks through both extension points.

**To add a new RPC method** (end to end):

1. Implement the handler in `vibechek/rpc.py` — `def _my_method(params: dict) -> dict:`.
2. Register it in the `METHODS` dict. If it's long-running, route it through the
   cancellation singleton so it can be cancelled and so concurrent reads still interleave.
3. If it accepts or returns a dataclass, define it in the relevant `vibechek/` module and
   run `python scripts/generate_ts_types.py` to regenerate the TS interfaces.
4. Add a typed wrapper in `ui/src/api/rpc.ts` so the UI calls it type-safely.
5. Add a test. `tests/test_rpc_method_sync.py` cross-checks that every `METHODS` entry has
   a matching TS wrapper, so a missing wrapper fails CI.

Full walkthrough (wire format, error codes, checklist): [docs/CONTRACTS.md](docs/CONTRACTS.md).
The authoritative method list lives in [`vibechek/rpc.py`](vibechek/rpc.py).

---

## Where to look next

- [docs/CONTRACTS.md](docs/CONTRACTS.md) — adding a new RPC method end to end.
- [docs/MAINTAINERS.md](docs/MAINTAINERS.md) — bus-factor notes: release flow, architecture landmines, CI gotchas, WSL debugging.
- [docs/RELEASING.md](docs/RELEASING.md) — version bumps, tag flow, the opt-in code-signing setup.
- [docs/ROADMAP.md](docs/ROADMAP.md) — what's planned and what's deliberately out of scope.
- [docs/PROJECT_SUMMARY.md](docs/PROJECT_SUMMARY.md) — the high-level architecture + feature map.
- [ui/README.md](ui/README.md) — the desktop UI internals + sidecar protocol.

If you get stuck, open a [Discussion](https://github.com/captinjack99/Vibechek/discussions) or a draft PR with `WIP:` in the title. We'd rather help early than review a finished thing that took a wrong turn.

By contributing, you agree your contributions are licensed under AGPL-3.0-or-later (the same license as the rest of the project) and that you have followed the [Code of Conduct](CODE_OF_CONDUCT.md).
