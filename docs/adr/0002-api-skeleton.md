---
status: accepted
---

# API skeleton: HTTP is for external actors, role tokens, receipts in jobs

[Spec §6](../../airlock-evolve-architecture.md) lists 17 routes, including ones that internal processes would call: a supervisor finishing a worker run, a controller queueing validation and integration runs. But the job-worker already holds Atlas credentials, since it polls `jobs`, so routing its results through HTTP adds a crash window without adding a trust boundary.

## Decisions

- **HTTP serves external actors only: the operator CLI, the MCP bridge, the hook adapter, and readers.** B's job-worker imports `controller/` and applies results through a library call, `complete(job, result)` ([0005](0005-state-machine.md)).
  - Reporting results over HTTP was rejected. With two hops, a crash between "job done" and "state advanced" leaves a completed job that never transitions, and recovering from it needs a reconciler.
  - An API-side loop that consumes job results was rejected because it adds polling lag and one more loop.
  - These spec routes become library calls: `/delegations/{id}/launch`, `/worker-runs/{id}/finish`, `/validation-runs`, `/integration-runs`, `/evolution-runs`, and `/evolution-runs/{id}/promote`.
- **Final routes:**
  - `POST /v1/projects`
  - `POST /v1/tasks`, which returns `202` and a `plan` job id ([0003](0003-planner.md))
  - `POST /v1/tasks/{id}/approve`
  - `GET /v1/tasks/{id}`
  - `POST /v1/tasks/{id}/delegations` ([0004](0004-scheduler.md))
  - `GET /v1/tasks/{id}/events` and `GET /v1/projects/{id}/events` ([0006](0006-events-and-sse.md))
  - `POST /v1/projects/{id}/rollback` and `POST /v1/projects/{id}/revoke` ([0007](0007-promotion-and-policies.md))
  - `POST /v1/hooks/pre-tool-use` and `POST /v1/hooks/lifecycle` ([0008](0008-plugin-and-mcp.md))
  - `GET /v1/jobs/{id}`
  - Publication is unchanged from the spec.
- **Auth is a static bearer token per role (`operator`, `bridge`, `hook`, `reader`).** Tokens are compared with `hmac.compare_digest` and mapped to capabilities through one FastAPI dependency.
  - The operator token lives in `~/.airlock/operator.token`. Only `cli/airlock.py` reads it, and a Claude Code `permissions.deny` rule on that path slows the parent session down.
  - `/approve` requires the `contract_digest` the human reviewed and returns 409 if it doesn't match. That digest binding is the guarantee that actually holds.
- **Accepted risk:** the bridge and hook run inside Claude Code's process tree, so on a single host the parent LLM can read any token and call `curl`. A token scheme can't prevent that, so we don't claim it does.
- **Command receipts live in `jobs`, as the spec says.**
  - A single helper, `idempotent(op, key, digest, fn)`, inserts a receipt and performs the mutation in one transaction.
  - A receipt has `kind: command_receipt`, `status: completed`, and `dedupe_key = receipt:{project}:{actor}:{op}:{key}`.
  - A duplicate request with the same key returns the cached result. A different digest returns 409. A duplicate that arrives while the first is still uncommitted returns 409 `IN_PROGRESS` with `retryable: true`.
  - Per-route idempotency was rejected because each route would need its own reasoning.
  - Receipts have no TTL; that's fine at demo scale.
- **Error envelope** as in the spec. Codes: `BASE_MISMATCH`, `DEPENDENCY_UNMET`, `CAPACITY` (retryable), `IN_PROGRESS` (retryable), `REAPPROVAL_REQUIRED`, `STALE_REVISION`. Status codes: 409 for stale state and unmet prerequisites, 403 for actor or scope violations, 422 for malformed bodies.

## Interface to B

- The job-worker never calls the HTTP API.
- A receipt is never `queued`, so claiming on `status: queued` already skips receipts.

## Consequences

- Our authorization is per role and per project, and it's enforced in the API, not by MongoDB roles. Collection-scoped credentials are a later upgrade.
