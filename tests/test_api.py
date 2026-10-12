import asyncio
import inspect
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute, iter_route_contexts
from pymongo.asynchronous.database import AsyncDatabase

from api.main import app
from controller.db import Doc
from controller.events import Doorbell
from controller.policies import accept_validation_guarded
from controller.states import TaskState, ValidationDecision, transition

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
    async with Doorbell(db) as app.state.doorbell, httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://airlock") as c:
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


async def test_rollback_to_unvalidated_is_409_and_validated_rollback_replays(client: httpx.AsyncClient, db: AsyncDatabase[Doc]) -> None:
    proj = await project(client, "rb")
    before = await db["projects"].find_one({"_id": proj})
    r = await client.post(f"/v1/projects/{proj}/rollback", headers=auth("operator", "rb-1"), json={"harness_version_id": "hv_999"})
    assert (r.status_code, r.json()["error"]["code"]) == (409, "NOT_VALIDATED")
    assert await db["projects"].find_one({"_id": proj}) == before
    ok, replay = [await client.post(f"/v1/projects/{proj}/rollback", headers=auth("operator", "rb-2"), json={"harness_version_id": "hv_001"})
                  for _ in range(2)]
    assert (ok.status_code, ok.json(), replay.content) == (200, {"project_id": proj, "active_harness_version_id": "hv_001"}, ok.content)
    p = await db["projects"].find_one({"_id": proj})
    assert p is not None and (p["harness_generation"], p["constraint_epoch"], p["event_seq"]) == (2, 1, 1)


async def test_bridge_token_on_rollback_and_revoke_is_403(client: httpx.AsyncClient) -> None:
    proj = await project(client, "pol-403")
    rs = [await client.post(f"/v1/projects/{proj}/rollback", headers=auth("bridge", "pol-403-1"), json={"harness_version_id": "hv_001"}),
          await client.post(f"/v1/projects/{proj}/revoke", headers=auth("bridge", "pol-403-2"))]
    assert [(r.status_code, r.json()["error"]["code"]) for r in rs] == [(403, "FORBIDDEN")] * 2


async def test_revoke_bumps_epoch_once_per_key_and_stales_inflight_validation(client: httpx.AsyncClient, db: AsyncDatabase[Doc]) -> None:
    proj = await project(client, "rv")
    run: Doc = {"project_id": proj, "constraint_epoch": 1}
    first, replay = [await client.post(f"/v1/projects/{proj}/revoke", headers=auth("operator", "rv-1")) for _ in range(2)]
    assert (first.status_code, first.json(), replay.content) == (200, {"project_id": proj, "constraint_epoch": 2}, first.content)
    p = await db["projects"].find_one({"_id": proj})
    assert p is not None and p["constraint_epoch"] == 2
    events = await db["events"].find({"stream_id": proj}).to_list()
    assert [(e["type"], e["actor_id"], e["payload"]) for e in events] == [("project.revoked", "operator", {"constraint_epoch": 2})]
    async with db.client.start_session() as s:
        assert await s.with_transaction(lambda s: accept_validation_guarded(s, db, ValidationDecision.PASS, run)) == ValidationDecision.INCONCLUSIVE


async def test_revoke_unknown_project_is_404(client: httpx.AsyncClient) -> None:
    r = await client.post("/v1/projects/proj_missing/revoke", headers=auth("operator", "rv-404"))
    assert (r.status_code, r.json()["error"]["code"]) == (404, "NOT_FOUND")


def deps(d: Dependant) -> list[Dependant]:
    return [d, *(x for sub in d.dependencies for x in deps(sub))]


async def test_every_route_and_dependency_is_async() -> None:
    routes = [r for c in iter_route_contexts(app.routes) if isinstance(r := c.original_route, APIRoute)]
    assert {r.path for r in routes} == {"/v1/projects", "/v1/projects/register", "/v1/tasks", "/v1/tasks/{task_id}/approve", "/v1/tasks/{task_id}", "/v1/jobs/{job_id}",
                                       "/v1/tasks/{task_id}/delegations", "/v1/tasks/{task_id}/cancel", "/v1/projects/{project_id}/tasks",
                                       "/v1/tasks/{task_id}/events", "/v1/projects/{project_id}/events",
                                       "/v1/projects/{project_id}/rollback", "/v1/projects/{project_id}/revoke",
                                       "/v1/hooks/pre-tool-use", "/v1/hooks/lifecycle", "/v1/hud"}
    sync = [(r.path, d.call) for r in routes for d in deps(r.dependant) if d.call and not inspect.iscoroutinefunction(d.call)]
    assert sync == []


async def test_task_project_from_cwd_and_base_defaults_and_checks(client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    proj = await project(client, "cwdroot")
    monkeypatch.setenv("AIRLOCK_ROOTS", f'{{"repo_cwdroot": "{tmp_path}"}}')
    (sub := tmp_path / "app").mkdir()
    body = {"parent_session_id": "claude_parent_01", "request": "Add bulk"}
    r = await client.post("/v1/tasks", headers=auth("bridge", "cwd-1"), json=body | {"cwd": str(sub)})
    assert r.status_code == 202, r.text
    task = (await client.get(f"/v1/tasks/{r.json()['task_id']}", headers=auth("bridge"))).json()["task"]
    assert (task["project_id"], task["base_artifact_id"], task["launchable"], task["last_error"]) == (proj, "art_base_01", [], None)
    r = await client.post("/v1/tasks", headers=auth("bridge", "cwd-2"), json=body | {"cwd": "/nowhere"})
    assert (r.status_code, r.json()["error"]["code"]) == (404, "NOT_FOUND")
    r = await client.post("/v1/tasks", headers=auth("bridge", "cwd-3"), json=body | {"project_id": proj, "base_artifact_id": "art_stale"})
    assert r.status_code == 404 and "art_stale" in r.json()["error"]["message"]
    assert (await client.post("/v1/tasks", headers=auth("bridge", "cwd-4"), json=body)).status_code == 422


async def test_task_resolves_project_by_db_root_path_without_env_roots(client: httpx.AsyncClient, db: AsyncDatabase[Doc],
                                                                       monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Auto-registered projects carry repository.root_path; cwd resolves off the DB even with no AIRLOCK_ROOTS entry."""
    monkeypatch.delenv("AIRLOCK_ROOTS", raising=False)
    proj = await project(client, "dbroot")
    await db["projects"].update_one({"_id": proj}, {"$set": {"repository.root_path": str(tmp_path.resolve())}})
    (sub := tmp_path / "pkg" / "deep").mkdir(parents=True)
    body = {"parent_session_id": "claude_parent_01", "request": "Add bulk", "cwd": str(sub)}
    r = await client.post("/v1/tasks", headers=auth("bridge", "dbroot-1"), json=body)
    assert r.status_code == 202, r.text
    task = (await client.get(f"/v1/tasks/{r.json()['task_id']}", headers=auth("bridge"))).json()["task"]
    assert task["project_id"] == proj
    # A cwd outside any registered root (env or DB) still 404s.
    r = await client.post("/v1/tasks", headers=auth("bridge", "dbroot-2"), json=body | {"cwd": "/nowhere"})
    assert (r.status_code, r.json()["error"]["code"]) == (404, "NOT_FOUND")


async def test_session_contract_skips_planner_and_awaits_approval(client: httpx.AsyncClient, db: AsyncDatabase[Doc]) -> None:
    proj = await project(client, "sesscontract")
    plan = {"constraints": [], "acceptance": ["works"], "interfaces": {},
            "assignments": [{"key": "a", "objective": "do it", "depends_on": [], "file_scope": ["app/**"], "profile_key": "backend"}]}
    r = await client.post("/v1/tasks", headers=auth("bridge", "sc-1"), json=task_body(proj) | {"contract": plan})
    assert (r.status_code, r.json()["status"], r.json()["job_id"]) == (202, "planned", None), r.text
    task = await db["tasks"].find_one({"_id": r.json()["task_id"]})
    assert task is not None and task["contract"]["assignments"][0]["key"] == "a" and task["contract_digest"].startswith("sha256:")
    assert await db["jobs"].count_documents({"task_id": task["_id"]}) == 0
    r = await client.post("/v1/tasks", headers=auth("bridge", "sc-2"), json=task_body(proj) | {"contract": plan | {"assignments": []}})
    assert (r.status_code, r.json()["error"]["code"]) == (422, "CONTRACT_INVALID")
