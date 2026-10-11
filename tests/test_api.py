import asyncio
import inspect
from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute
from pymongo.asynchronous.database import AsyncDatabase

from api.main import app
from controller.db import Doc
from controller.states import TaskState, transition

pytestmark = pytest.mark.anyio
TOKENS = {"operator": "op-secret", "bridge": "br-secret", "hook": "hk-secret", "reader": "rd-secret"}
DIGEST = "sha256:contract-example"


def auth(role: str, key: str | None = None) -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKENS[role]}", **({"Idempotency-Key": key} if key else {})}


@pytest.fixture
async def client(db: AsyncDatabase[Doc], monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[httpx.AsyncClient]:
    for role, token in TOKENS.items():
        monkeypatch.setenv(f"AIRLOCK_TOKEN_{role.upper()}", token)
    app.state.db = db
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://airlock") as c:
        yield c


async def project(client: httpx.AsyncClient, key: str) -> str:
    r = await client.post("/v1/projects", headers=auth("operator", key), json={
        "name": "auth-demo", "repository": {"registered_root_id": f"repo_{key}"}, "baseline_artifact_id": "art_base_01", "harness_version_id": "hv_001"})
    assert r.status_code == 201, r.text
    return r.json()["project_id"]


def task_body(proj: str, request: str = "Implement login and sessions") -> Doc:
    return {"project_id": proj, "parent_session_id": "claude_parent_01", "request": request, "base_artifact_id": "art_base_01"}


async def planned_task(client: httpx.AsyncClient, db: AsyncDatabase[Doc], key: str) -> str:
    r = await client.post("/v1/tasks", headers=auth("bridge", f"{key}-task"), json=task_body(await project(client, key)))
    tid = r.json()["task_id"]
    async with db.client.start_session() as s:
        await transition(s, db["tasks"], tid, 1, TaskState.PLANNED, {"contract": {"assignments": []}, "contract_digest": DIGEST})
    return tid


async def test_create_project_seeds_validated_harness(client: httpx.AsyncClient, db: AsyncDatabase[Doc]) -> None:
    doc = await db["projects"].find_one({"_id": await project(client, "p-seed")})
    assert doc is not None
    assert (doc["validated_harness_ids"], doc["active_harness_version_id"], doc["constraint_epoch"], doc["event_seq"]) == (["hv_001"], "hv_001", 1, 0)


async def test_create_task_is_draft_with_plan_job(client: httpx.AsyncClient, db: AsyncDatabase[Doc]) -> None:
    proj = await project(client, "t-draft")
    r = await client.post("/v1/tasks", headers=auth("bridge", "t-draft-1"), json=task_body(proj))
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["status"] == "draft"
    job = (await client.get(f"/v1/jobs/{body['job_id']}", headers=auth("reader"))).json()
    assert (job["kind"], job["status"], job["task_id"], job["payload"]) == ("plan", "queued", body["task_id"], {"task_id": body["task_id"]})
    got = (await client.get(f"/v1/tasks/{body['task_id']}", headers=auth("reader"))).json()
    assert (got["task"]["status"], got["task"]["approval_required"], got["delegations"]) == ("draft", False, [])


async def test_same_key_retry_returns_identical_body_and_one_task(client: httpx.AsyncClient, db: AsyncDatabase[Doc]) -> None:
    proj = await project(client, "t-retry")
    first, second = [await client.post("/v1/tasks", headers=auth("bridge", "t-retry-1"), json=task_body(proj)) for _ in range(2)]
    assert (first.status_code, second.status_code) == (202, 202)
    assert first.content == second.content
    assert await db["tasks"].count_documents({"project_id": proj}) == 1
    assert await db["jobs"].count_documents({"project_id": proj, "kind": "plan"}) == 1


async def test_same_key_different_body_is_409(client: httpx.AsyncClient, db: AsyncDatabase[Doc]) -> None:
    proj = await project(client, "t-mismatch")
    assert (await client.post("/v1/tasks", headers=auth("bridge", "t-mismatch-1"), json=task_body(proj))).status_code == 202
    r = await client.post("/v1/tasks", headers=auth("bridge", "t-mismatch-1"), json=task_body(proj, "something else"))
    assert r.status_code == 409
    assert r.json() == {"error": {"code": "IDEMPOTENCY_MISMATCH", "message": r.json()["error"]["message"], "retryable": False}}
    assert await db["tasks"].count_documents({"project_id": proj}) == 1


async def test_concurrent_same_key_one_success_and_in_progress_or_cached(client: httpx.AsyncClient, db: AsyncDatabase[Doc]) -> None:
    proj = await project(client, "t-race")
    rs = await asyncio.gather(*(client.post("/v1/tasks", headers=auth("bridge", "t-race-1"), json=task_body(proj)) for _ in range(2)))
    ok = [r for r in rs if r.status_code == 202]
    others = [r.json()["error"] for r in rs if r.status_code != 202]
    assert ok and all(r.content == ok[0].content for r in ok)
    assert all(e["code"] == "IN_PROGRESS" and e["retryable"] for e in others), others
    assert await db["tasks"].count_documents({"project_id": proj}) == 1


async def test_missing_idempotency_key_is_422(client: httpx.AsyncClient) -> None:
    r = await client.post("/v1/tasks", headers=auth("bridge"), json=task_body("proj_x"))
    assert (r.status_code, r.json()["error"]["code"]) == (422, "INVALID_REQUEST")


async def test_extra_body_field_is_422(client: httpx.AsyncClient) -> None:
    r = await client.post("/v1/tasks", headers=auth("bridge", "extra"), json=task_body("proj_x") | {"plan": "x"})
    assert r.status_code == 422


async def test_unknown_project_is_404_and_leaves_no_receipt(client: httpx.AsyncClient, db: AsyncDatabase[Doc]) -> None:
    r = await client.post("/v1/tasks", headers=auth("bridge", "t-404"), json=task_body("proj_missing"))
    assert (r.status_code, r.json()["error"]["code"]) == (404, "NOT_FOUND")
    assert await db["jobs"].count_documents({"dedupe_key": {"$regex": ":t-404$"}}) == 0


async def test_bad_or_missing_token_is_401(client: httpx.AsyncClient) -> None:
    assert (await client.get("/v1/jobs/job_x")).status_code == 401
    assert (await client.get("/v1/jobs/job_x", headers={"Authorization": "Bearer nope"})).status_code == 401


async def test_bridge_token_on_approve_is_403(client: httpx.AsyncClient, db: AsyncDatabase[Doc]) -> None:
    tid = await planned_task(client, db, "a-403")
    r = await client.post(f"/v1/tasks/{tid}/approve", headers=auth("bridge", "a-403-1"), json={"contract_digest": DIGEST})
    assert (r.status_code, r.json()["error"]["code"]) == (403, "FORBIDDEN")
    assert (await client.post("/v1/tasks", headers=auth("operator", "a-403-2"), json=task_body("proj_x"))).status_code == 403


async def test_wrong_digest_is_409(client: httpx.AsyncClient, db: AsyncDatabase[Doc]) -> None:
    tid = await planned_task(client, db, "a-digest")
    r = await client.post(f"/v1/tasks/{tid}/approve", headers=auth("operator", "a-digest-1"), json={"contract_digest": "sha256:other"})
    assert (r.status_code, r.json()["error"]["code"]) == (409, "DIGEST_MISMATCH")
    assert (await db["tasks"].find_one({"_id": tid}) or {})["status"] == "planned"


async def test_approve_records_approval_and_event(client: httpx.AsyncClient, db: AsyncDatabase[Doc]) -> None:
    tid = await planned_task(client, db, "a-ok")
    assert (await client.get(f"/v1/tasks/{tid}", headers=auth("reader"))).json()["task"]["approval_required"] is True
    r = await client.post(f"/v1/tasks/{tid}/approve", headers=auth("operator", "a-ok-1"), json={"contract_digest": DIGEST})
    assert r.status_code == 200, r.text
    assert r.json() == {"task_id": tid, "status": "approved", "contract_digest": DIGEST, "constraint_epoch": 1}
    task = await db["tasks"].find_one({"_id": tid})
    assert task is not None
    assert {k: task["approval"][k] for k in ("actor_id", "contract_digest", "constraint_epoch")} == {"actor_id": "operator", "contract_digest": DIGEST,
                                                                                                    "constraint_epoch": 1}
    assert (task["status"], task["revision"], task["event_seq"]) == ("approved", 3, 1)
    assert [e["type"] for e in await db["events"].find({"stream_id": tid}).to_list()] == ["task.approved"]
    again = await client.post(f"/v1/tasks/{tid}/approve", headers=auth("operator", "a-ok-2"), json={"contract_digest": DIGEST})
    assert (again.status_code, again.json()["error"]["code"]) == (409, "STALE_REVISION")


def deps(d: Dependant) -> list[Dependant]:
    return [d, *(x for sub in d.dependencies for x in deps(sub))]


async def test_every_route_and_dependency_is_async() -> None:
    routes = [r for r in app.routes if isinstance(r, APIRoute)]
    assert {r.path for r in routes} == {"/v1/projects", "/v1/tasks", "/v1/tasks/{task_id}/approve", "/v1/tasks/{task_id}", "/v1/jobs/{job_id}"}
    sync = [(r.path, d.call) for r in routes for d in deps(r.dependant) if d.call and not inspect.iscoroutinefunction(d.call)]
    assert sync == []
