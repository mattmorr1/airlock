---
status: accepted
---

# Plugin and MCP: three tools, a blocking Stop hook, marker-based fail-closed gates

Because the parent session launches delegations ([0004](0004-scheduler.md)), something has to bring it back when workers finish or repairs are due. The spec also asks the plugin to "prevent accidental bypass" and to block mutations "on controller failure" ([§5](../../airlock-evolve-architecture.md), [§6](../../airlock-evolve-architecture.md)). The trouble is that a hook learns whether its session is managed by asking the API, and that's the very thing that may be down.

Accepted after the spike against Claude Code 2.1.296 and its docs (hooks, skills, plugins reference, MCP), recorded under [Spike findings](#spike-findings). No session was spawned, so the payloads are the documented ones, not a logged capture.

## Decisions

- **The MCP bridge is a FastMCP stdio server** (`MCPServer` in `mcp` 2.x, which renamed FastMCP). It's a thin `httpx` client that uses the `bridge` token. It exposes three tools:
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
  - On `Edit`, `Write`, and `NotebookEdit`, paths inside the registered repository root are denied while a task is active. The API resolves `registered_root_id` to a host path through its `AIRLOCK_ROOTS` JSON object; an unmapped root denies the write.
  - A call that isn't denied gets no decision, never `allow`, because `allow` skips the user's permission prompt.
  - Bash is not gated. Path hooks can't bound Bash, as the spec itself says.
  - The `PreToolUse` hook on `ExitPlanMode` is dropped, because the controller writes the plan ([0003](0003-planner.md)).
- **Fail-closed applies only to known-managed sessions.** `plugin/scripts/hook.py` uses only stdlib `urllib`, with a 2-second timeout.
  - When the API reports an active task, the hook writes `~/.airlock/managed/<session_id>`. When it reports none, the hook deletes the file.
  - On a timeout, connection error, HTTP error, or unparsable reply, the hook denies a `PreToolUse` if that marker exists and allows otherwise. `Stop` is never blocked on an outage, since stopping mutates nothing.
  - Rejected: global fail-closed, which would brick every Claude session on the machine whenever the API is down, and fail-open, which ignores the spec.

## Consequences

- The parent spends roughly one turn per 50-second wait. That's about 10-20 turns for the demo.
- Every gated tool call costs one local HTTP round trip plus Python startup, about 50 ms.
- Claude Code caps consecutive Stop-hook continuations at 8 by default, about 7 minutes of 50-second waits. Operators raise it with `CLAUDE_CODE_STOP_HOOK_BLOCK_CAP` (for example 40) in the shell or `settings.json` `env`; a plugin can't set it.
- The bridge starts with `uv run --project ${CLAUDE_PLUGIN_ROOT}/..`, so the plugin is loaded in place with `claude --plugin-dir <repo>/plugin`, not from a marketplace copy.

## Spike findings

- `${CLAUDE_SESSION_ID}` is a documented skill substitution and applies to plugin skills; `/airlock:run` is the namespaced name of `plugin/skills/run`.
- Common hook input carries `session_id`, `cwd`, and `hook_event_name`. `PreToolUse` adds `tool_name`, `tool_input`, and `tool_use_id`. The subagent tool is `Agent` with `tool_input.subagent_type`; `Edit` and `Write` use `file_path`, `NotebookEdit` uses `notebook_path`.
- `PreToolUse` decisions are `hookSpecificOutput.permissionDecision` (`allow`, `deny`, `ask`, `defer`) with `permissionDecisionReason`; precedence across hooks is deny > defer > ask > allow.
- `Stop` input has `stop_hook_active` and `last_assistant_message`; `{"decision": "block", "reason": ...}` keeps Claude going, and omitting `decision` allows the stop. The 8-continuation cap is the one finding that breaks an assumption of the original design.
- Plugin MCP `env` expands `${VAR}` and `${VAR:-default}`, so the bridge token and URL come from the operator's environment.
- In `mcp` 2.x a tool's plain exception text is hidden from the model, so the bridge raises `ToolError` carrying the API error envelope.
