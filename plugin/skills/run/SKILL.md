---
name: run
description: Run a coding request as an Airlock task - the controller plans it, the operator approves it, and isolated workers implement each assignment while this session coordinates.
argument-hint: <project_id> <base_artifact_id> <request>
---

Run this request as a managed Airlock task: $ARGUMENTS

1. Call `airlock_plan` with `parent_session_id` set to exactly `${CLAUDE_SESSION_ID}`, plus the `project_id`, `base_artifact_id`, and `request` from the arguments above. Ask the user for any that are missing; never invent IDs.
2. Poll `airlock_status(task_id, 50)` until the task is `planned`, then show the user the contract's assignments and tell them to approve it themselves from the prompt with `! airlock -y approve <task_id>`. You cannot approve, revoke, roll back, or integrate; those are operator actions.
3. Once the task is `approved` or `running`, loop until it is `completed`, `failed`, or `cancelled`:
   - Call `airlock_delegate(task_id, key)` for every assignment that is ready (no delegation yet and every `depends_on` accepted) or whose delegation is `repair_required`.
   - Otherwise call `airlock_status(task_id, 50)` to wait for the next event. A `CAPACITY` error means wait the same way, then retry.
4. While the task is active, do not edit files in the registered repository and do not start coding subagents; the hooks deny both. `Explore` and `Plan` subagents are fine. Integration is automatic.
5. When the task finishes, report the final status, `final_artifact_id`, and any failed delegations.
