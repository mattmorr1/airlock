---
name: run
description: Run a coding request as an Airlock task - the controller plans it, the operator approves it, and isolated workers implement each assignment while this session coordinates.
argument-hint: <request>
---

Run this request as a managed Airlock task against the current project: $ARGUMENTS

The project is this session's working directory; you do not need a project id or a base artifact. Airlock registers the repo (if new) and bases the task on its latest integrated tree automatically.

1. Call `airlock_register` (no arguments) to make sure this directory is a registered Airlock project. It is idempotent and returns the `project_id` and a `dashboard_url`. Show the user the `dashboard_url` so they can watch the task live (frozen artifacts, checks, the before/after diff, integration).
2. Call `airlock_plan` with `parent_session_id` set to exactly `${CLAUDE_SESSION_ID}` and `request` set to the user's request above. Do not pass `project_id`, `cwd`, or `base_artifact_id` — Airlock resolves the project from this directory and defaults the base to its latest integrated tree. Only fall back to asking the user for a `project_id` if `airlock_plan` reports that no project could be resolved.
3. Poll `airlock_status(task_id, 50)` until the task is `planned`, then show the user the contract's assignments and tell them to approve it themselves from the prompt with `! airlock -y approve <task_id>`. You cannot approve, revoke, roll back, or integrate; those are operator actions.
4. Once the task is `approved` or `running`, drive it to completion with `airlock_run(task_id)`. It delegates every ready or `repair_required` assignment, waits for events, and returns when the task is `completed`/`failed`/`cancelled`, when it needs operator approval again, or when its time budget is spent. If the returned `note` says the budget was spent, call `airlock_run(task_id)` again to keep driving. Report notable lines from its `log` as it goes.
5. While the task is active, do not edit files in the registered repository and do not start coding subagents; the hooks deny both. `Explore` and `Plan` subagents are fine. Integration is automatic.
6. When the task finishes, report the final status, `final_artifact_id`, and any failed delegations, and point the user back at the `dashboard_url`.
