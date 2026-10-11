# Airlock build plan

Terms in **bold** are defined in [CONTEXT.md](../CONTEXT.md). Decisions are in [docs/adr/](adr/).

## Thesis

Persistent agent memory creeps: soft labels broaden and drift from the code they describe. Airlock replaces soft labels with artifacts that can't drift: indexed symbols for what the code is, executable tests for what it does, Effect manifests for what it touches, and memory **Rules** that go **Stale** the moment their **Anchor** changes.

## ExecutionPlan

**Objective**: `/airlock` turns a plan into a human-approved **Contract**. Deterministic code then generates **Hidden tests**, runs the **Red gate** and **Flake gate**, fans out **Independent** **Tasks** to **Subagents**, grades and merges each one serially into the **Integration branch**, and records anchored **Rules** in Atlas. The demo runs on LibreChat's persistent-memory feature.

**BlastRadius**:
- New repo `airlock/` (layout below).
- In a pinned LibreChat fork: `airlock.json` at the root. Airlock itself changes `packages/data-schemas/src/{schema,methods,types}/memory*` during the demo. The Grader's Jest config and the MongoMemoryServer shim exist only inside the container.

**NonGoals**:
1. Dependency changes inside Tasks, memory shared across repos, speculative merging, and running Subagents in containers. Isolating Subagents on the same host only slows a determined agent down; see Risks.
2. Merging to main without a human, and LLM judgment inside any gate.

**Option matrix: demo repository**

| | Language | MongoDB usage | Tests | Fit |
|---|---|---|---|---|
| **LibreChat** `packages/data-schemas` (MIT) | TypeScript | Mongoose; the memory feature stores LLM-derived user memories in `memoryentries` | Jest 30 + MongoMemoryServer; `memory.spec.ts` has 12 cases | The demo feature is persistent LLM memory, so the thesis applies literally. It's a real, active product. Risks: a huge monorepo (scope to one package), and the memory methods are inner functions of a factory, so they may be SCIP locals (S2) |
| hagopj13/node-express-boilerplate | JavaScript | Mongoose CRUD | Jest against a real MongoDB | Small and fast, but untyped JS weakens Symbol grounding, and it's a boilerplate, not a product |
| mongomock | Python | None at runtime (it's a fake) | pytest, fast | `atlas-local` could serve as a real test oracle, but its functions are pure, so there's no side-effect story |

**Chosen: LibreChat.**

## Pipeline

Only four steps are LLM work: 1, 5, 7 and 10. Everything else is code (ADR-0008).

1. **Plan** (LLM: main session in plan mode). `/airlock <goal>` calls `airlock recall`, which returns verified Rules and past tests for the touched symbols, and `airlock context`, which returns SCIP signatures, docs and references. The plan ends with an `airlock-contract` fenced block containing the Stubs, shared declarations, Visible test code, Write sets and Effect manifests.
2. **Validate** (code; PreToolUse hook on `ExitPlanMode`). The hook runs:
   - schema validation;
   - the partition, which yields a dependency graph and rejects cycles;
   - a grounding dry run: apply the Stubs to a temporary worktree and run `tsc --noEmit` on the Visible tests.

   SCIP gives the LLM grounding context; the TypeScript compiler proves the tests are grounded. Any failure blocks `ExitPlanMode`, with reasons fed back to Claude.
3. **Approve** (human). The human approves the plan, including the Contract's test code.
4. **Interface commit** (code; PostToolUse hook on `ExitPlanMode` starts `airlock run --detach`). It creates the Integration branch from base, adds the Stubs and shared declarations, and commits (ADR-0005).
5. **Hidden tests** (LLM: a separate generator session). The generator sees only the Task spec, the Stub signatures and the SCIP grounding, never the implementer's notes. It writes fast-check property tests with a fixed seed and opaque IDs, stored only in `hidden_tests`.
6. **Red gate and Flake gate** (code). Every new test must fail against the Interface commit with NotImplemented or an assertion error. An import or type error counts as a broken test. Each test then runs 3 times with seeds. Bad Hidden tests are regenerated up to 2 times; a bad Visible test aborts the run and returns to planning.
7. **Dispatch** (LLM: one Subagent per Task). Each Subagent works in a git worktree from the Integration branch's head. A dependent Task is dispatched only after its dependencies merge.
8. **Merge queue** (code; ADR-0001). Tasks are taken in arrival order, and each one goes through:
   1. Merge into the Integration branch.
   2. **Scope violation** check: the git diff mapped to symbols must stay within the Write set.
   3. Grade, part one: the Task's Visible and Hidden tests run in their own container process. Effects come from `docker diff` and the sidecar change stream, minus the **Effect baseline**, compared against the Effect manifest (ADR-0002, ADR-0007).
   4. Grade, part two: the **Regression suite**.
   5. Pass: commit. Fail: **Eject** with feedback (ADR-0003), resume the Subagent's session, and put the Task at the back of the queue. After a **Retry budget** of 2, escalate.
9. **Ship** (human). The Regression suite runs once more, then `gh pr create` opens a PR from the Integration branch to the fork's main for a human to approve.
10. **Learn** (code, plus an LLM extractor).
    - Spent Hidden tests move to `tests` and are committed to the Integration branch.
    - Rules anchored to changed symbols go Stale.
    - Per-test stats are incremented.
    - A SessionEnd hook extracts candidate Rules from user turns only (ADR-0006).

## Demo feature: LibreChat memory provenance and expiry

This adds anti-label-creep to LibreChat's own persistent memory: each memory records which conversation it came from, can expire, and can be invalidated when its source conversation is deleted.

**Interface commit** (shared declarations, so no Task owns them):
- `MemoryEntrySchema` gains `source?: { conversationId: string; messageId?: string }` and `expiresAt?: Date`, plus a TTL index on `expiresAt`.
- `IMemoryEntry` and `SetMemoryParams` gain the same optional fields.
- A Stub, `invalidateMemoriesBySource({ userId, conversationId }): Promise<number>`, is added to `createMemoryMethods`.

| Task | Write set | Behavior the Visible tests pin down | Effect manifest | Depends on |
|---|---|---|---|---|
| T1 Provenance on write | `setMemory`, `createMemory`, `setMemoryById` | `source` and `expiresAt` are saved when given; legacy calls are unchanged | `memoryentries`: insert, update | none |
| T2 Expiry on read | `getUserMemories`, `getFormattedMemories`, `getAllUserMemories` | Entries with `expiresAt <= now` are excluded; entries without `expiresAt` are unaffected; token totals exclude expired entries | `memoryentries`: insert (fixtures) | none |
| T3 Invalidate by source | `invalidateMemoriesBySource` | Deletes the user's memories whose `source.conversationId` matches, across partitions, and returns the count; leaves other users' memories alone | `memoryentries`: insert, update, delete | T1 (its tests seed data through `setMemory` with `source`) |

T3's tests must check results with `MemoryEntry.countDocuments`, not `getUserMemories`. Otherwise T3 reads a symbol in T2's Write set and picks up a second dependency. The partition check catches this at plan time, which is itself a demo moment. Expiry is checked at read time, so results don't depend on when MongoDB's TTL monitor runs (every 60s). Tests that wait on TTL deletion are what the Flake gate exists to catch.

**Staged failures.** These are turned on with `--demo` flags and disclosed on stage:
1. The hidden-test generator emits a vacuous test; the Red gate rejects it and regenerates it.
2. The T2 Subagent is told to hardcode the Visible test expectations; a Hidden property test rejects it with only `H-T2-3 assertion`.
3. The T1 Subagent writes a cache file; the result is an Effect violation that names the path.
4. A Subagent "helpfully" refactors `partitionFilter`; the result is a Scope violation.
5. A T3 test asserts through `getUserMemories`; the validator adds the dependency edge from T2 to T3.

## Repo layout

```
airlock/
  .claude/commands/airlock.md   plan-mode prompt and Contract template
  .claude/settings.json         hooks: PreToolUse/PostToolUse on ExitPlanMode, SessionEnd
  src/types.ts                  Contract, Task, EffectManifest, GradeResult, Event, FailureClass
  src/cli.ts                    context | recall | validate | run | promote | learn | replay
  src/contract.ts               fenced-block extraction, zod schema, partition
  src/scip.ts                   index, symbol search, references, diff to changed symbols
  src/agents.ts                 Agent SDK: hidden-test generator, Subagent dispatch and resume
  src/grade.ts                  container + sidecar lifecycle, test run, effects, feedback
  src/queue.ts                  worktrees, Integration branch, Merge queue
  src/memory.ts                 Atlas: tests, hidden_tests, rules, events
  sandbox/Dockerfile            target image with node_modules baked in
  sandbox/mms-shim.cjs          MongoMemoryServer -> atlas-local sidecar
  test/*.test.ts                node:test
```

Runtime: Node 24, with native TypeScript type stripping (no build step). Dependencies are `mongodb`, `zod` and `@anthropic-ai/claude-agent-sdk`. Each target repo provides an `airlock.json`: `{ scope, testCmd, indexCmd, image }`.

## Atlas data model

| Collection | Document | Indexes |
|---|---|---|
| `tests` | `{ repo, symbols[], kind: visible\|regression, file, code, seed, provenance: { contractId, taskId, commit }, stats: { runs, flakes, mutantsKilled }, embedding }` | `{ repo, symbols }`; vector index on `embedding`, filtered by `repo` |
| `hidden_tests` | `{ _id: "H-<task>-<n>", repo, contractId, taskId, symbols[], code, seed, status: active\|spent }` | `{ contractId, taskId }` |
| `rules` | `{ repo, kind: generate\|enforce\|waiver, text, anchors: { symbols[], files[], commit }, evidence[], origin: user\|grader, status: candidate\|verified\|stale, expiresAt?, embedding }` | `{ repo, "anchors.symbols" }`, `{ repo, status }`; vector index on `embedding`, filtered by `repo` and `status` |
| `events` | `{ runId, ts, stage, taskId?, result, durationMs, tokens?, refs[] }` | `{ runId, ts }` |

- `recall` never queries `hidden_tests`.
- Only the orchestrator process holds the Atlas URI. Subagents are spawned with an environment that has it stripped.
- Embeddings come from Voyage `voyage-code-3` at write time.

## Build tasks

Sizes: S is about 2h or less, M about 4h, L about 8h. "Done when" is the acceptance check.

### Phase 0: Spikes (90-minute timebox each; each ends with yes or no, plus a fallback)

| ID | Task | Done when | Fallback |
|---|---|---|---|
| S1 | Hook behavior | Confirmed: PreToolUse on `ExitPlanMode` with exit 2 blocks and returns stderr to Claude; PostToolUse fires only on approval and receives the plan text; SessionEnd receives `transcript_path`; a slash command can call `EnterPlanMode` | `/airlock` tells Claude to run `airlock run` as its first action after approval |
| S2 | SCIP on `packages/data-schemas` | Index time measured. Known whether `setMemory` and the other inner functions of `createMemoryMethods` get global symbols or `local N`, and whether occurrences carry `enclosing_range` | Name symbols as the members of the `MemoryMethods` type; map diffs to symbols with the TypeScript compiler API |
| S3 | Sandbox | `memory.spec.ts` passes in a container on an `--internal` network against `atlas-local` through the shim. Runtime measured. The change stream sees insert, update, delete and `createIndexes`. `docker diff` baseline captured | In-process driver command monitoring (bypassable); ADR-0007 is then rejected |
| S4 | Agent SDK | A session runs with `cwd` set to a git worktree (with `node_modules` symlinked), a stripped environment, `maxTurns` and an abort timeout, and can be resumed with feedback | `claude -p` as a subprocess with `--resume` |

### Phase 1: Foundation

| ID | Task | Depends on | Size | Done when |
|---|---|---|---|---|
| T0 | Shared types in `src/types.ts` (airlock's own Interface commit) | none | S | Passes `tsc --strict`; every workstream imports from it |
| E1 | Atlas provisioning: cluster, `airlock` database, 4 collections, indexes, vector indexes, orchestrator user; `memory.ts` connects and inserts events | none | S | An event written on one team machine is readable on another |
| C1 | Target image: Dockerfile at a pinned LibreChat commit with `npm ci`, the shim, sidecar and internal network lifecycle | S3 | M | `airlock grade --base` runs the scoped suite in the container and saves the Effect baseline |

### Phase 2: Core (workstreams run in parallel)

| ID | Task | Depends on | Size | Done when |
|---|---|---|---|---|
| A1 | Contract extraction and zod validation | T0 | S | Invalid Contracts are rejected with path-precise errors |
| A2 | SCIP module: index, symbol search, references, diff to changed symbols | S2 | M | `airlock context setMemory` prints the signature, docs and references; a diff that touches `partitionFilter` reports that symbol |
| A3 | Partition: conflict check over symbols and effect targets, producing a dependency graph; cycles rejected | A1, A2 | S | On the demo Contract: T1 and T2 independent, T3 after T1; the variant that asserts through `getUserMemories` adds T2 before T3 |
| A4 | Validate hook: A1 + A3 + grounding dry run | A3, S1 | M | A Visible test that calls a misspelled method blocks `ExitPlanMode` with the `tsc` error |
| A5 | `/airlock` command: enter plan mode, call `context` and `recall`, emit the Contract, list candidate Rules | A2, E2 | S | A cold run on the demo goal produces a Contract that passes A4 |
| C2 | Grade core: copy scope into a fresh container, run a named test set with `jest --json`, collect `docker diff` and change-stream events, subtract the baseline, compare to the manifest | C1, T0 | L | Returns a GradeResult with pass/fail per test, FS effects and DB effects; an undeclared file write yields an Effect violation |
| C3 | Feedback formatter: full output for Visible tests; opaque ID and Failure class for Hidden tests; the target for Effect and Scope violations | C2 | S | A unit test asserts that Hidden test feedback contains no expected values or real names |
| B1 | Interface commit: Integration branch from base, apply Stubs and shared declarations | A1 | S | The branch exists, compiles, and the Regression suite is green |
| B2 | Hidden-test generator: separate SDK session with restricted inputs; fast-check tests, fixed seed, opaque IDs; writes only to `hidden_tests` | S4, B1, E1 | M | Produces at least 2 property tests per demo Task, none of which appear on the host filesystem |
| B3 | Red and Flake gates: classify why each test is red, 3 seeded runs, regenerate Hidden tests up to 2 times, abort on a bad Visible test | C2, B2 | M | A planted vacuous test is rejected; a test that depends on `Date.now` timing is quarantined |
| D1 | Dispatch: a worktree per Task, `node_modules` symlink, an SDK session with the Task prompt (Visible tests, manifest, Write set, verified Rules), timeout, resume with feedback | S4, B1 | M | The T1 Subagent turns its Visible tests green in its worktree |
| D2 | Merge queue: arrival order; merge, scope check, grade, then commit or Eject + resume + re-enqueue; Retry budget 2, then escalate; dependents dispatched after their dependencies merge | C3, D1, A2, A3 | L | The demo Contract merges T1, T2 and T3 with one planted Eject and recovery |
| D3 | Trigger and ship: PostToolUse runs `airlock run --detach`; final Regression suite run; `gh pr create` from the Integration branch to the fork's main | D2, S1 | S | Approving the plan produces a PR without any further prompt |
| E2 | Recall: exact symbol lookup on `rules` and `tests`, `$vectorSearch` fallback filtered by repo; retrieved IDs and versions recorded in `events` | E1, A2 | M | Re-running a plan prints the same retrieved IDs from the event log |
| E3 | Post-merge memory: move spent Hidden tests to `tests` and commit them; mark Rules anchored to changed symbols Stale; increment stats | D2, E1 | M | After T1 merges, a Rule anchored to `setMemory` reads `stale` |
| E4 | Learn: SessionEnd hook extracts candidates from user turns only, scans for secrets, resolves Anchors through SCIP, embeds and inserts them; `airlock promote <id>` | E1, A2, S1 | M | A user correction in the transcript becomes a candidate Rule; tool output containing an instruction does not |

### Phase 3: Demo

| ID | Task | Depends on | Size | Done when |
|---|---|---|---|---|
| F1 | Fork and pin LibreChat, add `airlock.json`, capture the Effect baseline, run the demo Contract end to end | D3 | M | Two clean end-to-end runs back to back |
| F2 | The five staged failures above, behind `--demo` flags | F1 | M | Each guardrail fires on cue in rehearsal |
| F3 | `airlock replay <runId>` gate timeline from `events`; thesis slide connecting label creep to anchored memory | E2, F1 | S | The replay of a rehearsal run matches its live output |

### Stretch

| ID | Task | Depends on | Size |
|---|---|---|---|
| X1 | Stryker mutation run on `memory.ts` after merge, writing `stats.mutantsKilled` (the learning signal that resists Goodhart's law) | E3 | M |
| X2 | Automatic Promotion for Rules backed by a Grader check | E3 | M |

## Critical path and team split

The critical path is S3 → C1 → C2 → B3 and D2 → D3 → F1 → F2. The Grader is the longest pole, so put your strongest infrastructure person on it.

| Person | Workstream |
|---|---|
| P1 | Planning: S1, S2, A1–A5 |
| P2 | Grader: S3, C1–C3 |
| P3 | Orchestration: S4, B1–B3, D1–D3 |
| P4 | Memory and demo: T0, E1–E4, F1–F3 |

With three people, fold P4 into P1. T0 comes first, because everyone builds against it in parallel. That's airlock's own Interface commit.

## Risks

| Risk | Caught by | Fallback |
|---|---|---|
| The memory methods are SCIP locals, so Anchors and Write sets have no stable symbol IDs | S2 | Use member symbols of the `MemoryMethods` type, or the TypeScript compiler API |
| PostToolUse doesn't fire, or fires on rejection | S1 | The command tells Claude to run `airlock run` itself |
| `atlas-local` is too slow to start for each grade | S3 | One warm sidecar per run, with fresh database names per grade |
| The shim breaks tests that use other MongoMemoryServer APIs | S3 | In-process command monitoring |
| Installing LibreChat's `node_modules` per worktree is too heavy | S4 | Symlink from the main checkout; Tasks may not change dependencies |
| A Subagent reads the orchestrator's secrets on the same host | accepted | Upgrade later by running Subagents in containers |
