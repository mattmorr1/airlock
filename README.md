# Airlock: a self-improving harness for AI coding agents

Airlock runs delegated AI coding work as **untrusted** work. It freezes each
agent's output, tests it in isolation against checks the agent can't weaken,
merges sibling work without silent overwrites, and **improves its own test
harness** whenever a bug slips through. A new harness is adopted only after an
independent evaluator shows it on held-out cases.

> The agent ships the feature; the harness learns to test it.

Built for the Oct 2026 MongoDB hackathon. All state lives in **MongoDB**:
Airlock's tasks, jobs, evidence, artifacts, and harness history are on Atlas,
and the app under test runs on its own separate Mongo.

## The 20-second version

We ask agents for `PATCH /api/v1/items/bulk`, an atomic bulk update with
idempotency. The naive implementation updates Alice's item, reaches Bob's item,
returns `403`, and **leaves Alice's row already changed**. A status-code test
passes, but the database is corrupted.

| | Harness v1 (status codes) | Harness v2 (evolved) |
|---|---|---|
| Faulty code | **passes**: the bug escapes | **caught**: `rows_unchanged_on_rejection` fails, with before/after evidence |
| Repaired code | passes | passes |
| Escaped failures on the comparison set | 3 | **0**, with 0 legitimate runs blocked |

v2 wasn't written by hand. Airlock proposed it from the escape, checked it
against v1 on held-out cases, and promoted it only because it caught the bug
**without blocking correct work**.

## What you'll see

- **Claude Code front door.** `/airlock:run` plans the task. You approve the
  contract with `! airlock -y approve <task>`, and Airlock delegates the work
  while hooks keep the parent session from editing the repo underneath it.
- **Hatch, the statusline astronaut.** It reads real task state from
  `/v1/hud`: *thinking* while planning, *waiting on you* at approval, *alarm*
  when validation catches a bug, *success* when the work merges.
- **A live dashboard** (`:8099`), pushed by **MongoDB change streams**. Each
  attempt shows its frozen artifact, whether a scripted fixture or Claude wrote
  it, every check, the red before/after diff of the partial write, the repair,
  the integration, harness evolution (escaped 3 → 0), and the event stream.

Step-by-step demo with timings: **[DEMO.md](DEMO.md)**.

## Quickstart

Needs Python 3.13 + [uv](https://docs.astral.sh/uv/), an Atlas cluster, and a
local `mongod` for the target app (`brew tap mongodb/brew && brew install mongodb-community`).

```bash
cp .env.example .env              # set AIRLOCK_MONGO_URI (+ AIRLOCK_DB)
uv sync
uv run --env-file .env pytest -q  # 196 tests; DB tests use throwaway airlock_test_* databases
scripts/up.sh                     # target mongod, API :8000, job worker, dashboard :8099
uv run --env-file .env python scripts/run_via_api.py   # second terminal: plan → approve → delegate → caught → repaired → merged
```

`scripts/up.sh --real` swaps in the real Claude planner and real Claude Agent
SDK coding workers.

## What's real and what's scripted

We use stand-ins only where an LLM would make the demo nondeterministic. Each
one is labeled where it runs.

| Piece | Default demo (`up.sh`) | `up.sh --real` |
|---|---|---|
| Control API, job queue, leases, fencing, state machine, events | real | real |
| Freeze, content-addressed artifacts, scope check | real | real |
| Trusted runner: boots the app, runs scenarios, before/after DB inspection | real | real |
| Serial 3-way integration + final validation | real | real |
| Harness evolution + paired holdout evaluation + promotion | real | real |
| Planner | **stub**: fixed 1-assignment contract | Claude (Agent SDK), validated scope grammar + inferred edges |
| Coding worker | **scripted fixture**: attempt 1 faulty, repair fixed. The dashboard tags it `scripted stand-in` | Claude Agent SDK session with a scope-enforcing PreToolUse hook. Tagged `claude-sdk · turns · $cost` |

Why the default is scripted: a real coder may write correct code on the first
try, so "the bug gets caught" isn't guaranteed live. The real planner splits
this feature into about 4 dependent assignments. In `--real`, multi-assignment
serial integration is less rehearsed than the single-assignment path. Worker
isolation is the dev-only `local` mode; container isolation is designed in the
spec but not shipped.

## MongoDB features doing real work

| Feature | Used for | Where |
|---|---|---|
| Multi-document transactions | Every state transition, job completion, and event append commit together or not at all | `controller/scheduler.py`, `controller/states.py` |
| CAS on `revision` + lease fences | Stale workers can't complete; duplicate delivery can't double-apply | `controller/states.py`, `controller/scheduler.py` |
| Change streams | SSE doorbell for task/project events; the live dashboard | `controller/events.py`, `ui/server.py` |
| GridFS | Immutable, content-addressed artifacts (baselines, frozen worker output, merged trees) | `controller/artifacts.py` |
| JSON Schema validators + unique/partial indexes | Enum-checked states, dedupe keys, gapless per-stream event sequences, idempotent command receipts | `mongo/setup.py`, `api/idempotency.py` |
| Aggregation | HUD "today" totals, dashboard queue counts | `api/hooks.py`, `ui/server.py` |
| Atlas as the shared record | Provenance and harness history shared across teammates' machines | everything above |

## Architecture

```
Claude Code ──/airlock:run──▶ MCP bridge ──▶ Control API ──▶ jobs (Mongo, leased + fenced)
   ▲  hooks gate edits/Stop                      │                 │
   └── Hatch HUD ◀── /v1/hud                     ▼                 ▼
                                   plan → approve → delegate    job worker: launch → freeze → validate
                                                                  → repair? → integrate → final validate
                                   escape ─▶ evolve: propose stricter harness → paired holdout eval → promote
dashboard ◀── change streams ── Atlas (tasks, delegations, worker_runs, validation_runs, artifacts, harness_versions, events)
```

- **Control plane:** FastAPI API, durable job queue + scheduler, CAS state
  machine, LLM planner, events/SSE, promotion/rollback/revoke, and the Claude
  Code plugin + MCP bridge + hooks. (`api/`, `controller/{db,states,scheduler,planner,policies,events}.py`,
  `mongo/`, `cli/`, `plugin/`, `bridge/`)
- **Execution plane:** worker launch, freeze/manifest/digest, trusted runner
  with effect inspectors, serial 3-way integration, harness evolution with
  paired holdout evaluation. (`controller/{handlers,workers,snapshots,artifacts,integration,harness,validation}.py`,
  `runner/`, `evolution/`, `trusted-evals/`) See [PERSON_B.md](PERSON_B.md).
- **Target app:** [`target-app/`](target-app/README.md), a FastAPI + MongoDB
  items app. The feature variants are in `demo/bulk_feature/{faulty,fixed}/`.

### Safety properties

- *Finished* ≠ *accepted*: a delegation counts only after it's integrated and validated.
- Observer or capture failure → `inconclusive`, never `pass`.
- `airlock revoke` bumps the constraint epoch. Approvals and in-flight results
  from the old epoch are void.
- The evolver proposes; an independent evaluator decides eligibility. Promotion
  is a conditional pointer flip, and `airlock rollback` restores any previously
  validated version.

Spec: [airlock-evolve-architecture.md](airlock-evolve-architecture.md) ·
Glossary: [CONTEXT.md](CONTEXT.md) · Decisions: [docs/adr/](docs/adr/)

## Built during the event

All code in this repo: the controller, contracts and collection layouts,
execution plane, harness evolution, target app and bulk feature,
plugin/MCP/hooks, HUD, and dashboard. External platforms we build on: Claude
Code (hooks, plugins, statusline), the Claude Agent SDK, and MongoDB Atlas
(transactions, change streams, GridFS).
