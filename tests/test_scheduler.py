import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from pymongo.asynchronous.database import AsyncDatabase
from test_api import DIGEST, TOKENS, auth

from api.main import app
from controller import scheduler
from controller.db import Doc
from controller.policies import accept_validation_guarded, promote
from controller.scheduler import LeaseLost, claim, complete, enqueue, fail, heartbeat, on_validation_pass, run_worker
from controller.states import (
    Conflict,
    DelegationState,
    EvolutionRunState,
    JobKind,
    JobStatus,
    TaskState,
    ValidationDecision,
    WorkerRunState,
    new_id,
    transition,
)
from controller.states import Prefix as P

pytestmark = pytest.mark.anyio
EVOLVE = [JobKind.EVOLVE]
CONTRACT: Doc = {"assignments": [
    {"key": "schema", "objective": "Add sessions table", "depends_on": [], "file_scope": ["db/**"], "profile_key": "schema"},
    {"key": "service", "objective": "Login service", "depends_on": ["schema"], "file_scope": ["src/auth/service.py"], "profile_key": "auth"},
    {"key": "routes", "objective": "Login routes", "depends_on": ["schema"], "file_scope": ["src/auth/routes.py"], "profile_key": "auth"}]}


@pytest.fixture
async def client(db: AsyncDatabase[Doc], monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[httpx.AsyncClient]:
    for role, token in TOKENS.items():
        monkeypatch.setenv(f"AIRLOCK_TOKEN_{role.upper()}", token)
    app.state.db = db
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://airlock") as c:
        yield c


@pytest.fixture
async def jobs(db: AsyncDatabase[Doc]) -> AsyncDatabase[Doc]:
    await db["jobs"].delete_many({"kind": JobKind.EVOLVE})  # claim() is global over a kind; evolve is reserved for these tests
    return db


async def queued(db: AsyncDatabase[Doc], payload: Doc | None = None) -> str:
    async with db.client.start_session() as s:
        job_id = await enqueue(s, db, JobKind.EVOLVE, payload or {}, new_id(P.JOB), "proj_sched")
    assert job_id is not None
    return job_id


async def get(db: AsyncDatabase[Doc], coll: str, id_: str) -> Doc:
    doc = await db[coll].find_one({"_id": id_})
    assert doc is not None
    return doc


async def test_concurrent_claimers_exactly_one_wins(jobs: AsyncDatabase[Doc]) -> None:
    jid = await queued(jobs)
    won = [j for j in await asyncio.gather(*(claim(jobs, f"w{i}", EVOLVE) for i in range(5))) if j is not None]
    assert len(won) == 1
    assert (won[0]["_id"], won[0]["status"], won[0]["lease"]["fence"], won[0]["attempts"]) == (jid, "running", 1, 1)


async def test_expired_lease_reclaimed_with_next_fence_and_old_owner_rejected(jobs: AsyncDatabase[Doc]) -> None:
    jid = await queued(jobs)
    assert (first := await claim(jobs, "old", EVOLVE)) is not None
    assert await claim(jobs, "new", EVOLVE) is None
    await jobs["jobs"].update_one({"_id": jid}, {"$set": {"lease.expires_at": datetime.now(UTC) - timedelta(seconds=1)}})
    assert (second := await claim(jobs, "new", EVOLVE)) is not None
    assert (second["lease"]["owner"], second["lease"]["fence"], second["attempts"]) == ("new", first["lease"]["fence"] + 1, 2)
    assert await heartbeat(jobs, jid, "old", 1) is False

    async def noop(_: object) -> None:
        return None

    with pytest.raises(LeaseLost):
        await complete(jobs, jid, "old", 1, noop)
    with pytest.raises(LeaseLost):
        await fail(jobs, jid, "old", 1, "late")
    assert await heartbeat(jobs, jid, "new", 2) is True
    await complete(jobs, jid, "new", 2, noop)
    assert (await get(jobs, "jobs", jid))["status"] == "completed"


async def test_fail_backs_off_with_jitter_then_fails_after_three_attempts(jobs: AsyncDatabase[Doc], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(scheduler.random, "uniform", lambda lo, hi: hi)  # type: ignore[reportUnknownLambdaType]
    jid = await queued(jobs)
    for attempt, delay in ((1, 4), (2, 8)):
        assert (job := await claim(jobs, "w", EVOLVE)) is not None and job["attempts"] == attempt
        assert await fail(jobs, jid, "w", job["lease"]["fence"], f"err{attempt}") == JobStatus.QUEUED
        doc = await get(jobs, "jobs", jid)
        assert (doc["status"], doc["last_error"]) == ("queued", f"err{attempt}")
        assert abs((doc["available_at"] - datetime.now(UTC)).total_seconds() - delay) < 2
        assert await claim(jobs, "w", EVOLVE) is None
        await jobs["jobs"].update_one({"_id": jid}, {"$set": {"available_at": datetime.now(UTC)}})
    assert (job := await claim(jobs, "w", EVOLVE)) is not None and job["attempts"] == 3
    assert await fail(jobs, jid, "w", job["lease"]["fence"], "err3") == JobStatus.FAILED
    assert (await get(jobs, "jobs", jid))["status"] == "failed"
    assert await claim(jobs, "w", EVOLVE) is None


async def test_run_worker_cancels_handler_on_lease_loss_and_fails_on_error(jobs: AsyncDatabase[Doc], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(scheduler.random, "uniform", lambda lo, hi: hi)  # type: ignore[reportUnknownLambdaType]
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def handler(job: Doc) -> None:
        if job["payload"].get("boom"):
            raise RuntimeError("boom")
        started.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    hang = await queued(jobs)
    worker = asyncio.create_task(run_worker(jobs, "w", {JobKind.EVOLVE: handler}, poll=0.05, heartbeat_every=0.05))
    try:
        await asyncio.wait_for(started.wait(), 10)
        await jobs["jobs"].update_one({"_id": hang}, {"$set": {"lease.owner": "thief"}})
        await asyncio.wait_for(cancelled.wait(), 10)
        boom, doc = await queued(jobs, {"boom": True}), {}
        for _ in range(200):
            if (doc := await get(jobs, "jobs", boom)).get("last_error"):
                break
            await asyncio.sleep(0.05)
        assert (doc["status"], doc["attempts"], doc["last_error"]) == ("queued", 1, "RuntimeError('boom')")
    finally:
        worker.cancel()
        await asyncio.wait([worker])


async def approved_task(client: httpx.AsyncClient, db: AsyncDatabase[Doc], key: str, cap: int = 2) -> tuple[str, str, str]:
    hv = new_id(P.HARNESS_VERSION)
    r = await client.post("/v1/projects", headers=auth("operator", f"{key}-p"), json={
        "name": "deleg", "repository": {"registered_root_id": f"repo_del_{key}"}, "baseline_artifact_id": "art_base_01", "harness_version_id": hv})
    proj = r.json()["project_id"]
    await db["harness_versions"].insert_one(harness(proj, hv, 1, cap))
    r = await client.post("/v1/tasks", headers=auth("bridge", f"{key}-t"), json={
        "project_id": proj, "parent_session_id": "claude_parent_01", "request": "Implement login", "base_artifact_id": "art_base_01"})
    tid = r.json()["task_id"]
    async with db.client.start_session() as s:
        await transition(s, db["tasks"], tid, 1, TaskState.PLANNED, {"contract": CONTRACT, "contract_digest": DIGEST, "contract_version": 1})
    assert (await client.post(f"/v1/tasks/{tid}/approve", headers=auth("operator", f"{key}-a"), json={"contract_digest": DIGEST})).status_code == 200
    return tid, proj, hv


def harness(proj: str, hv: str, version: int, cap: int) -> Doc:
    return {"_id": hv, "schema_version": 1, "project_id": proj, "version": version, "created_at": datetime.now(UTC),
            "config": {"delegation": {"isolated_coding": True, "max_parallel_workers": cap}}}


async def delegate(client: httpx.AsyncClient, tid: str, assignment: str, key: str, role: str = "bridge") -> httpx.Response:
    return await client.post(f"/v1/tasks/{tid}/delegations", headers=auth(role, key), json={"assignment_key": assignment})


async def set_status(db: AsyncDatabase[Doc], coll: str, query: Doc, status: str) -> None:
    assert (await db[coll].update_many(query, {"$set": {"status": status}, "$inc": {"revision": 1}})).matched_count >= 1


async def test_delegate_launches_from_contract_and_pins_active_harness(client: httpx.AsyncClient, db: AsyncDatabase[Doc]) -> None:
    tid, _, hv = await approved_task(client, db, "launch")
    r = await delegate(client, tid, "schema", "launch-1")
    assert r.status_code == 202, r.text
    body = r.json()
    assert {k: body[k] for k in ("status", "attempt", "harness_version_id", "constraint_epoch")} == {
        "status": "queued", "attempt": 1, "harness_version_id": hv, "constraint_epoch": 1}
    d, w, job = (await get(db, "delegations", body["delegation_id"]), await get(db, "worker_runs", body["worker_run_id"]),
                 await get(db, "jobs", body["job_id"]))
    assert {k: d[k] for k in ("status", "attempt", "objective", "file_scope", "profile_key", "depends_on", "resolved_input_artifact_id")} == {
        "status": "running", "attempt": 1, "objective": "Add sessions table", "file_scope": ["db/**"], "profile_key": "schema", "depends_on": [],
        "resolved_input_artifact_id": "art_base_01"}
    assert (w["status"], w["harness_version_id"], w["constraint_epoch"], w["input_artifact_id"], w["attempt"]) == ("queued", hv, 1, "art_base_01", 1)
    assert (job["kind"], job["status"], job["payload"]) == ("launch", "queued", {"worker_run_id": w["_id"], "delegation_id": d["_id"]})
    task = await get(db, "tasks", tid)
    assert (task["status"], task["delegation_ids"]) == ("running", [d["_id"]])
    assert [e["type"] for e in await db["events"].find({"stream_id": tid}).sort("sequence").to_list()] == ["task.approved", "delegation.launched"]
    again = await delegate(client, tid, "schema", "launch-1")
    assert (again.status_code, again.content) == (202, r.content)
    assert await db["worker_runs"].count_documents({"task_id": tid}) == 1
    assert (await delegate(client, tid, "schema", "launch-2")).json()["error"]["code"] == "STALE_REVISION"


async def test_dependent_before_dependency_accepted_is_409_then_uses_integrated_input(client: httpx.AsyncClient, db: AsyncDatabase[Doc]) -> None:
    tid, proj, _ = await approved_task(client, db, "deps")
    schema = (await delegate(client, tid, "schema", "deps-1")).json()["delegation_id"]
    r = await delegate(client, tid, "service", "deps-2")
    assert (r.status_code, r.json()["error"]["code"], r.json()["error"]["retryable"]) == (409, "DEPENDENCY_UNMET", False)
    assert await db["delegations"].count_documents({"task_id": tid}) == 1
    await set_status(db, "delegations", {"_id": schema}, DelegationState.ACCEPTED)
    await db["integration_runs"].insert_one({"_id": new_id(P.INTEGRATION_RUN), "schema_version": 1, "project_id": proj, "task_id": tid,
                                             "status": "accepted", "integration_fence": 1, "result_artifact_id": "art_schema_integrated"})
    r = await delegate(client, tid, "service", "deps-3")
    assert r.status_code == 202, r.text
    d = await get(db, "delegations", r.json()["delegation_id"])
    assert (d["depends_on"], d["resolved_input_artifact_id"]) == ([schema], "art_schema_integrated")


async def test_max_parallel_workers_is_409_capacity(client: httpx.AsyncClient, db: AsyncDatabase[Doc]) -> None:
    tid, _, _ = await approved_task(client, db, "cap", cap=1)
    schema = (await delegate(client, tid, "schema", "cap-1")).json()
    await set_status(db, "delegations", {"_id": schema["delegation_id"]}, DelegationState.ACCEPTED)
    r = await delegate(client, tid, "service", "cap-2")
    assert (r.status_code, r.json()["error"]["code"], r.json()["error"]["retryable"]) == (409, "CAPACITY", True)
    await set_status(db, "worker_runs", {"_id": schema["worker_run_id"]}, WorkerRunState.FINISHED)
    assert (await delegate(client, tid, "service", "cap-3")).status_code == 202


async def test_repair_is_attempt_two_on_new_harness_and_third_attempt_fails_delegation(client: httpx.AsyncClient, db: AsyncDatabase[Doc]) -> None:
    tid, proj, hv1 = await approved_task(client, db, "repair")
    did = (await delegate(client, tid, "schema", "repair-1")).json()["delegation_id"]
    await set_status(db, "delegations", {"_id": did}, DelegationState.REPAIR_REQUIRED)
    hv2 = new_id(P.HARNESS_VERSION)
    await db["harness_versions"].insert_one(harness(proj, hv2, 2, 2))
    await db["projects"].update_one({"_id": proj}, {"$set": {"active_harness_version_id": hv2}})
    r = await delegate(client, tid, "schema", "repair-2")
    assert r.status_code == 202, r.text
    assert (r.json()["delegation_id"], r.json()["attempt"], r.json()["harness_version_id"]) == (did, 2, hv2)
    runs = await db["worker_runs"].find({"delegation_id": did}).sort("attempt").to_list()
    assert [(w["attempt"], w["harness_version_id"]) for w in runs] == [(1, hv1), (2, hv2)]
    await set_status(db, "delegations", {"_id": did}, DelegationState.REPAIR_REQUIRED)
    for _ in range(2):
        r = await delegate(client, tid, "schema", "repair-3")
        assert (r.status_code, r.json()["error"]["code"]) == (409, "ATTEMPTS_EXHAUSTED")
    d = await get(db, "delegations", did)
    assert (d["status"], d["attempt"]) == ("failed", 2)
    assert await db["worker_runs"].count_documents({"delegation_id": did}) == 2
    assert (await get(db, "tasks", tid))["status"] == "failed"
    assert await db["events"].count_documents({"stream_id": tid, "type": "task.failed"}) == 1


async def test_epoch_bump_requires_reapproval_and_guards(client: httpx.AsyncClient, db: AsyncDatabase[Doc]) -> None:
    tid, proj, _ = await approved_task(client, db, "epoch")
    assert (await delegate(client, tid, "nope", "epoch-1")).status_code == 404
    assert (await delegate(client, tid, "schema", "epoch-2", role="reader")).status_code == 403
    await db["projects"].update_one({"_id": proj}, {"$inc": {"constraint_epoch": 1}})
    r = await delegate(client, tid, "schema", "epoch-3")
    assert (r.status_code, r.json()["error"]["code"]) == (409, "REAPPROVAL_REQUIRED")
    assert await db["delegations"].count_documents({"task_id": tid}) == 0


async def lease(db: AsyncDatabase[Doc], job_id: str, attempts: int = 1) -> None:
    await db["jobs"].update_one({"_id": job_id}, {"$set": {"status": JobStatus.RUNNING, "lease.owner": "w", "lease.fence": 1, "attempts": attempts}})


async def test_validation_pass_enqueues_serial_integrate_parked_behind_unfinished_one(db: AsyncDatabase[Doc]) -> None:
    proj, tid, did, sib, now = new_id(P.PROJECT), new_id(P.TASK), new_id(P.DELEGATION), new_id(P.DELEGATION), datetime.now(UTC)
    base: Doc = {"schema_version": 1, "project_id": proj, "task_id": tid, "status": DelegationState.VALIDATING, "attempt": 1}
    await db["tasks"].insert_one({"_id": tid, "schema_version": 1, "project_id": proj, "status": TaskState.RUNNING, "revision": 2, "event_seq": 0,
                                  "contract": CONTRACT | {"assignments": CONTRACT["assignments"][:2]}, "created_at": now})
    await db["delegations"].insert_many([{**base, "_id": did, "assignment_key": "service", "revision": 3},
                                         {**base, "_id": sib, "assignment_key": "schema", "revision": 4}])
    deleg = await get(db, "delegations", did)
    async with db.client.start_session() as s:
        first = await s.with_transaction(lambda s: on_validation_pass(s, db, deleg))
        second = await s.with_transaction(lambda s: on_validation_pass(s, db, {**base, "_id": sib, "revision": 4}))
    job, parked, d = await get(db, "jobs", first), await get(db, "jobs", second), await get(db, "delegations", did)
    assert (d["status"], d["revision"]) == ("integrating", 4)
    assert (job["kind"], job["dedupe_key"], job["payload"]) == (
        "integrate", f"{tid}:integrate:1", {"task_id": tid, "delegation_id": did, "attempt": 1, "integration_seq": 1})
    assert (job["available_at"] <= datetime.now(UTC), parked["available_at"], parked["payload"]["integration_seq"]) == (True, scheduler.PARKED, 2)
    assert [e["type"] for e in await db["events"].find({"stream_id": tid}).to_list()] == ["delegation.integrating"] * 2
    with pytest.raises(Conflict):
        async with db.client.start_session() as s:
            await s.with_transaction(lambda s: on_validation_pass(s, db, deleg))
    assert await db["jobs"].count_documents({"task_id": tid, "kind": "integrate"}) == 2
    await lease(db, first)

    async def noop(_: object) -> None:
        return None

    await complete(db, first, "w", 1, noop)
    assert ((await get(db, "jobs", second))["available_at"] <= datetime.now(UTC), (await get(db, "tasks", tid))["integrations_done"]) == (True, 1)


async def test_exhausted_job_fails_its_owner_in_the_same_transaction(client: httpx.AsyncClient, db: AsyncDatabase[Doc]) -> None:
    tid, proj, _ = await approved_task(client, db, "exhaust")
    run = (await delegate(client, tid, "schema", "exhaust-1")).json()
    await lease(db, run["job_id"], attempts=3)
    assert await fail(db, run["job_id"], "w", 1, "docker down") == JobStatus.FAILED
    w, d = await get(db, "worker_runs", run["worker_run_id"]), await get(db, "delegations", run["delegation_id"])
    assert (w["status"], d["status"], (await get(db, "tasks", tid))["status"]) == ("failed", "failed", "failed")
    r = await client.post("/v1/tasks", headers=auth("bridge", "exhaust-2"), json={
        "project_id": proj, "parent_session_id": "claude_parent_01", "request": "Implement login", "base_artifact_id": "art_base_01"})
    plan_task, plan_job = r.json()["task_id"], r.json()["job_id"]
    await lease(db, plan_job, attempts=3)
    assert await fail(db, plan_job, "w", 1, "sdk timeout") == JobStatus.FAILED
    assert (await get(db, "tasks", plan_task))["status"] == "failed"
    failed = [e["payload"] for t in (tid, plan_task) for e in await db["events"].find({"stream_id": t, "type": "job.failed"}).to_list()]
    assert failed == [{"job_id": run["job_id"], "kind": "launch", "error": "docker down", "owners": {w["_id"]: "failed", d["_id"]: "failed"}},
                      {"job_id": plan_job, "kind": "plan", "error": "sdk timeout", "owners": {plan_task: "failed"}}]


async def test_revoke_mid_run_is_inconclusive_and_next_delegate_needs_reapproval(client: httpx.AsyncClient, db: AsyncDatabase[Doc]) -> None:
    tid, proj, _ = await approved_task(client, db, "revoke-mid")
    run = (await delegate(client, tid, "schema", "revoke-mid-1")).json()
    assert (await client.post(f"/v1/projects/{proj}/revoke", headers=auth("operator", "revoke-mid-2"))).status_code == 200
    w = await get(db, "worker_runs", run["worker_run_id"])
    async with db.client.start_session() as s:
        assert await s.with_transaction(lambda s: accept_validation_guarded(s, db, ValidationDecision.PASS, w)) == ValidationDecision.INCONCLUSIVE
    r = await delegate(client, tid, "routes", "revoke-mid-3")
    assert (r.status_code, r.json()["error"]["code"]) == (409, "REAPPROVAL_REQUIRED")
    assert await db["delegations"].count_documents({"task_id": tid}) == 1


async def test_promotion_pins_next_delegation_while_running_one_keeps_its_version(client: httpx.AsyncClient, db: AsyncDatabase[Doc]) -> None:
    tid, proj, hv1 = await approved_task(client, db, "promo")
    schema = (await delegate(client, tid, "schema", "promo-1")).json()
    await set_status(db, "worker_runs", {"_id": schema["worker_run_id"]}, WorkerRunState.RUNNING)
    hv2, evo = new_id(P.HARNESS_VERSION), new_id(P.EVOLUTION_RUN)
    await db["harness_versions"].insert_one(harness(proj, hv2, 2, 2))
    await db["evolution_runs"].insert_one({"_id": evo, "schema_version": 1, "project_id": proj, "status": EvolutionRunState.ELIGIBLE, "revision": 3,
                                           "constraint_epoch": 1, "baseline_harness_version_id": hv1, "candidate_harness_version_id": hv2,
                                           "created_at": datetime.now(UTC)})
    async with db.client.start_session() as s:
        assert await s.with_transaction(lambda s: promote(s, db, evo, "evaluator")) == EvolutionRunState.PROMOTED
    await set_status(db, "delegations", {"_id": schema["delegation_id"]}, DelegationState.ACCEPTED)
    r = await delegate(client, tid, "service", "promo-2")
    assert (r.status_code, r.json()["harness_version_id"]) == (202, hv2)
    pins = {w["_id"]: (w["status"], w["harness_version_id"]) for w in await db["worker_runs"].find({"task_id": tid}).to_list()}
    assert pins == {schema["worker_run_id"]: ("running", hv1), r.json()["worker_run_id"]: ("queued", hv2)}


async def test_operator_cancel_closes_task_and_unfinished_work_once(client: httpx.AsyncClient, db: AsyncDatabase[Doc]) -> None:
    tid, _, _ = await approved_task(client, db, "cancel")
    run = (await delegate(client, tid, "schema", "cancel-1")).json()
    assert (await client.post(f"/v1/tasks/{tid}/cancel", headers=auth("bridge", "cancel-2"))).status_code == 403
    r = await client.post(f"/v1/tasks/{tid}/cancel", headers=auth("operator", "cancel-3"))
    assert r.status_code == 200, r.text
    statuses = [(await get(db, c, i))["status"] for c, i in (("tasks", tid), ("delegations", run["delegation_id"]), ("worker_runs", run["worker_run_id"]))]
    assert statuses == ["cancelled", "cancelled", "cancelled"]
    r = await client.post(f"/v1/tasks/{tid}/cancel", headers=auth("operator", "cancel-4"))
    assert (r.status_code, r.json()["error"]["code"]) == (409, "STALE_REVISION")


def test_regression_gate_passes_progress_and_blocks_regressions() -> None:
    from controller.handlers import regression_gate

    def run(decision: str, partial: bool = False, **checks: str) -> Doc:
        return {"decision": decision, "observer_ok": True, "behavior_delta": {"partial_write": partial},
                "checks": [{"id": k, "status": v} for k, v in checks.items()]}
    ref = run("repair_required", http_bulk="fail", rows_unchanged="pass")
    assert regression_gate(run("repair_required", http_bulk="fail", rows_unchanged="pass"), ref) == "pass"
    assert regression_gate(run("repair_required", http_bulk="fail", rows_unchanged="fail"), ref) == "repair_required"
    assert regression_gate(run("repair_required", True, http_bulk="fail", rows_unchanged="pass"), ref) == "repair_required"
    assert regression_gate(run("inconclusive", http_bulk="error", rows_unchanged="pass"), ref) == "inconclusive"
    assert regression_gate(run("repair_required", http_bulk="fail", rows_unchanged="pass"), None) == "repair_required"
