# Airlock — a self-improving agent harness

Airlock is a controller for delegated AI coding work. It treats coding agents as
**untrusted**: it freezes their output, runs it in isolation against independent
checks they can't weaken, composes sibling work without silent overwrites — and
it **improves its own testing harness** whenever a failure escapes, proving the
improvement on held-out evidence before adopting it.

> The agent ships the feature; the harness learns to test it.

Built for the Oct 2026 MongoDB hackathon. Everything runs on **MongoDB** —
Airlock's own state, evidence, harness versions, and promotion history on an
Atlas cluster, and the target app under test on its own separate Mongo.

## The idea in one picture

```
you type a request in Claude Code
  → plan (LLM)  → operator approves the contract
  → delegate assignments → isolated workers write code
  → FREEZE each output → TEST it in a sandbox against trusted invariants
  → compose + re-test → verified result
        └─ a failure escaped?  → propose a stricter harness
                               → paired evaluation on held-out cases
                               → promote only if it catches the bug
                                 without failing legitimate work
```

Two loops: the **application loop** (code → freeze → validate → repair →
integrate) and the **harness loop** (escape → hypothesis → candidate harness →
paired evaluation → promote/rollback). The thing that self-improves is the
**test policy**, judged by an **independent evaluator it cannot game**.

## The demo

Target: a MongoDB items app where users own items. Feature delegated to Airlock:
`PATCH /api/v1/items/bulk` — atomic bulk update with idempotency.

The planted bug is a **partial write**: a naive implementation updates Alice's
item, then hits Bob's item and returns `403` — but Alice's row is already
changed. A status-only test passes; the database is corrupted. Harness **v1**
(success + status codes) misses it. An independent probe (a mixed-owner batch +
a before/after DB snapshot) finds it. Airlock evolves to **v2** — adds the
mixed-owner scenario and the `rows_unchanged_on_rejection` invariant — proves it
on fresh cases, promotes it, and the repaired code passes.

## Architecture

- **Control plane (Person A):** FastAPI control API, durable job queue +
  scheduler, coarse state machine with CAS + fencing, LLM planner, events/SSE,
  promotion/rollback, and the **Claude Code plugin + MCP bridge + hooks** that
  drive it from the editor. (`api/`, `controller/{db,states,scheduler,planner,policies,events}.py`,
  `mongo/`, `cli/`, `plugin/`, `bridge/`.)
- **Execution plane (Person B):** the sandbox + trusted judge — bounded worker
  launch, freeze/manifest/digest into content-addressed artifacts, a trusted
  runner with effect inspectors, serial 3-way integration, and harness
  evolution with paired holdout evaluation. (`controller/{handlers,workers,snapshots,artifacts,integration,harness,validation}.py`,
  `runner/`, `evolution/`, `trusted-evals/`, `common/`.) See [PERSON_B.md](PERSON_B.md).
- **Target app:** [`target-app/`](target-app/README.md) — purpose-built FastAPI +
  MongoDB app (users + items with `owner_id`). The bulk feature variants live in
  `demo/bulk_feature/{faulty,fixed}/`.

Design spec: [airlock-evolve-architecture.md](airlock-evolve-architecture.md).
Glossary: [CONTEXT.md](CONTEXT.md). Decisions: [docs/adr/](docs/adr/).

## Quickstart (local)

Requires a MongoDB Atlas cluster (set `AIRLOCK_MONGO_URI`) and a local Mongo
replica set for the target app.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt        # or: uv sync

# fast unit + runner tests (no cloud needed):
python -m pytest tests/ -q

# end-to-end through the real control-plane API (bootstrap + plan→approve→delegate):
export AIRLOCK_MONGO_URI='<atlas srv uri>' AIRLOCK_DB=airlock_plugin
export TARGET_MONGO_URI='mongodb://127.0.0.1:27018/?replicaSet=rs0'
export AIRLOCK_EXECUTION_MODE=local
export AIRLOCK_TOKEN_OPERATOR=op-secret AIRLOCK_TOKEN_BRIDGE=bridge-secret AIRLOCK_TOKEN_HOOK=hook-secret
export AIRLOCK_STUB_PLAN=1 AIRLOCK_DEV_FIXTURE=auto
mongod --dbpath /tmp/airlock-mongo/db --port 27018 --replSet rs0 --bind_ip 127.0.0.1 &
python -m uvicorn api.main:app --port 8000 &
python -m controller.worker_main &
python -m uvicorn ui.server:app --port 8099 &     # dashboard at http://127.0.0.1:8099
python scripts/run_via_api.py
```

To drive it from Claude Code with the plugin, and to resume a session, see
[SESSION_LOG.md](SESSION_LOG.md).

## What the team built during the event

The Airlock controller, its contracts and collection layouts, the execution
plane, the harness-evolution mechanism, the target app and bulk feature, the
plugin/MCP/hooks, and the dashboard. The platform capabilities it stands on
(Claude Code hooks/subagents, the Claude Agent SDK, MongoDB transactions/change
streams/GridFS, Docker) are external and referenced in the design spec.
