# Airlock — session handoff log

Pick-up notes for resuming work. Branch: **`person-b-execution`** (pushed to `origin`).

## What this is
Airlock = a self-improving agent harness. Person A built the **control plane**
(API, scheduler, state machine, planner, events, promotion, **Claude Code
plugin + MCP + hooks**). Person B (this branch) built the **execution plane**
(sandbox + trusted judge: worker launch, freeze, validate, 3-way integrate,
harness evolution) and **reconciled it onto A's seam**.

Demo: delegate "atomic bulk item updates with idempotency" on a MongoDB
`target-app`. The planted bug is a **partial write** — a rejected mixed-owner
batch still changes a row. Harness v1 (status-only) misses it; an independent
probe finds it; the harness evolves to v2 (`rows_unchanged_on_rejection` +
`idempotency_semantics`), is promoted, and the repaired code passes.

## Current state (verified working)
- B's engine reconciled onto A's async seam: handlers in `controller/handlers.py`
  plug into A's `run_worker`/`complete`/`transition`/`on_validation_pass`/
  `on_integration_pass`/`promote`. Worker entrypoint: `controller/worker_main.py`
  (handles plan/launch/validate/integrate/evolve).
- Full flow proven end-to-end on Atlas via A's real HTTP API: plan → approve →
  delegate → launch → validate (faulty caught under v2) → repair → integrate →
  completed; and separately evolve → promote (escaped 3→0).
- 17 B unit tests pass (`uv run pytest tests/test_hashing.py tests/test_inspectors.py
  tests/test_snapshots.py tests/test_compose.py tests/test_runner_suite.py`).

## Environment / config (dev)
- Python venv at `.venv` (`source .venv/bin/activate`). `claude-agent-sdk`,
  `mcp`, pymongo, fastapi, httpx, certifi installed.
- **Airlock control plane → Atlas** via `AIRLOCK_MONGO_URI` (the srv URI; kept in
  `.env` / chat, NOT in git). Demo DB name: `AIRLOCK_DB=airlock_plugin`.
- **Target app → separate Mongo** `TARGET_MONGO_URI=mongodb://127.0.0.1:27018/?replicaSet=rs0`
  (local single-node replica set; data in `/tmp/airlock-mongo/db`).
- Dev API tokens (local only, not secrets): `AIRLOCK_TOKEN_OPERATOR=op-secret`,
  `AIRLOCK_TOKEN_BRIDGE=bridge-secret`, `AIRLOCK_TOKEN_HOOK=hook-secret`,
  `AIRLOCK_TOKEN_READER=reader-secret`. Also set in `~/.claude/settings.json`
  `env` (backup at `~/.claude/settings.json.bak`).
- Worker demo flags: `AIRLOCK_STUB_PLAN=1` (deterministic planner, no SDK),
  `AIRLOCK_DEV_FIXTURE=auto` (attempt 1 faulty → repair attempt fixed).
- ⚠️ **Rotate the Atlas password after the event** — it was pasted in chat.

## Ready-made demo project (in `airlock_plugin`)
- `PROJECT_ID = proj_6acc0fa61fdb65ed52709aeb`
- `BASE_ARTIFACT_ID = art_281598a77d4f`

## Restart the backend (4 services) — use `uv` (reliable; manages deps from pyproject)
```bash
cd /Users/sumanth/Projects/mongohack
uv sync                              # once, installs all deps incl certifi/mcp/claude-agent-sdk
set -a && source .env && set +a      # loads Atlas URI, AIRLOCK_DB, tokens, demo flags

# target Mongo replica set (only if not already running):
pgrep -f "mongod.*27018" || mongod --dbpath /tmp/airlock-mongo/db --port 27018 --replSet rs0 --bind_ip 127.0.0.1 &
# if /tmp was cleared on reboot, re-init once:
#   mongosh --port 27018 --eval 'rs.initiate({_id:"rs0",members:[{_id:0,host:"127.0.0.1:27018"}]})'

uv run python -m uvicorn api.main:app --port 8000 &   # control plane API
uv run python -m controller.worker_main &              # B worker (A's run_worker + B handlers)
uv run python -m uvicorn ui.server:app --port 8099 &   # read-only dashboard
```
Health: API `curl -s -o /dev/null -w '%{http_code}' :8000/v1/tasks/x` → 401;
UI `:8099/healthz` → ok; worker `pgrep -f controller.worker_main`.
(The `.venv/` pip env is inconsistent — prefer `uv run` everywhere.)

## Ways to run it
1. **Plugin in Claude Code** (the "front door"): install via
   `/plugin marketplace add /Users/sumanth/Projects/mongohack` →
   `/plugin install airlock@mongohack` → `/reload-plugins` → restart Claude Code →
   `/airlock:run <PROJECT_ID> <BASE_ARTIFACT_ID> <request>` → approve in the prompt.
   Full detailed steps are in the chat (and summarized below).
2. **Through A's HTTP API (plugin's exact calls):**
   `uv run python scripts/run_via_api.py` (bootstraps a project + drives plan→approve→
   delegate→complete). Opens the UI.
3. **Live worker daemon + driver:** start services above, then
   `uv run python scripts/run_fullflow.py` (seeds + enqueues; the running worker
   processes; polls to promotion).
4. **Seam test (B handlers on A's real machine, throwaway Atlas db):**
   `AIRLOCK_DB=airlock_seamtest uv run python scripts/seam_test.py`.
5. **Monitor only:** `uv run python scripts/monitor.py <PROJECT_ID>` (CLI), or the web
   UI at `:8099`.

## Approve a planned task (operator, non-interactive)
```bash
uv run python -c "import httpx;h={'Authorization':'Bearer op-secret'};t=httpx.get('http://127.0.0.1:8000/v1/tasks/<TASK_ID>',headers=h).json()['task'];print(httpx.post('http://127.0.0.1:8000/v1/tasks/<TASK_ID>/approve',headers={**h,'Idempotency-Key':'ap1'},json={'contract_digest':t['contract_digest']}).text)"
```

## Key files
- Reconciliation bridge: `controller/handlers.py`, `controller/worker_main.py`
- B engine: `controller/{artifacts,snapshots,integration,harness,validation,workers,dev_fixtures}.py`,
  `runner/{execute,inspectors,bulk_execute,bulk_inspectors}.py`,
  `evolution/{propose,evaluate}.py`, `trusted-evals/`, `common/`
- Target app + feature: `target-app/`, `demo/bulk_feature/{faulty,fixed}/app/bulk.py`
- A's control plane: `api/`, `controller/{db,states,scheduler,planner,policies,events}.py`,
  `mongo/setup.py`, `cli/`, plus `plugin/`, `bridge/`, `.claude-plugin/marketplace.json`
- UI: `ui/server.py`; scripts: `scripts/{run_via_api,run_fullflow,seam_test,demo_bulk_pipeline,monitor}.py`
- Docs: `PERSON_B.md`, `airlock-evolve-architecture.md`, `CONTEXT.md`, `docs/adr/`

## Open items / next steps
- **Real LLM mode:** restart the worker WITHOUT `AIRLOCK_STUB_PLAN`/
  `AIRLOCK_DEV_FIXTURE` to use the real planner + real coding sub-agent
  (`claude-agent-sdk` + your `claude` auth). A real coder may write a correct
  impl first try, so keep stub/auto for a reliable "caught" demo.
- **Coordination w/ A:** the single job-worker now registers A's PLAN handler
  (via B's `handle_plan`) alongside B's four — confirm A is OK with B's worker
  owning PLAN, or move PLAN to a shved process.
- **`origin/hud-sprite`** branch adds a cosmetic statusline astronaut + `/v1/hud`
  (not merged). Merge only if wanted.
- **Multi-delegation serial integration:** `_integration_done` unparks the next
  integrate on failure; confirm success-path unpark for >1 delegation (demo uses
  a single `bulk` assignment, so untested here).
- Consider a project-scoped `.claude/settings.json` so plugin env travels with
  the repo instead of the global file.
