from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends
from pymongo.asynchronous.client_session import AsyncClientSession

from api.auth import Cap, Role, require
from api.errors import ApiError
from api.idempotency import digest, idempotent
from api.main import DB, Key, find
from api.models import Name, Strict
from controller.db import Doc
from controller.scheduler import enqueue
from controller.states import SCHEMA_VERSION, IntegrationRunState, JobKind, Prefix, TaskState, WorkerRunState, append_event, new_id, transition
from controller.states import DelegationState as DS

router = APIRouter()
MAX_ATTEMPTS = 2
LIVE = [WorkerRunState.QUEUED, WorkerRunState.STARTING, WorkerRunState.RUNNING, WorkerRunState.STOPPING, WorkerRunState.SNAPSHOTTING]


class Delegate(Strict):
    assignment_key: Name


@router.post("/v1/tasks/{task_id}/delegations", status_code=202)
async def delegate(task_id: str, body: Delegate, db: DB, key: Key, actor: Annotated[Role, Depends(require(Cap.DELEGATE))]) -> Doc:
    async def fn(s: AsyncClientSession) -> Doc:
        task = await find(db, "tasks", task_id, s)
        if task["status"] not in (TaskState.APPROVED, TaskState.RUNNING):
            raise ApiError(409, "STALE_REVISION", f"task {task_id} is {task['status']}, not approved or running")
        if (a := next((a for a in task["contract"]["assignments"] if a["key"] == body.assignment_key), None)) is None:
            raise ApiError(404, "NOT_FOUND", f"assignment {body.assignment_key!r} is not in the contract of task {task_id}")
        project = await find(db, "projects", task["project_id"], s)
        if (approved := task["approval"]["constraint_epoch"]) != (epoch := project["constraint_epoch"]):
            raise ApiError(409, "REAPPROVAL_REQUIRED", f"task {task_id} was approved at constraint epoch {approved}; the project is at {epoch}")
        prior = await db["delegations"].find_one({"task_id": task_id, "assignment_key": a["key"]}, session=s)
        if prior is not None and prior["status"] != DS.REPAIR_REQUIRED:
            raise ApiError(409, "STALE_REVISION", f"delegation {prior['_id']} is {prior['status']}, not repair_required")
        if prior is not None and prior["attempt"] >= MAX_ATTEMPTS:
            await transition(s, db["delegations"], prior["_id"], prior["revision"], DS.FAILED)
            await append_event(s, task_id, db["tasks"], "delegation.failed", actor, {"delegation_id": prior["_id"], "reason": "attempts_exhausted"})
            return {"error": {"code": "ATTEMPTS_EXHAUSTED", "message": f"delegation {prior['_id']} used {prior['attempt']} attempts and is now failed"}}
        deps = await db["delegations"].find({"task_id": task_id, "assignment_key": {"$in": a["depends_on"]}, "status": DS.ACCEPTED},
                                            {"assignment_key": 1}, session=s).to_list()
        if unmet := sorted(set(a["depends_on"]) - {d["assignment_key"] for d in deps}):
            raise ApiError(409, "DEPENDENCY_UNMET", f"assignment {a['key']!r} waits on unaccepted {unmet}")
        hv = await find(db, "harness_versions", project["active_harness_version_id"], s)
        cap = hv["config"]["delegation"]["max_parallel_workers"]
        if (n := await db["worker_runs"].count_documents({"task_id": task_id, "status": {"$in": LIVE}}, session=s)) >= cap:
            raise ApiError(409, "CAPACITY", f"task {task_id} has {n} live workers; harness {hv['_id']} allows {cap}", retryable=True)
        integrated = await db["integration_runs"].find_one({"task_id": task_id, "status": IntegrationRunState.ACCEPTED}, {"result_artifact_id": 1},
                                                           sort=[("integration_fence", -1)], session=s)
        input_id = integrated["result_artifact_id"] if integrated else task["base_artifact_id"]
        now, wid, attempt = datetime.now(UTC), new_id(Prefix.WORKER_RUN), prior["attempt"] + 1 if prior else 1
        pin = {"harness_version_id": hv["_id"], "constraint_epoch": epoch}
        if prior is None:
            did = new_id(Prefix.DELEGATION)
            await db["delegations"].insert_one({
                "_id": did, "schema_version": SCHEMA_VERSION, "project_id": task["project_id"], "task_id": task_id, "assignment_key": a["key"],
                "objective": a["objective"], "depends_on": [d["_id"] for d in deps], "file_scope": a["file_scope"], "profile_key": a["profile_key"],
                "status": DS.RUNNING, "attempt": 1, "revision": 1, "resolved_input_artifact_id": input_id, "contract_version": task["contract_version"],
                "active_worker_run_id": wid, "accepted_artifact_id": None, "created_at": now}, session=s)
            await db["tasks"].update_one({"_id": task_id}, {"$push": {"delegation_ids": did}}, session=s)
        else:
            did = prior["_id"]
            await transition(s, db["delegations"], did, prior["revision"], DS.RUNNING,
                             {"attempt": attempt, "resolved_input_artifact_id": input_id, "active_worker_run_id": wid})
        await db["worker_runs"].insert_one({
            "_id": wid, "schema_version": SCHEMA_VERSION, "project_id": task["project_id"], "task_id": task_id, "delegation_id": did, "attempt": attempt,
            "status": WorkerRunState.QUEUED, "revision": 1, "input_artifact_id": input_id, **pin, "output_artifact_id": None, "started_at": None,
            "finished_at": None, "created_at": now}, session=s)
        job_id = await enqueue(s, db, JobKind.LAUNCH, {"worker_run_id": wid, "delegation_id": did}, f"{wid}:launch", task["project_id"], task_id)
        if task["status"] == TaskState.APPROVED:
            await transition(s, db["tasks"], task_id, task["revision"], TaskState.RUNNING)
        res = {"delegation_id": did, "worker_run_id": wid, "job_id": job_id, "status": WorkerRunState.QUEUED.value, "attempt": attempt, **pin}
        await append_event(s, task_id, db["tasks"], "delegation.launched", actor, res | {"assignment_key": a["key"]})
        return res

    project = (await find(db, "tasks", task_id))["project_id"]
    res = await idempotent(db, project, actor, "task.delegate", key, digest(f"POST /v1/tasks/{task_id}/delegations", body.model_dump(mode="json")), fn)
    if err := res.get("error"):
        raise ApiError(409, err["code"], err["message"])
    return res
