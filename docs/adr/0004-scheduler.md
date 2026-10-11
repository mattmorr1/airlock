---
status: accepted
---

# Scheduler: parent-launched delegations, automatic integration, leases reclaimed at claim time

The spec gives the parent session `airlock_delegate` and `airlock_integrate`, which makes it parent-driven. It also says dependencies are satisfied only by integrated outputs the controller has accepted. Jobs are leased with fencing tokens, but the spec leaves the reclaim and polling mechanics open ([§7.10](../../airlock-evolve-architecture.md)).

## Decisions

- **The parent LLM launches delegations.** `airlock_delegate` maps to `POST /v1/tasks/{id}/delegations {assignment_key}`, which creates and launches in one transaction. Within that transaction it:
  - Requires the task to be `approved` or `running`.
  - Requires the key to exist in the approved contract. The caller can't override scope, objective, or profile.
  - Requires every `depends_on` delegation to be `accepted`. Otherwise it returns 409 `DEPENDENCY_UNMET`.
  - Requires the approval epoch to equal `projects.constraint_epoch`. Otherwise it returns 409 `REAPPROVAL_REQUIRED`.
  - Enforces `max_parallel_workers` from the harness config, returning 409 `CAPACITY` (retryable) when full.
  - Sets `resolved_input_artifact_id` to the latest integrated artifact.
  - Pins the currently active harness version.
  - Inserts the delegation, a `queued` worker_run, a `launch` job, and an event.
  - Rejected: a reconcile step inside the transaction that launches automatically, and a polling scheduler loop.
- **A repair is the same call.** It creates `attempt` + 1. After two attempts the delegation moves to `failed`, per spec §11.
- **Integration is automatic, one candidate at a time.** A passing validation for a delegation enqueues an `integrate` job in the same transaction. Integrations for a task run one after another under `integration_fence`.
  - A job enqueued while an earlier one is unfinished is parked (`available_at = PARKED`); completing or exhausting an integrate job releases the next.
  - Finality is decided when the job runs, not when it's enqueued ([0005](0005-state-machine.md)).
  - `airlock_integrate` is dropped. If the parent forgets to integrate, dependents can never launch.
  - Batching sibling candidates was rejected. It saves one validation cycle, which isn't worth a stall.
- **Leases are reclaimed at claim time.** `claim()` is one `find_one_and_update` with the filter `{$or: [{status: queued, available_at <= now}, {status: running, "lease.expires_at" < now}]}`, sorted by `available_at`. It sets the owner and expiry and increments `lease.fence` and `attempts` with `$inc`.
  - Leases last 30 seconds. Heartbeats run every 10 seconds, and workers poll every second.
  - `complete()` and `fail()` match on both the owner and the fence.
  - `fail()` sets `available_at = now + uniform(0, min(60, 2 * 2**attempts))` seconds (full jitter). After 3 attempts the job becomes `failed`, and in the same transaction its owner moves: `plan` fails the task, `launch` fails the worker run and delegation, `validate` (recorded `inconclusive`) and `integrate` send the delegation to `repair_required`.
  - A separate reaper loop was rejected because it adds a second loop and a recovery delay.
  - A change-stream wakeup on `jobs` was rejected because it saves under a second, and the spec says to poll.

## Interface to B

- Use `claim`, `heartbeat`, `complete`, and `fail` from A's jobs library.
- Run the heartbeat as its own asyncio task, never inline between SDK messages, because a long tool call would starve it.
- If a heartbeat matches zero documents, the lease is lost. Kill the container or SDK session right away and discard the result.
- The dispatch kinds are `plan`, `launch`, `validate`, `integrate`, and `evolve`.

## Consequences

- Progress depends on the parent session staying alive and being re-prompted. The blocking `Stop` hook in [0008](0008-plugin-and-mcp.md) exists for this reason.
- Delivery is at-least-once, so job handlers must be idempotent. Model inference can run twice after an ambiguous crash, and that case is recorded rather than hidden.
