import asyncio
import json
import time
from collections.abc import AsyncGenerator, AsyncIterator, MutableMapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from pymongo.asynchronous.database import AsyncDatabase

import api.events
from api.main import app
from controller.db import Doc
from controller.events import Doorbell
from controller.states import Prefix, TaskState, append_event, new_id

pytestmark = pytest.mark.anyio
TOKEN = "rd-secret"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
type Frame = dict[str, str]


@dataclass(frozen=True, slots=True)
class Env:
    db: AsyncDatabase[Doc]
    proj: str
    task: str
    bell: Doorbell
    http: httpx.AsyncClient


@pytest.fixture
async def env(db: AsyncDatabase[Doc], monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Env]:
    monkeypatch.setenv("AIRLOCK_TOKEN_READER", TOKEN)
    monkeypatch.setattr(api.events, "HEARTBEAT", 0.3)
    proj, task, now = new_id(Prefix.PROJECT), new_id(Prefix.TASK), datetime.now(UTC)
    await db["projects"].insert_one({"_id": proj, "schema_version": 1, "constraint_epoch": 1, "event_seq": 0, "revision": 1, "created_at": now})
    await db["tasks"].insert_one({"_id": task, "schema_version": 1, "project_id": proj, "status": TaskState.RUNNING, "revision": 1, "event_seq": 0,
                                  "created_at": now})
    app.state.db = db
    async with Doorbell(db) as app.state.doorbell, httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://airlock") as http:
        yield Env(db, proj, task, app.state.doorbell, http)


async def append(db: AsyncDatabase[Doc], stream_id: str, coll: str, n: int, type_: str = "tick") -> None:
    async with db.client.start_session() as s:
        for _ in range(n):
            await s.with_transaction(lambda s: append_event(s, stream_id, db[coll], type_, "tester", {}))


@asynccontextmanager
async def sse(path: str, last_event_id: int | None = None) -> AsyncGenerator[asyncio.Queue[Frame]]:
    """Drives the ASGI app directly: httpx's ASGITransport buffers the whole body, so it cannot read an endless stream."""
    frames: asyncio.Queue[Frame] = asyncio.Queue()
    gone, asked = asyncio.Event(), False
    headers = [(b"authorization", AUTH["Authorization"].encode()), *([(b"last-event-id", str(last_event_id).encode())] if last_event_id is not None else [])]
    scope: Doc = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET", "scheme": "http", "path": path,
                  "raw_path": path.encode(), "query_string": b"", "root_path": "", "headers": headers, "server": ("airlock", 80), "client": ("test", 1)}

    async def receive() -> Doc:
        nonlocal asked
        if asked:
            await gone.wait()
            return {"type": "http.disconnect"}
        asked = True
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(m: MutableMapping[str, Any]) -> None:
        if m["type"] == "http.response.start":
            assert m["status"] == 200
        elif m.get("body"):
            frames.put_nowait(dict(line.split(": ", 1) for line in m["body"].decode().strip().split("\n")))

    served = asyncio.create_task(app(scope, receive, send))
    try:
        yield frames
    finally:
        gone.set()
        await asyncio.wait_for(served, 5)


async def ids(frames: asyncio.Queue[Frame], n: int, within: float = 10) -> list[int]:
    out: list[int] = []
    async with asyncio.timeout(within):
        while len(out) < n:
            if "id" in (f := await frames.get()):
                assert json.loads(f["data"])["sequence"] == int(f["id"])
                out.append(int(f["id"]))
    return out


async def idle(frames: asyncio.Queue[Frame]) -> None:
    """Asserts no further event arrives before the next heartbeat."""
    async with asyncio.timeout(5):
        while "id" not in (f := await frames.get()) and f != {"": "heartbeat"}:
            pass
    assert "id" not in f, f


async def test_reconnect_with_last_event_id_has_no_gaps_or_duplicates(env: Env) -> None:
    await append(env.db, env.task, "tasks", 5)
    async with sse(f"/v1/tasks/{env.task}/events") as frames:
        assert await ids(frames, 5) == [1, 2, 3, 4, 5]
    await append(env.db, env.task, "tasks", 3)
    async with sse(f"/v1/tasks/{env.task}/events", last_event_id=5) as frames:
        assert await ids(frames, 3) == [6, 7, 8]
        await append(env.db, env.task, "tasks", 2)
        assert await ids(frames, 2) == [9, 10]
        await idle(frames)


async def test_closed_change_stream_still_delivers_within_heartbeat(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api.events, "HEARTBEAT", 2.0)
    async with sse(f"/v1/tasks/{env.task}/events") as frames:
        await append(env.db, env.task, "tasks", 2)
        assert await ids(frames, 2) == [1, 2]
        old = env.bell.stream
        assert old is not None
        await old.close()
        await append(env.db, env.task, "tasks", 3)
        start = time.monotonic()
        assert await ids(frames, 3) == [3, 4, 5]
        assert time.monotonic() - start < 2.0
        assert env.bell.stream is not old
        await append(env.db, env.task, "tasks", 1)
        assert await ids(frames, 1) == [6]
        await idle(frames)


async def test_dead_doorbell_falls_back_to_heartbeat_reads(env: Env) -> None:
    assert env.bell.task is not None
    env.bell.task.cancel()
    async with sse(f"/v1/tasks/{env.task}/events") as frames:
        await append(env.db, env.task, "tasks", 3)
        assert await ids(frames, 3, within=5) == [1, 2, 3]
        await idle(frames)


async def test_project_stream_events_only_on_project_endpoint(env: Env) -> None:
    await append(env.db, env.task, "tasks", 1)
    async with sse(f"/v1/projects/{env.proj}/events") as frames:
        await append(env.db, env.proj, "projects", 1, "harness.promoted")
        await append(env.db, env.proj, "projects", 1, "project.revoked")
        assert await ids(frames, 2) == [1, 2]
        await idle(frames)
    assert [e["type"] for e in await env.db["events"].find({"stream_id": env.proj}).sort("sequence").to_list()] == ["harness.promoted", "project.revoked"]
    async with sse(f"/v1/tasks/{env.task}/events") as frames:
        assert await ids(frames, 1) == [1]
        await idle(frames)
    assert (await env.http.get("/v1/tasks/task_missing/events", headers=AUTH)).status_code == 404
    assert (await env.http.get(f"/v1/projects/{env.proj}/events")).status_code == 401


async def test_long_poll_returns_early_on_event_and_at_timeout_otherwise(env: Env) -> None:
    start = time.monotonic()
    r = await env.http.get(f"/v1/tasks/{env.task}", params={"wait_seconds": 1}, headers=AUTH)
    assert (r.status_code, r.json()["task"]["event_seq"]) == (200, 0)
    assert 1 <= time.monotonic() - start < 3

    poll = asyncio.create_task(env.http.get(f"/v1/tasks/{env.task}", params={"wait_seconds": 30, "after": 0}, headers=AUTH))
    await asyncio.sleep(0.5)
    assert not poll.done()
    start = time.monotonic()
    await append(env.db, env.task, "tasks", 1)
    assert (await asyncio.wait_for(poll, 10)).json()["task"]["event_seq"] == 1
    assert time.monotonic() - start < 5

    start = time.monotonic()
    assert (await env.http.get(f"/v1/tasks/{env.task}", params={"wait_seconds": 30, "after": 0}, headers=AUTH)).json()["task"]["event_seq"] == 1
    assert time.monotonic() - start < 3
    assert (await env.http.get(f"/v1/tasks/{env.task}", params={"wait_seconds": 51}, headers=AUTH)).status_code == 422
