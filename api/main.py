import asyncio
import json
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pymongo.asynchronous.client_session import AsyncClientSession
from pymongo.asynchronous.database import AsyncDatabase
from pymongo.errors import DuplicateKeyError

from api.auth import Cap, Role, require
from api.errors import ApiError
from api.events import Bell, router
from api.idempotency import digest, idempotent
from api.models import Approve, Approved, ProjectCreate, ProjectCreated, TaskCreate, TaskCreated
from controller import db as dbmod
from controller import planner
from controller.db import Doc
from controller.events import Doorbell
from controller.scheduler import enqueue
from controller.states import SCHEMA_VERSION, Conflict, IntegrationRunState, JobKind, Prefix, TaskState, append_event, new_id, transition


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    async with dbmod.client() as c:
        app.state.db = dbmod.database(c)
        async with Doorbell(app.state.db) as app.state.doorbell:
            yield


app = FastAPI(title="Airlock", lifespan=lifespan)
app.include_router(router)


def error(status: int, code: str, message: str, retryable: bool = False) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message, "retryable": retryable}}, status_code=status)


@app.exception_handler(ApiError)
async def on_api_error(_: Request, e: ApiError) -> JSONResponse:
    return error(e.status, e.code, e.message, e.retryable)


@app.exception_handler(Conflict)
async def on_conflict(_: Request, e: Conflict) -> JSONResponse:
    return error(409, "STALE_REVISION", str(e))


@app.exception_handler(DuplicateKeyError)
async def on_duplicate(_: Request, e: DuplicateKeyError) -> JSONResponse:
    return error(409, "DUPLICATE", f"already exists: {e.details.get('keyValue') if e.details else e}")


@app.exception_handler(RequestValidationError)
async def on_invalid(_: Request, e: RequestValidationError) -> JSONResponse:
    return error(422, "INVALID_REQUEST", "; ".join(f"{'.'.join(map(str, err['loc']))}: {err['msg']}" for err in e.errors()))


async def get_db(request: Request) -> AsyncDatabase[Doc]:
    return request.app.state.db


DB = Annotated[AsyncDatabase[Doc], Depends(get_db)]
Key = Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=200)]


async def find(db: AsyncDatabase[Doc], coll: str, id_: str, session: AsyncClientSession | None = None) -> Doc:
    if (doc := await db[coll].find_one({"_id": id_}, session=session)) is None:
        raise ApiError(404, "NOT_FOUND", f"{coll} {id_} not found")
    return doc


@app.post("/v1/projects", status_code=201, response_model=ProjectCreated)
async def create_project(body: ProjectCreate, db: DB, key: Key, actor: Annotated[Role, Depends(require(Cap.PROJECT_CREATE))]) -> Doc:
    async def fn(s: AsyncClientSession) -> Doc:
        pid = new_id(Prefix.PROJECT)
        await db["projects"].insert_one({"_id": pid, "schema_version": SCHEMA_VERSION, "name": body.name, "repository": body.repository.model_dump(),
                                         "baseline_artifact_id": body.baseline_artifact_id, "active_harness_version_id": body.harness_version_id,
                                         "harness_generation": 1, "constraint_epoch": 1, "validated_harness_ids": [body.harness_version_id],
                                         "event_seq": 0, "revision": 1, "created_at": datetime.now(UTC)}, session=s)
        return {"project_id": pid}
    return await idempotent(db, "-", actor, "project.create", key, digest("POST /v1/projects", body.model_dump(mode="json")), fn)


async def resolve_base(db: AsyncDatabase[Doc], body: TaskCreate) -> tuple[str, str]:
    """(project_id, base_artifact_id) with defaults applied; 404 for an unknown project or an artifact outside it."""
    if (pid := body.project_id) is None:
        if body.cwd is None:
            raise ApiError(422, "INVALID_REQUEST", "give project_id or cwd")
        roots = sorted(((r, p) for r, p in json.loads(os.environ.get("AIRLOCK_ROOTS") or "{}").items() if hooks.inside(body.cwd, p)),
                       key=lambda rp: len(rp[1]), reverse=True)
        proj = await db["projects"].find_one({"repository.registered_root_id": roots[0][0]}, {"_id": 1}, sort=[("created_at", -1)]) if roots else None
        if proj is None:
            raise ApiError(404, "NOT_FOUND", f"no project is registered for {body.cwd}; pass project_id or register this repo in AIRLOCK_ROOTS")
        pid = proj["_id"]
    proj = await find(db, "projects", pid)
    if (art := body.base_artifact_id) is None:
        last = await db["integration_runs"].find_one({"project_id": pid, "status": IntegrationRunState.ACCEPTED}, {"result_artifact_id": 1},
                                                     sort=[("created_at", -1)])
        return pid, last["result_artifact_id"] if last else proj["baseline_artifact_id"]
    if art != proj["baseline_artifact_id"] and await db["artifacts"].count_documents({"_id": art, "project_id": pid}, limit=1) == 0:
        raise ApiError(404, "NOT_FOUND", f"artifact {art} is not in project {pid}; omit base_artifact_id to use its latest integrated tree")
    return pid, art


@app.post("/v1/tasks", status_code=202, response_model=TaskCreated)
async def create_task(body: TaskCreate, db: DB, key: Key, actor: Annotated[Role, Depends(require(Cap.TASK_CREATE))]) -> Doc:
    pid, base = await resolve_base(db, body)
    contract = None if body.contract is None else planner.validate(body.request, body.contract)
    if isinstance(contract, list):
        raise ApiError(422, "CONTRACT_INVALID", "; ".join(contract))
    planned = {"status": TaskState.PLANNED, "contract_version": 1, "contract": contract, "contract_digest": planner.digest(contract)} if contract else \
        {"status": TaskState.DRAFT, "contract_version": 0, "contract": None, "contract_digest": None}

    async def fn(s: AsyncClientSession) -> Doc:
        tid = new_id(Prefix.TASK)
        await db["tasks"].insert_one({"_id": tid, "schema_version": SCHEMA_VERSION, "project_id": pid, "parent_session_id": body.parent_session_id,
                                      "request": body.request, "base_artifact_id": base, "revision": 1, **planned, "approval": None, "event_seq": 0,
                                      "delegation_ids": [], "final_artifact_id": None, "final_validation_run_id": None, "created_at": datetime.now(UTC)},
                                     session=s)
        if contract:
            await append_event(s, tid, db["tasks"], "task.planned", actor, {"contract_digest": planned["contract_digest"], "author": "session"})
            return {"task_id": tid, "status": TaskState.PLANNED.value, "job_id": None}
        job_id = await enqueue(s, db, JobKind.PLAN, {"task_id": tid}, f"{tid}:plan", pid, tid)
        return {"task_id": tid, "status": TaskState.DRAFT.value, "job_id": job_id}
    return await idempotent(db, pid, actor, "task.create", key, digest("POST /v1/tasks", body.model_dump(mode="json")), fn)


@app.post("/v1/tasks/{task_id}/approve", response_model=Approved)
async def approve_task(task_id: str, body: Approve, db: DB, key: Key, actor: Annotated[Role, Depends(require(Cap.TASK_APPROVE))]) -> Doc:
    async def fn(s: AsyncClientSession) -> Doc:
        task = await find(db, "tasks", task_id, s)
        if task.get("contract_digest") != body.contract_digest:
            raise ApiError(409, "DIGEST_MISMATCH", f"task {task_id} contract digest is {task.get('contract_digest')}, not {body.contract_digest}")
        epoch = (await find(db, "projects", task["project_id"], s))["constraint_epoch"]
        approval = {"actor_id": actor.value, "contract_digest": body.contract_digest, "constraint_epoch": epoch, "approved_at": datetime.now(UTC)}
        await transition(s, db["tasks"], task_id, task["revision"], TaskState.APPROVED, {"approval": approval})
        await append_event(s, task_id, db["tasks"], "task.approved", actor, {"contract_digest": body.contract_digest, "constraint_epoch": epoch})
        return {"task_id": task_id, "status": TaskState.APPROVED.value, "contract_digest": body.contract_digest, "constraint_epoch": epoch}
    project = (await find(db, "tasks", task_id))["project_id"]
    return await idempotent(db, project, actor, "task.approve", key, digest(f"POST /v1/tasks/{task_id}/approve", body.model_dump(mode="json")), fn)


@app.get("/v1/tasks/{task_id}")
async def get_task(task_id: str, db: DB, bell: Bell, _: Annotated[Role, Depends(require(Cap.READ))],
                   wait_seconds: Annotated[float, Query(ge=0, le=50)] = 0, after: Annotated[int | None, Query(ge=0)] = None) -> Doc:
    loop = asyncio.get_running_loop()
    ev, deadline, task = bell.arm(task_id), loop.time() + wait_seconds, await find(db, "tasks", task_id)
    seen = task["event_seq"] if after is None else after
    while task["event_seq"] <= seen and (left := deadline - loop.time()) > 0:
        await bell.wait(ev, left)
        ev, task = bell.arm(task_id), await find(db, "tasks", task_id)
    dels = await db["delegations"].find({"task_id": task_id}).sort("_id").to_list()
    failed_job = await db["jobs"].find_one({"task_id": task_id, "status": "failed"}, {"kind": 1, "last_error": 1}, sort=[("created_at", -1)])
    live = task["status"] in (TaskState.APPROVED, TaskState.RUNNING)
    launchable = [*hooks.ready_keys(task, dels), *(d["assignment_key"] for d in dels if d["status"] == "repair_required")] if live else []
    return {"task": task | {"approval_required": task["status"] == TaskState.PLANNED, "launchable": launchable,
                            "next": hooks.next_step(task, dels) if live else None,
                            "last_error": f"{failed_job['kind']}: {failed_job.get('last_error')}" if failed_job else None},
            "delegations": dels}


@app.get("/v1/projects")
async def list_projects(db: DB, _: Annotated[Role, Depends(require(Cap.READ))]) -> list[Doc]:
    return await db["projects"].find({}, {"name": 1, "baseline_artifact_id": 1, "constraint_epoch": 1, "created_at": 1},
                                     sort=[("created_at", -1)]).limit(20).to_list()


@app.get("/v1/projects/{project_id}/tasks")
async def list_tasks(project_id: str, db: DB, _: Annotated[Role, Depends(require(Cap.READ))]) -> list[Doc]:
    return await db["tasks"].find({"project_id": project_id}, {"request": 1, "status": 1, "final_artifact_id": 1, "created_at": 1},
                                  sort=[("created_at", -1)]).limit(20).to_list()


@app.get("/v1/jobs/{job_id}")
async def get_job(job_id: str, db: DB, _: Annotated[Role, Depends(require(Cap.READ))]) -> Doc:
    return await find(db, "jobs", job_id)


from api.delegations import router as delegations  # noqa: E402  (circular: it imports DB, Key, find from here)

app.include_router(delegations)

from api import policies  # noqa: E402  # imported last: it imports DB, Key, find from this module

app.include_router(policies.router)

from api import hooks  # noqa: E402  # imported last: it imports DB, find from this module

app.include_router(hooks.router)
