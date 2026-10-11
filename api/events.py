from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import StreamingResponse
from pydantic_core import to_json
from pymongo.asynchronous.database import AsyncDatabase

from api.auth import Cap, Role, require
from api.errors import ApiError
from controller.db import Doc
from controller.events import Doorbell, read_after

HEARTBEAT = 15.0
BATCH = 100
router = APIRouter()


async def get_doorbell(request: Request) -> Doorbell:
    return request.app.state.doorbell


Bell = Annotated[Doorbell, Depends(get_doorbell)]
Reader = Annotated[Role, Depends(require(Cap.READ))]
LastEventId = Annotated[int, Header(alias="Last-Event-ID", ge=0)]


async def sse(request: Request, bell: Doorbell, coll: str, stream_id: str, cursor: int) -> StreamingResponse:
    db: AsyncDatabase[Doc] = request.app.state.db
    if await db[coll].find_one({"_id": stream_id}, {"_id": 1}) is None:
        raise ApiError(404, "NOT_FOUND", f"{coll} {stream_id} not found")

    async def frames() -> AsyncIterator[str]:
        nonlocal cursor
        while True:
            ev = bell.arm(stream_id)
            for e in (batch := await read_after(db, stream_id, cursor, BATCH)):
                cursor = e["sequence"]
                yield f"id: {cursor}\nevent: {e['type']}\ndata: {to_json(e).decode()}\n\n"
            if len(batch) < BATCH and not await bell.wait(ev, HEARTBEAT):
                yield ": heartbeat\n\n"
    return StreamingResponse(frames(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


@router.get("/v1/tasks/{task_id}/events")
async def task_events(task_id: str, request: Request, bell: Bell, _: Reader, last_event_id: LastEventId = 0) -> StreamingResponse:
    return await sse(request, bell, "tasks", task_id, last_event_id)


@router.get("/v1/projects/{project_id}/events")
async def project_events(project_id: str, request: Request, bell: Bell, _: Reader, last_event_id: LastEventId = 0) -> StreamingResponse:
    return await sse(request, bell, "projects", project_id, last_event_id)
