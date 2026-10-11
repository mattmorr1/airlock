# Airlock

Test-first multi-agent development: a human approves an executable Contract, parallel Subagents implement it, and deterministic gates decide what merges.

## Language

### Planning

**Contract**:
The human-approved executable output of planning: Stubs, Visible tests, Effect manifests, and Write sets for every Task.
_Avoid_: spec, plan (the plan is prose; the Contract is what gets approved)

**Task**:
One unit of Contract work assigned to exactly one Subagent.
_Avoid_: job, ticket, subtask

**Interface commit**:
The commit that adds every Stub and every shared declaration (type members, schema fields) the Contract needs, made before any test is generated.
_Avoid_: scaffold

**Stub**:
A typed declaration of a new symbol whose body only signals not-implemented.

**Symbol grounding**:
Resolving every function a test calls to an exact indexed symbol instead of a guessed name.
_Avoid_: test shape

**Write set**:
The symbols and effect targets a Task may create or modify.

**Read set**:
The symbols a Task's code and tests directly reference and the effect targets they depend on.

**Independent**:
Two Tasks where neither's Write set intersects the other's Read or Write set.
_Avoid_: non-conflicting, parallelizable

### Tests

**Visible test**:
A test the implementing Subagent can read and run.

**Hidden test**:
A test only the Grader can read, used to gate exactly one merge.
_Avoid_: secret test, private test

**Regression suite**:
The repository's existing tests plus every spent Hidden test, all of which must stay green.
_Avoid_: pass-to-pass suite

**Red gate**:
The check that every new test fails against the Stubs for the intended reason and the Regression suite still passes.

**Flake gate**:
The check that repeated seeded runs of a test always produce the same result.

**Failure class**:
The coarse category of a Hidden test failure (assertion, exception, or timeout).

### Execution

**Subagent**:
An implementing agent working in its own worktree on exactly one Task.

**Grader**:
The deterministic process that runs a Task's tests in a sandbox and decides pass or fail.
_Avoid_: judge, reviewer

**Effect manifest**:
The side effects a Task's test process is declared to produce: files, database collections, network hosts, environment.

**Effect baseline**:
The side effects observed when the unchanged suite runs at the base commit, ignored when checking manifests.

**Effect violation**:
An observed side effect that is absent from both the Task's Effect manifest and the Effect baseline.

**Scope violation**:
A change to a symbol outside the Task's Write set.

**Integration branch**:
The branch that collects one Contract's merged Tasks until a human approves it into main.

**Merge queue**:
The strictly serial line in which graded Tasks are merged into the Integration branch and re-tested one at a time.

**Eject**:
To remove a failing Task from the Merge queue and return it to its Subagent.

**Retry budget**:
The number of Ejects a Task may take before it escalates to the human.

### Memory

**Rule**:
A stored, repository-specific instruction about how tests are written, enforced, or exempted.

**Anchor**:
The indexed symbol or file, plus commit, that a Rule is bound to.

**Stale**:
The state of a Rule whose Anchor has changed since the Rule was verified.

**Waiver**:
A Rule that exempts a Task or symbol from a gate, always with an expiry.
_Avoid_: exception, skip

**Promotion**:
Moving a Rule from candidate to verified, by an automated check or human approval only.

## Relationships

- A **Contract** has one or more **Tasks** and exactly one **Integration branch**
- A **Task** has exactly one **Effect manifest**, one **Write set**, one **Read set**, and at least one **Visible test** and one **Hidden test**
- A **Task** is assigned to exactly one **Subagent**
- Two **Tasks** that aren't **Independent** are ordered; the later one is dispatched only after the earlier one merges
- Only the **Grader** reads **Hidden tests**
- A **Hidden test** gates exactly one merge, then joins the **Regression suite**
- A **Rule** has one or more **Anchors**, and becomes **Stale** when any Anchor changes

## Example dialogue

> **Dev:** "Tasks 1 and 2 both call `parseOrder`. Can they run in parallel?"
> **Domain expert:** "Only if neither has `parseOrder` in its **Write set**. Both reading it is fine; if Task 1 modifies it, they aren't **Independent**, and Task 2 waits for Task 1 to merge."
> **Dev:** "Task 2 also needs a new field on the order schema. Whose Write set gets it?"
> **Domain expert:** "Neither. Shared declarations go in the **Interface commit**, so both Tasks only write function bodies."
> **Dev:** "After Task 1 merges, does its **Hidden test** stay hidden?"
> **Domain expert:** "No. A **Hidden test** gates one merge, then it's spent and joins the **Regression suite**."

## Flagged ambiguities

- "deterministic test outcomes vs rough test shapes": resolved. The index provides **Symbol grounding** (which functions a test calls); expected values come only from the **Contract**.
- "approved and merged into main": resolved. **Tasks** merge into the **Integration branch**; only a human moves it to main.
- "m2/m3": resolved. Memory holds tests, outcomes, and anchored **Rules**; transcripts contribute only candidate **Rules** drawn from the user's own turns.
