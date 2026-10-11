# Airlock

A controller for delegated coding work: it plans a task, launches bounded workers, validates and integrates their frozen output, and evolves its own harness only on independent evidence. Spec: [airlock-evolve-architecture.md](airlock-evolve-architecture.md). Decisions: [docs/adr/](docs/adr/).

## Language

### Planning

**Project**:
One registered repository with its baseline artifact, active harness pointer, and constraint epoch.

**Task**:
One user request and everything done for it, from plan to verified output.
_Avoid_: job, ticket. A Task is never one worker's unit; that is a Delegation.

**Contract**:
The controller-generated, operator-approved plan for a Task: constraints, acceptance criteria, interfaces, Assignments, and Inferred edges, identified by its digest.
_Avoid_: spec, plan (the plan is the draft; the Contract is what is approved)

**Assignment**:
One keyed unit of work inside a Contract, with its File scope and `depends_on`.

**File scope**:
The exact paths and `dir/**` prefixes an Assignment may change.

**Inferred edge**:
A dependency the planner adds between two Assignments whose File scopes overlap, shown in the approved Contract.

### Execution

**Parent session**:
The Claude Code session that requested the Task and launches its Delegations.

**Delegation**:
The live execution of one Assignment, across all its attempts.
_Avoid_: subtask, subagent

**Worker run**:
One attempt of a Delegation: one container, one SDK session, one pinned harness version and epoch.

**Artifact**:
An immutable, hash-identified snapshot: baseline, frozen worker output, composed tree, scenario, or trace.

**Stage validation**:
Validation of a non-final integration against that stage's criteria.

**Final validation**:
Validation of the integration that completes the last Delegation, against the full Contract.

**Integration run**:
The serial composition of one candidate onto the Task's integrated tree, followed by validation.

**Integration fence**:
The per-Task token that makes integration single-writer.

### Harness

**Harness version**:
An immutable executable configuration for context, tools, and validation.

**Active harness pointer**:
`projects.active_harness_version_id`; the only thing Promotion and Rollback change.

**Evolution run**:
One proposal of a candidate Harness version and its paired evaluation against the active one.

**Promotion**:
The conditional pointer flip to an eligible candidate, rejected if the baseline or epoch moved.

**Rollback**:
Pointing back to a version in `validated_harness_ids`.

**Constraint epoch**:
A per-Project counter; any approval, pinned run, or eligibility decision from an older epoch is void.

**Revocation**:
An operator action that increments the Constraint epoch.

### Control

**Job**:
A durable, leased work item in `jobs` (`plan`, `launch`, `validate`, `integrate`, `evolve`).

**Lease**:
A Job's time-bounded ownership, renewed by heartbeat and reclaimable after expiry.

**Fence**:
The token incremented on every claim; completion from an older fence is rejected.

**Command receipt**:
The stored result of an idempotent HTTP mutation, keyed by actor, operation, and Idempotency-Key.

**Event stream**:
The gapless, per-Task or per-Project sequence of events, keyed by `stream_id`.

**Operator**:
The human who approves Contracts, revokes, and rolls back, through the CLI only.

## Relationships

- A **Project** has many **Tasks**, many **Harness versions**, and exactly one **Active harness pointer**
- A **Task** has exactly one approved **Contract** and one **Event stream**
- A **Contract** has one or more **Assignments**; each **Assignment** becomes at most one **Delegation**
- A **Delegation** has one or more **Worker runs** (at most two attempts) and is accepted only through an **Integration run**
- A **Delegation** may launch only after every Delegation it depends on is accepted
- A **Worker run** pins one **Harness version** and one **Constraint epoch**
- An **Evolution run** compares one candidate **Harness version** with the active one and ends in **Promotion** or rejection

## Example dialogue

> **Dev:** "The service worker says it's done. Can routes start?"
> **Domain expert:** "Routes doesn't depend on service, only on schema. And 'done' means the Worker run is finished, which only means its output is frozen. A dependent can launch only once the Delegation it waits on is accepted, meaning integrated and validated."
> **Dev:** "We promoted hv_002 while service was running. Does service switch?"
> **Domain expert:** "No. Its Worker run pinned hv_001. The next Delegation picks up hv_002. If the operator had revoked instead, the epoch would move and service's result would come back inconclusive."

## Flagged ambiguities

- "task": resolved. A Task is the whole request; one worker's unit is a Delegation.
- "finished vs accepted": resolved. Finished means output frozen; accepted means integrated and validated.
- "delegate": resolved. Delegating creates the Delegation and launches its first Worker run in one call.
