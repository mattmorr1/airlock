import json
import os
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


def next_step(task: Doc, delegations: list[Doc]) -> str:
    tid, by_key = task["_id"], {d["assignment_key"]: d for d in delegations}
    accepted = {k for k, d in by_key.items() if d["status"] == DelegationState.ACCEPTED}
    repair = [k for k, d in by_key.items() if d["status"] == DelegationState.REPAIR_REQUIRED]
    ready = [a["key"] for a in task["contract"]["assignments"] if a["key"] not in by_key and set(a["depends_on"]) <= accepted]
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
        return {"active": False, "systemMessage": f"Airlock task {tid} awaiting operator: ! airlock approve {tid}"}
    if status == TaskState.DRAFT:
        return {"active": False, "systemMessage": f"Airlock task {tid} is still being planned; check back with airlock_status({tid!r}, 50)"}
    delegations = await db["delegations"].find({"task_id": tid}, {"assignment_key": 1, "status": 1}).to_list()
    return {"active": True, "decision": "block", "reason": f"Airlock task {tid} is {status}. Next: {next_step(task, delegations)}"}
