---
status: accepted
---

# State machine: coarse task state, detail on children, completion in one transaction

The spec gives tasks ten states, including `awaiting_integration`, `validating`, and `repair_required` ([§7.2](../../airlock-evolve-architecture.md)). Delegations run concurrently, so those labels contradict each other: one delegation can be `repair_required` while a sibling is still running and a third is integrating, and a single enum on the task can't say all three.

## Decisions

- **Task states are coarse:** `draft → planned → approved → running → completed | failed | cancelled`.
  - The detail lives on the entity it describes.
  - Delegation states: `running → validating → integrating → accepted | repair_required`. A repair goes `repair_required → running`, with a new attempt. A delegation can also be `failed` or `cancelled`.
  - Integration runs carry their own state.
  - `GET /v1/tasks/{id}` returns the task together with its delegations.
  - Rejected: deriving a task state by precedence, which is lossy and adds write contention, and transitioning all ten spec states literally, which is wrong whenever siblings disagree.
- **Completion rule.** When an integrate job is enqueued, it's tagged `final` if every other delegation is already `accepted`.
  - Final validation runs the full task contract. If it passes, the task becomes `completed` with `final_artifact_id` set.
  - Earlier integrations validate against that stage's criteria. Running the full login contract against the schema step alone would fail, because the routes don't exist yet.
  - There's no separate final-validation step.
- **One table and one compare-and-set.** `ALLOWED: dict[Entity, dict[State, frozenset[State]]]` covers task, delegation, worker_run, integration_run, evolution_run, and job.
  - `transition()` runs `update_one({_id, revision: r, status: {$in: allowed_from}}, {$set: {status, ...}, $inc: {revision: 1}})`.
  - If it matches zero documents, it raises `Conflict`: a 409 `STALE_REVISION` over HTTP, or an aborted transaction inside the library.
  - No state-machine library.
- **Completion happens in one transaction.** `complete(job, result)` runs inside a single `with_transaction`:
  - it completes the job, guarded by `lease.owner` and `lease.fence` ([0004](0004-scheduler.md)),
  - applies the entity transitions,
  - appends the events, incrementing `event_seq` on the owner ([0006](0006-events-and-sse.md)),
  - and enqueues follow-on jobs.
  - Any exception leaves nothing partially written.
- **Validation acceptance checks the constraint epoch.** If the worker_run's pinned `constraint_epoch` no longer matches the project's, the result is recorded as `inconclusive` and never accepted ([0007](0007-promotion-and-policies.md)).
- **The delegate route takes no `expected_task_revision`.** With coarse task states, the guards that matter are the task status and dependency acceptance. Making the parent LLM track revisions adds nothing.

## Interface to B

- Report every result through `complete(job, result)`.
- Never write `status` or `revision` directly.
- Git, Docker, and GridFS work happens before `complete()`, idempotently, because a transaction can't cover it.

## Consequences

- The acceptance check for this work is a solo end-to-end test: a stub job-worker runs on the real jobs library with canned results for `plan`, `launch`, `validate`, and `integrate`. It drives plan → approve → delegate → schedule → events against `atlas-local` through `httpx.ASGITransport`, and asserts that the event sequence has no gaps.
