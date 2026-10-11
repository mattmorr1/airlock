import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from conftest import throwaway
from pymongo.asynchronous.client_session import AsyncClientSession
from pymongo.asynchronous.database import AsyncDatabase
from test_api import TOKENS, auth
from test_events import idle, ids, sse
from test_planner import SPEC, asg, plan
from test_scheduler import harness

import api.events
from api.main import app
from controller.db import Doc
from controller.events import Doorbell
from controller.planner import run_plan_job
from controller.policies import accept_validation_guarded
from controller.scheduler import PARKED, claim, complete, enqueue, heartbeat, integration_is_final, on_integration_pass, on_validation_pass
from controller.states import DelegationState, JobKind, Prefix, TaskState, ValidationDecision, WorkerRunState, append_event, new_id, transition

pytestmark = pytest.mark.anyio
OWNER = "stub-worker"
PLANS: dict[str, Doc] = {"Implement login and sessions": SPEC, "Two independent edits": plan(asg("left", ["src/left.py"]), asg("right", ["src/right.py"]))}
type Handler = Callable[[AsyncDatabase[Doc], Doc], Awaitable[object]]


@pytest.fixture(scope="module")
async def edb() -> AsyncIterator[AsyncDatabase[Doc]]:
    """A database of its own: the stub worker claims every runnable job, so other modules' leftovers must not be in reach."""
    async with throwaway() as database:
        yield database


@pytest.fixture
async def http(edb: AsyncDatabase[Doc], monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[httpx.AsyncClient]:
    for role, token in TOKENS.items():
        monkeypatch.setenv(f"AIRLOCK_TOKEN_{role.upper()}", token)
    monkeypatch.setattr(api.events, "HEARTBEAT", 0.3)
    app.state.db = edb
    async with Doorbell(edb) as app.state.doorbell, httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://airlock") as c:
        yield c


async def one(db: AsyncDatabase[Doc], coll: str, id_: str, s: AsyncClientSession | None = None) -> Doc:
    doc = await db[coll].find_one({"_id": id_}, session=s)
    assert doc is not None, f"{coll} {id_}"
    return doc


async def artifact(db: AsyncDatabase[Doc], id_: str, project_id: str, task_id: str, kind: str, **extra: object) -> str:
    # Outside the transaction and idempotent, as B's GridFS/git work must be.
    await db["artifacts"].update_one({"_id": id_}, {"$setOnInsert": {"schema_version": 1, "project_id": project_id, "task_id": task_id, "kind": kind,
                                                                      **extra, "created_at": datetime.now(UTC)}}, upsert=True)
    return id_


async def validation(s: AsyncClientSession, db: AsyncDatabase[Doc], project_id: str, task_id: str, kind: str, candidate: str,
                     decision: ValidationDecision) -> str:
    vid = new_id(Prefix.VALIDATION_RUN)
    await db["validation_runs"].insert_one({"_id": vid, "schema_version": 1, "project_id": project_id, "task_id": task_id, "kind": kind,
                                            "decision": decision, "inputs": {"candidate_artifact_id": candidate}, "created_at": datetime.now(UTC)}, session=s)
    return vid


async def propose(request: str, _checkout: Path, _errors: list[str] | None) -> object:
    return PLANS[request]


async def plan_job(db: AsyncDatabase[Doc], job: Doc) -> object:
    return await run_plan_job(db, job, OWNER, job["lease"]["fence"], Path("."), propose=propose)


async def launch(db: AsyncDatabase[Doc], job: Doc) -> object:
    p = job["payload"]
    art = await artifact(db, f"art_snap_{p['worker_run_id']}", job["project_id"], job["task_id"], "code_snapshot", worker_run_id=p["worker_run_id"])

    async def apply(s: AsyncClientSession) -> None:
        wr, d = await one(db, "worker_runs", p["worker_run_id"], s), await one(db, "delegations", p["delegation_id"], s)
        for i, to in enumerate((WorkerRunState.STARTING, WorkerRunState.RUNNING, WorkerRunState.SNAPSHOTTING, WorkerRunState.FINISHED)):
            await transition(s, db["worker_runs"], wr["_id"], wr["revision"] + i, to, {"output_artifact_id": art} if to is WorkerRunState.FINISHED else None)
        await transition(s, db["delegations"], d["_id"], d["revision"], DelegationState.VALIDATING)
        await enqueue(s, db, JobKind.VALIDATE, p | {"artifact_id": art}, f"{wr['_id']}:validate", job["project_id"], job["task_id"])
        await append_event(s, job["task_id"], db["tasks"], "delegation.validating", OWNER, {"delegation_id": d["_id"], "artifact_id": art})
    return await complete(db, job["_id"], OWNER, job["lease"]["fence"], apply, art)


async def validate(db: AsyncDatabase[Doc], job: Doc) -> object:
    p = job["payload"]

    async def apply(s: AsyncClientSession) -> str:
        wr, d = await one(db, "worker_runs", p["worker_run_id"], s), await one(db, "delegations", p["delegation_id"], s)
        decision = await accept_validation_guarded(s, db, ValidationDecision.PASS, wr)
        await validation(s, db, job["project_id"], job["task_id"], "worker", p["artifact_id"], decision)
        assert decision is ValidationDecision.PASS
        return await on_validation_pass(s, db, d)
    return await complete(db, job["_id"], OWNER, job["lease"]["fence"], apply)


async def integrate(db: AsyncDatabase[Doc], job: Doc) -> object:
    p = job["payload"]
    tid, did, seq = p["task_id"], p["delegation_id"], p["integration_seq"]
    final = await integration_is_final(None, db, tid, did)
    art = await artifact(db, f"art_int_{tid}_{seq}", job["project_id"], tid, "integration", delegation_id=did, final=final)

    async def apply(s: AsyncClientSession) -> TaskState | None:
        vid = await validation(s, db, job["project_id"], tid, "final" if final else "stage", art, ValidationDecision.PASS)
        return await on_integration_pass(s, db, await one(db, "delegations", did, s), seq, art, vid)
    assert await complete(db, job["_id"], OWNER, job["lease"]["fence"], apply, art) == (TaskState.COMPLETED if final else None)
    return final


HANDLERS: dict[JobKind, Handler] = {JobKind.PLAN: plan_job, JobKind.LAUNCH: launch, JobKind.VALIDATE: validate, JobKind.INTEGRATE: integrate}


async def drain(db: AsyncDatabase[Doc], done: Callable[[], Awaitable[bool]]) -> None:
    """Run the stub worker on the real jobs library until `done`; nothing claimable before then is a stall, not a wait."""
    async with asyncio.timeout(30):
        while not await done():
            job = await claim(db, OWNER, HANDLERS)
            assert job is not None, "stalled: nothing claimable"
            assert await heartbeat(db, job["_id"], OWNER, job["lease"]["fence"])
            await HANDLERS[job["kind"]](db, job)


def status_is(db: AsyncDatabase[Doc], coll: str, query: Doc, want: str) -> Callable[[], Awaitable[bool]]:
    async def check() -> bool:
        return await db[coll].count_documents(query) > 0 and await db[coll].count_documents(query | {"status": {"$ne": want}}) == 0
    return check


async def approved(http: httpx.AsyncClient, db: AsyncDatabase[Doc], key: str, request: str, sent: list[tuple[str, str, str, Doc | None, httpx.Response]]
                   ) -> tuple[str, str]:
    """Register a project (operator), create a task (bridge), drain until planned, approve with the digest read back."""
    async def post(path: str, role: str, k: str, body: Doc | None = None) -> httpx.Response:
        r = await http.post(path, headers=auth(role, k), json=body)
        assert r.status_code in (200, 201, 202), r.text
        sent.append((path, role, k, body, r))
        return r

    hv = new_id(Prefix.HARNESS_VERSION)
    proj = (await post("/v1/projects", "operator", f"{key}-p", {"name": key, "repository": {"registered_root_id": f"repo_{key}"},
                                                                  "baseline_artifact_id": "art_base_01", "harness_version_id": hv})).json()["project_id"]
    await db["harness_versions"].insert_one(harness(proj, hv, 1, 2))
    tid = (await post("/v1/tasks", "bridge", f"{key}-t", {"project_id": proj, "parent_session_id": "claude_parent_01", "request": request,
                                                          "base_artifact_id": "art_base_01"})).json()["task_id"]
    await drain(db, status_is(db, "tasks", {"_id": tid}, TaskState.PLANNED))
    got = (await http.get(f"/v1/tasks/{tid}", headers=auth("reader"))).json()["task"]
    assert got["approval_required"] and [a["key"] for a in got["contract"]["assignments"]] == [a["key"] for a in PLANS[request]["assignments"]]
    await post(f"/v1/tasks/{tid}/approve", "operator", f"{key}-a", {"contract_digest": got["contract_digest"]})
    return proj, tid


async def snapshot(db: AsyncDatabase[Doc]) -> dict[str, list[Doc]]:
    return {c: await db[c].find().sort("_id").to_list() for c in sorted(await db.list_collection_names())}


async def test_control_plane_end_to_end(edb: AsyncDatabase[Doc], http: httpx.AsyncClient) -> None:
    db, start, sent = edb, time.monotonic(), list[tuple[str, str, str, Doc | None, httpx.Response]]()
    _, tid = await approved(http, db, "e2e", "Implement login and sessions", sent)

    async def delegate(assignment: str, key: str) -> httpx.Response:
        r = await http.post(f"/v1/tasks/{tid}/delegations", headers=auth("bridge", key), json={"assignment_key": assignment})
        if r.status_code == 202:
            sent.append((f"/v1/tasks/{tid}/delegations", "bridge", key, {"assignment_key": assignment}, r))
        return r

    assert (await delegate("schema", "e2e-schema")).status_code == 202
    early = await delegate("service", "e2e-service-early")
    assert (early.status_code, early.json()["error"]["code"]) == (409, "DEPENDENCY_UNMET")
    await drain(db, status_is(db, "delegations", {"task_id": tid, "assignment_key": "schema"}, DelegationState.ACCEPTED))
    assert [(await delegate(k, f"e2e-{k}")).status_code for k in ("service", "routes")] == [202, 202]
    await drain(db, status_is(db, "tasks", {"_id": tid}, TaskState.COMPLETED))

    task, delegs = await one(db, "tasks", tid), {d["assignment_key"]: d for d in await db["delegations"].find({"task_id": tid}).to_list()}
    final = await db["integration_runs"].find_one({"task_id": tid}, sort=[("integration_fence", -1)])
    assert final is not None and final["integration_fence"] == 3
    assert (task["status"], task["final_artifact_id"]) == ("completed", final["result_artifact_id"])
    assert (await one(db, "validation_runs", task["final_validation_run_id"]))["kind"] == "final"
    assert {k: d["status"] for k, d in delegs.items()} == dict.fromkeys(("schema", "service", "routes"), "accepted")
    assert {delegs[k]["resolved_input_artifact_id"] for k in ("service", "routes")} == {delegs["schema"]["accepted_artifact_id"]}

    events = await db["events"].find({"stream_id": tid}).sort("sequence").to_list()
    n = task["event_seq"]
    assert [e["sequence"] for e in events] == list(range(1, n + 1))
    assert events[-1]["type"] == "task.completed"
    async with sse(f"/v1/tasks/{tid}/events") as frames:
        assert await ids(frames, n) == list(range(1, n + 1))
        await idle(frames)
    async with sse(f"/v1/tasks/{tid}/events", last_event_id=n // 2) as frames:
        assert await ids(frames, n - n // 2) == list(range(n // 2 + 1, n + 1))
        await idle(frames)

    before = await snapshot(db)
    for path, role, key, body, first in sent:
        again = await http.post(path, headers=auth(role, key), json=body)
        assert (again.status_code, again.content) == (first.status_code, first.content), path
    assert await snapshot(db) == before
    assert time.monotonic() - start < 60


async def test_sibling_validations_passing_back_to_back_both_integrate_and_complete(edb: AsyncDatabase[Doc], http: httpx.AsyncClient) -> None:
    db = edb
    _, tid = await approved(http, db, "pair", "Two independent edits", [])
    for k in ("left", "right"):
        assert (await http.post(f"/v1/tasks/{tid}/delegations", headers=auth("bridge", f"pair-{k}"), json={"assignment_key": k})).status_code == 202
    await drain(db, status_is(db, "delegations", {"task_id": tid}, DelegationState.INTEGRATING))
    jobs = await db["jobs"].find({"task_id": tid, "kind": JobKind.INTEGRATE}).sort("payload.integration_seq").to_list()
    assert [(j["status"], j["available_at"] == PARKED) for j in jobs] == [("queued", False), ("queued", True)]
    await drain(db, status_is(db, "tasks", {"_id": tid}, TaskState.COMPLETED))
    flow = [e["type"] for e in await db["events"].find({"stream_id": tid, "type": {"$regex": r"^(delegation\.(integrating|accepted)|task\.completed)$"}})
            .sort("sequence").to_list()]
    assert flow == ["delegation.integrating", "delegation.integrating", "delegation.accepted", "delegation.accepted", "task.completed"]
    assert (await one(db, "tasks", tid))["final_artifact_id"] == f"art_int_{tid}_2"
