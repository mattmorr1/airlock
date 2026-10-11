import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from api.auth import Cap, Role, require
from api.main import DB, find
from api.models import Name
from controller.db import Doc
from controller.states import DelegationState, TaskState

router = APIRouter()
Hook = Annotated[Role, Depends(require(Cap.HOOK))]
OPEN = [TaskState.DRAFT, TaskState.PLANNED, TaskState.APPROVED, TaskState.RUNNING]
ACTIVE = (TaskState.APPROVED, TaskState.RUNNING)
READ_ONLY_AGENTS = frozenset({"Explore", "Plan"})
PATH_FIELD = {"Edit": "file_path", "Write": "file_path", "NotebookEdit": "notebook_path"}
LIVE = (DelegationState.RUNNING, DelegationState.VALIDATING, DelegationState.INTEGRATING)


class HookInput(BaseModel):
    """The native Claude Code hook payload; unknown fields are ignored."""
    session_id: Name
    cwd: str = "/"
    tool_name: str = ""
    tool_input: dict[str, object] = {}


async def open_task(db: DB, session_id: str) -> Doc | None:
    return await db["tasks"].find_one({"parent_session_id": session_id, "status": {"$in": OPEN}}, sort=[("created_at", -1)])


def deny(reason: str) -> Doc:
    return {"active": True, "hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": reason}}


def repo_root(root_id: str) -> str | None:
    """Host path for a registered root id, from the API's AIRLOCK_ROOTS JSON object."""
    return json.loads(os.environ.get("AIRLOCK_ROOTS") or "{}").get(root_id)


def inside(path: str, root: str) -> bool:
    # realpath is a few local lstat calls (microseconds), cheaper than a thread hop; it closes symlink escapes into the repo.
    return Path(os.path.realpath(path)).is_relative_to(os.path.realpath(root))


@router.post("/v1/hooks/pre-tool-use")
async def pre_tool_use(body: HookInput, db: DB, _: Hook) -> Doc:
    # No decision means Claude Code's normal permission flow runs; "allow" would skip the user's prompt.
    if (task := await open_task(db, body.session_id)) is None or task["status"] not in ACTIVE:
        return {"active": False}
    tid, kind = task["_id"], str(body.tool_input.get("subagent_type", ""))
    if body.tool_name == "Agent" and kind not in READ_ONLY_AGENTS:
        return deny(f"Airlock task {tid} is active: coding work runs as isolated delegations, not `{kind or 'general-purpose'}` subagents. "
                    f"Call airlock_delegate({tid!r}, <assignment_key>) for a ready assignment; Explore and Plan subagents stay allowed.")
    if (field := PATH_FIELD.get(body.tool_name)) is None:
        return {"active": True}
    root_id = (await find(db, "projects", task["project_id"]))["repository"]["registered_root_id"]
    if (root := repo_root(root_id)) is None:
        return deny(f"Airlock task {tid} is active and registered root {root_id!r} has no host path in the API's AIRLOCK_ROOTS, so writes are refused.")
    if inside(target := os.path.join(body.cwd, str(body.tool_input.get(field, ""))), root):
        return deny(f"Airlock task {tid} is active: {target} is inside the registered repository, which only delegations change. "
                    f"Call airlock_delegate({tid!r}, <assignment_key>) or airlock_status({tid!r}, 50).")
    return {"active": True}


def ready_keys(task: Doc, delegations: list[Doc]) -> list[str]:
    """Contract assignments not yet delegated whose dependencies are all accepted."""
    seen, accepted = {d["assignment_key"] for d in delegations}, {d["assignment_key"] for d in delegations if d["status"] == DelegationState.ACCEPTED}
    return [a["key"] for a in task["contract"]["assignments"] if a["key"] not in seen and set(a["depends_on"]) <= accepted]


def next_step(task: Doc, delegations: list[Doc]) -> str:
    tid, ready = task["_id"], ready_keys(task, delegations)
    repair = [d["assignment_key"] for d in delegations if d["status"] == DelegationState.REPAIR_REQUIRED]
    live = sum(d["status"] in LIVE for d in delegations)
    steps = [*(f"`{k}` is repair_required: call airlock_delegate({tid!r}, {k!r})" for k in repair),
             *(f"`{k}` is ready: call airlock_delegate({tid!r}, {k!r})" for k in ready),
             *([f"{live} delegation(s) in progress: call airlock_status({tid!r}, 50)"] if live else [])]
    return "; ".join(steps) or f"task {tid} is {task['status']} with nothing launchable: call airlock_status({tid!r}, 50)"


@router.post("/v1/hooks/lifecycle")
async def lifecycle(body: HookInput, db: DB, _: Hook) -> Doc:
    if (task := await open_task(db, body.session_id)) is None:
        return {"active": False}
    tid, status = task["_id"], task["status"]
    if status == TaskState.PLANNED:
        return {"active": False, "systemMessage": f"Airlock task {tid} awaiting operator: ! airlock -y approve {tid}"}
    if status == TaskState.DRAFT:
        return {"active": False, "systemMessage": f"Airlock task {tid} is still being planned; check back with airlock_status({tid!r}, 50)"}
    delegations = await db["delegations"].find({"task_id": tid}, {"assignment_key": 1, "status": 1}).to_list()
    return {"active": True, "decision": "block", "reason": f"Airlock task {tid} is {status}. Next: {next_step(task, delegations)}"}


TODAY_TYPES = {"task.completed": "done", "delegation.accepted": "merged", "delegation.failed": "failed", "task.failed": "failed", "job.failed": "failed",
               "harness.promoted": "promoted"}


def mood(task: Doc | None, delegations: list[Doc], expired: int) -> tuple[str, str]:
    """(mood, reason) for the HUD sprite; alarm outranks waiting, which outranks watching."""
    if task is None or task["status"] in (TaskState.COMPLETED, TaskState.CANCELLED):
        return "content", "done" if task else "idle"
    states = {d["status"] for d in delegations}
    if task["status"] == TaskState.FAILED or DelegationState.FAILED in states:
        return "alarm", "failed"
    if DelegationState.REPAIR_REQUIRED in states:
        return "alarm", "repair"
    if expired:
        return "alarm", "lease"
    if task["status"] in (TaskState.DRAFT, TaskState.PLANNED):
        return ("waiting", "approve") if task["status"] == TaskState.PLANNED else ("watching", "planning")
    if states & set(LIVE):
        return "watching", "merging"
    return ("waiting", "delegate") if ready_keys(task, delegations) else ("watching", "merging")


@router.get("/v1/hud")
async def hud(session_id: Name, db: DB, _: Hook) -> Doc:
    """Compact status for the statusline sprite: the session's latest task, merge progress, queue, harness, and today's project totals."""
    now = datetime.now(UTC)
    newest = [("created_at", -1)]
    task = await db["tasks"].find_one({"parent_session_id": session_id}, sort=newest)
    # AIRLOCK_HUD_FOLLOW_LATEST: sessions with no task of their own follow the DB's newest task (demo driven from another process).
    if task is None and os.environ.get("AIRLOCK_HUD_FOLLOW_LATEST"):
        task = await db["tasks"].find_one({}, sort=newest)
    if task is None:
        return {"mood": "content", "reason": "idle", "task": None}
    tid = task["_id"]
    delegations = await db["delegations"].find({"task_id": tid}, {"assignment_key": 1, "status": 1, "attempt": 1}).to_list()
    jobs = await db["jobs"].find({"task_id": tid, "status": {"$in": ["queued", "running"]}}, {"status": 1, "lease": 1, "available_at": 1}).to_list()
    leases: list[Doc] = [j.get("lease") or {} for j in jobs if j["status"] == "running"]
    expiries: list[datetime | None] = [lease.get("expires_at") for lease in leases]
    expired = sum(e is not None and e < now for e in expiries)
    project = await find(db, "projects", task["project_id"])
    last = await db["events"].find_one({"stream_id": tid}, {"type": 1, "created_at": 1}, sort=[("sequence", -1)])
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    # ponytail: unindexed (project_id, created_at) scan over today's events; add an index if events grow past demo scale.
    match = {"project_id": task["project_id"], "created_at": {"$gte": midnight}, "type": {"$in": list(TODAY_TYPES)}}
    counts: list[Doc] = await (await db["events"].aggregate([{"$match": match}, {"$group": {"_id": "$type", "n": {"$sum": 1}}}])).to_list()
    today = dict.fromkeys(TODAY_TYPES.values(), 0)
    for c in counts:
        today[TODAY_TYPES[c["_id"]]] += c["n"]
    by_state = {s: sum(d["status"] == s for d in delegations) for s in DelegationState}
    m, reason = mood(task, delegations, expired)
    return {"mood": m, "reason": reason, "today": today,
            "task": {"id": tid, "status": task["status"], "request": task.get("request", ""), "age_s": int((now - task["created_at"]).total_seconds())},
            "progress": {"total": len(task.get("contract", {}).get("assignments", [])), **{s.value: n for s, n in by_state.items()},
                         "ready": ready_keys(task, delegations) if task.get("contract") else [],
                         "retries": {d["assignment_key"]: d["attempt"] for d in delegations if d.get("attempt", 1) > 1}},
            "jobs": {"running": sum(j["status"] == "running" for j in jobs), "queued": sum(j["status"] == "queued" for j in jobs), "expired": expired},
            "harness": {"version": project.get("active_harness_version_id"), "generation": project.get("harness_generation"),
                        "epoch": project.get("constraint_epoch")},
            "last": {"type": last["type"], "age_s": int((now - last["created_at"]).total_seconds())} if last else None}
