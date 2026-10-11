---
status: accepted
---

# Planner: controller-side read-only LLM session, restricted scope grammar, inferred edges

The spec disagrees with itself about who writes the plan. [§6](../../airlock-evolve-architecture.md) has `POST /v1/tasks` take a full `plan`, while the `airlock_plan` tool in §5 takes only a request and returns a draft contract. The spec also says siblings run concurrently only "if their file scopes are independent", but never defines what independent means for two globs.

## Decisions

- **The controller generates the plan. The parent session does not.**
  - `POST /v1/tasks {project_id, parent_session_id, request, base_artifact_id}` creates the task in `draft`, enqueues a `plan` job, and returns `202`.
  - The `plan` handler runs a Claude Agent SDK session with structured output generated from the Pydantic plan model, then validates the result again. Structured output constrains the shape, not the meaning.
  - On success the task moves to `planned`. If validation fails, the errors go back to the planner for one re-plan. A second failure moves the task to `failed`.
  - The parent session never writes the contract, so "an LLM-generated plan cannot grant new privileges" holds against the parent too.
  - Parent-authored plans and a dual path were rejected.
- **The planner session's limits:**
  - Tools are `Read`, `Glob`, and `Grep`, with no Bash.
  - `cwd` is a controller-owned checkout of `base_artifact_id`.
  - `max_turns` is 15, with a 180-second timeout.
  - It runs on the host. The spec allows that for read-only profiles without Bash.
  - A tool-less single turn was rejected because the planner would be blind to files outside the context patterns. A full container was rejected because a planner doesn't need to run code.
- **What validation checks:**
  - Pydantic `extra="forbid"`.
  - Every `depends_on` refers to a key that exists.
  - The graph is acyclic.
  - The scope grammar.
- **A `file_scope` entry is an exact path or `dir/**`.** No mid-segment `*`, no `?`, no braces. One function, `scope_matches(scope, path)`, is true when `path == p` or `path.startswith(dir + "/")`.
  - Overlap between two scopes then reduces to equality and prefix tests.
  - General `fnmatch` overlap was rejected. Two globs can both match a file nobody has created yet, so checking overlap against the base tree is unsound.
- **Overlapping scopes add an edge.** If two assignments overlap and have no path between them in the graph, the planner adds an edge from the earlier one in list order to the later one. The edge is recorded in `contract.inferred_edges` before the digest is computed, so the human approves it explicitly.
  - If an added edge creates a cycle, the plan is rejected.
  - Rejecting overlapping plans outright was rejected because it would spend the single re-plan.
  - Letting overlapping assignments run concurrently and relying on the merge was rejected. It contradicts the spec, and every real conflict costs a full repair cycle.
- **Digest:** `sha256(json.dumps(contract, sort_keys=True, separators=(",", ":"), ensure_ascii=False))`. It covers the request, constraints, acceptance, interfaces, assignments, and `inferred_edges`. The contract holds only strings and lists, so this matches RFC 8785 without a new dependency.

## Interface to B

- `plan` is an entry in the job-worker's dispatch table, implemented by A's `controller/planner.py`.
- B's snapshot scope check uses `scope_matches()`, so planning and enforcement share one definition.

## Consequences

- Planning takes 30-90 seconds, so the parent polls with `airlock_status` ([0008](0008-plugin-and-mcp.md)).
- A malicious repository file can still steer the plan through prompt injection. The defenses are grammar validation and the human approving the digest.
