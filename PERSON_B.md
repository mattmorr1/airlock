# Airlock — Person B: execution & evaluation plane

This branch implements **Person B** from the two-person split of
`airlock-evolve-architecture.md`: everything that actually runs code, freezes
it, tests it, composes it, and improves the harness. Person A (control plane:
plugin, MCP, API, scheduler, promotion pointer) is not in this branch.

## What Person B owns

| Area | Module |
|---|---|
| Managed worker launch (SDK + container), freeze-after-stop | `controller/workers.py` |
| Snapshot / manifest / digest / scope check | `controller/snapshots.py` |
| Immutable content-addressed artifact store (GridFS) | `controller/artifacts.py` |
| Harness versions (immutable config) | `controller/harness.py` |
| Validation runs (exact-input cache key, fail-closed) | `controller/validation.py` |
| Serial integration (3-way merge, fenced) | `controller/integration.py` |
| Trusted inspectors + execution orchestrator | `runner/inspectors.py`, `runner/execute.py` |
| Fixed scenarios + in-runner observation driver | `trusted-evals/` |
| Evolution: propose + paired evaluation | `evolution/propose.py`, `evolution/evaluate.py` |
| Job worker loop (claims jobs, dispatches handlers) | `controller/jobrunner.py` |
| Target application under test | `demo-app/` |

Shared contract (frozen, co-owned with A): `common/` and `mongo/setup.py`.

## The interface with Person A (the only coupling)

Communication is through Atlas documents on the `jobs` queue — no shared code
paths. A enqueues jobs and owns `tasks`/`delegations`/`jobs`/`events` and the
promotion pointer in `projects`. B consumes jobs and owns `worker_runs`,
`artifacts`, `validation_runs`, `integration_runs`, `harness_versions`, and the
evidence in `evolution_runs`.

Job kinds B handles: `launch`, `validate`, `integrate`, `evolve`
(payloads in `common/contracts.py`).

## Security posture (carried from the spec)

- Managed workers are separate SDK sessions in bounded containers — the real
  isolation. The `local` execution mode is DEV ONLY and is **not** a security
  boundary (labeled throughout).
- Worker output is frozen only after the process tree is stopped.
- `finished` ≠ accepted; pending validation ≠ pass; observer/capture failure ⇒
  `inconclusive`, never pass.
- The evolver only proposes within a fixed ceiling; the independent evaluator
  decides eligibility; it never promotes. Holdout labels are trusted inputs,
  not evolver-supplied.
- Artifacts are content-addressed and idempotent; validations are cache-keyed;
  integration is fenced — so at-least-once job delivery can't duplicate outputs.

## Run it

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

# Fast unit + runner tests (no database needed):
python -m pytest tests/ -q

# Full end-to-end loop through the job queue (needs a Mongo replica set):
export AIRLOCK_MONGO_URI="mongodb://127.0.0.1:27018/?replicaSet=rs0"
export AIRLOCK_EXECUTION_MODE=local
python mongo/setup.py            # create collections, indexes, validators
python scripts/demo_pipeline.py  # walks the spec section 14 acceptance checks
```

The demo's enqueue / project-registration / pointer-flip steps stand in for
Person A; everything they trigger is Person B's real code.

## Configuration (env, prefix `AIRLOCK_`)

- `MONGO_URI`, `DB_NAME` (default `airlock`), `GRIDFS_BUCKET`
- `EXECUTION_MODE` = `local` (dev) | `container` (real isolation)
- `RUNNER_IMAGE`, `WORKER_IMAGE`, `WORK_ROOT`
- `LEASE_TTL_SECONDS`, `INFERENCE_BASE_URL`, `WORKER_DEFAULT_MODEL`
