import asyncio
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
from controller.db import Doc
from controller.events import Doorbell
from controller.scheduler import enqueue
from controller.states import SCHEMA_VERSION, Conflict, JobKind, Prefix, TaskState, append_event, new_id, transition


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


@app.post("/v1/tasks", status_code=202, response_model=TaskCreated)
async def create_task(body: TaskCreate, db: DB, key: Key, actor: Annotated[Role, Depends(require(Cap.TASK_CREATE))]) -> Doc:
    async def fn(s: AsyncClientSession) -> Doc:
        await find(db, "projects", body.project_id, s)
        tid = new_id(Prefix.TASK)
        await db["tasks"].insert_one({"_id": tid, "schema_version": SCHEMA_VERSION, **body.model_dump(), "status": TaskState.DRAFT, "revision": 1,
                                      "contract_version": 0, "contract": None, "contract_digest": None, "approval": None, "event_seq": 0,
                                      "delegation_ids": [], "final_artifact_id": None, "final_validation_run_id": None, "created_at": datetime.now(UTC)},
                                     session=s)
        job_id = await enqueue(s, db, JobKind.PLAN, {"task_id": tid}, f"{tid}:plan", body.project_id, tid)
        return {"task_id": tid, "status": TaskState.DRAFT.value, "job_id": job_id}
    return await idempotent(db, body.project_id, actor, "task.create", key, digest("POST /v1/tasks", body.model_dump(mode="json")), fn)


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
    return {"task": task | {"approval_required": task["status"] == TaskState.PLANNED},
            "delegations": await db["delegations"].find({"task_id": task_id}).sort("_id").to_list()}


@app.get("/v1/jobs/{job_id}")
async def get_job(job_id: str, db: DB, _: Annotated[Role, Depends(require(Cap.READ))]) -> Doc:
    return await find(db, "jobs", job_id)
