from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pymongo.asynchronous.client_session import AsyncClientSession

from api.auth import Cap, Role, require
from api.idempotency import digest, idempotent
from api.main import DB, Key, app, error, find
from api.models import Rollback
from controller.db import Doc
from controller.policies import NotValidated, revoke, rollback

router = APIRouter()


# Starlette resolves handlers by exception MRO, so this wins over the generic Conflict handler.
@app.exception_handler(NotValidated)
async def on_not_validated(_: Request, e: NotValidated) -> JSONResponse:
    return error(409, "NOT_VALIDATED", str(e))


@router.post("/v1/projects/{project_id}/rollback")
async def rollback_project(project_id: str, body: Rollback, db: DB, key: Key, actor: Annotated[Role, Depends(require(Cap.ROLLBACK))]) -> Doc:
    async def fn(s: AsyncClientSession) -> Doc:
        await rollback(s, db, project_id, body.harness_version_id, actor)
        return {"project_id": project_id, "active_harness_version_id": body.harness_version_id}
    return await idempotent(db, project_id, actor, "project.rollback", key, digest(f"POST /v1/projects/{project_id}/rollback", body.model_dump()), fn)


@router.post("/v1/projects/{project_id}/revoke")
async def revoke_project(project_id: str, db: DB, key: Key, actor: Annotated[Role, Depends(require(Cap.REVOKE))]) -> Doc:
    async def fn(s: AsyncClientSession) -> Doc:
        await find(db, "projects", project_id, s)
        return {"project_id": project_id, "constraint_epoch": await revoke(s, db, project_id, actor)}
    return await idempotent(db, project_id, actor, "project.revoke", key, digest(f"POST /v1/projects/{project_id}/revoke"), fn)
