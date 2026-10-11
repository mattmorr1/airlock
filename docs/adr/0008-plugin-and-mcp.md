---
status: proposed
---

# Plugin and MCP: three tools, a blocking Stop hook, marker-based fail-closed gates

Because the parent session launches delegations ([0004](0004-scheduler.md)), something has to bring it back when workers finish or repairs are due. The spec also asks the plugin to "prevent accidental bypass" and to block mutations "on controller failure" ([§5](../../airlock-evolve-architecture.md), [§6](../../airlock-evolve-architecture.md)). The trouble is that a hook learns whether its session is managed by asking the API, and that's the very thing that may be down.

This ADR stays `proposed` until a 10-minute spike confirms `${CLAUDE_SESSION_ID}` substitution and the hook payload fields on the installed CLI.

## Decisions

- **The MCP bridge is a FastMCP stdio server.** It's a thin `httpx` client that uses the `bridge` token. It exposes three tools:
  - `airlock_plan(project_id, parent_session_id, request, base_artifact_id)`
  - `airlock_status(task_id, wait_seconds)`
  - `airlock_delegate(task_id, assignment_key)`
  - `airlock_integrate` is dropped because integration is automatic. Approve, revoke, and rollback are available only through the operator CLI. The human approves from the prompt with `! airlock approve <task>`.
- **Correlation.** The `/airlock:run` skill passes `${CLAUDE_SESSION_ID}` as `parent_session_id`. Hooks receive `session_id` and look it up on the `(parent_session_id, status)` index. Assigning by "most recent launch" is never allowed.
- **A blocking `Stop` hook** calls `POST /v1/hooks/lifecycle`.
  - While the session's task is `approved` or `running`, it returns `decision: block` with an actionable reason, such as "`routes` is `repair_required`: call `airlock_delegate(task, 'routes')`" or "2 workers running: call `airlock_status(task, 50)`".
  - It lets Claude stop when the task is `planned` (the reason says it's waiting for the operator to approve) or terminal.
  - It deliberately ignores `stop_hook_active`. The loop ends when the task ends, and the repair cap and worker budgets bound it.
  - Rejected: relying on skill text alone, because Claude routinely ends its turn, and a report-only hook, because the human would have to keep re-prompting.
- **`PreToolUse` gates** call `POST /v1/hooks/pre-tool-use`.
  - On `Agent`, any non-read-only `subagent_type` is denied while a task is active; `Explore` and `Plan` are allowed. The deny reason redirects to `airlock_delegate`.
  - On `Edit`, `Write`, and `NotebookEdit`, paths inside the registered repository root are denied while a task is active.
  - Bash is not gated. Path hooks can't bound Bash, as the spec itself says.
  - The `PreToolUse` hook on `ExitPlanMode` is dropped, because the controller writes the plan ([0003](0003-planner.md)).
- **Fail-closed applies only to known-managed sessions.** `plugin/scripts/hook.py` uses only stdlib `urllib`, with a 2-second timeout.
  - When the API reports an active task, the hook writes `~/.airlock/managed/<session_id>`. When it reports none, the hook deletes the file.
  - On a timeout, the hook denies if that marker exists and allows otherwise.
  - Rejected: global fail-closed, which would brick every Claude session on the machine whenever the API is down, and fail-open, which ignores the spec.

## Consequences

- The parent spends roughly one turn per 50-second wait. That's about 10-20 turns for the demo.
- Every gated tool call costs one local HTTP round trip plus Python startup, about 50 ms.
