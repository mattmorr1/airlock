import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from pymongo.asynchronous.client_session import AsyncClientSession
from pymongo.asynchronous.database import AsyncDatabase

from controller.db import Doc
from controller.scheduler import LeaseLost, complete, enqueue, integration_is_final
from controller.states import (
    Conflict,
    DelegationState,
    IllegalTransition,
    JobKind,
    JobStatus,
    TaskState,
    ValidationDecision,
    WorkerRunState,
    accept_validation,
    append_event,
    new_id,
    transition,
)
from controller.states import Prefix as P

pytestmark = pytest.mark.anyio
COLLS = ("projects", "tasks", "delegations", "worker_runs", "jobs", "events", "validation_runs")


@dataclass(frozen=True, slots=True)
class World:
    proj: str
    task: str
    deleg: str
    sibling: str
    worker: str
    job: str


async def world(db: AsyncDatabase[Doc], project_epoch: int = 1, keys: tuple[str, ...] = ("schema", "service")) -> World:
    w, now = World(new_id(P.PROJECT), new_id(P.TASK), new_id(P.DELEGATION), new_id(P.DELEGATION), new_id(P.WORKER_RUN), new_id(P.JOB)), datetime.now(UTC)
    base = {"schema_version": 1, "project_id": w.proj}
    await db["projects"].insert_one({"_id": w.proj, "schema_version": 1, "constraint_epoch": project_epoch, "event_seq": 0, "revision": 1, "created_at": now})
    await db["tasks"].insert_one({**base, "_id": w.task, "status": TaskState.RUNNING, "revision": 1, "event_seq": 0, "created_at": now,
                                  "contract": {"assignments": [{"key": k} for k in keys]}})
    await db["delegations"].insert_many([
        {**base, "_id": w.deleg, "task_id": w.task, "assignment_key": "service", "status": DelegationState.VALIDATING, "attempt": 1, "revision": 1},
        {**base, "_id": w.sibling, "task_id": w.task, "assignment_key": "schema", "status": DelegationState.ACCEPTED, "attempt": 1, "revision": 4}])
    await db["worker_runs"].insert_one({**base, "_id": w.worker, "task_id": w.task, "delegation_id": w.deleg, "attempt": 1, "status": WorkerRunState.FINISHED,
                                        "revision": 1, "constraint_epoch": 1})
    await db["jobs"].insert_one({**base, "_id": w.job, "task_id": w.task, "kind": JobKind.VALIDATE, "payload": {"worker_run_id": w.worker},
                                 "status": JobStatus.RUNNING, "dedupe_key": w.job, "available_at": now, "attempts": 1,
                                 "lease": {"owner": "w1", "expires_at": now, "fence": 3}, "result_ref": None, "created_at": now})
    return w


async def snapshot(db: AsyncDatabase[Doc], w: World) -> dict[str, list[Doc]]:
    return {c: await db[c].find({"$or": [{"_id": w.proj}, {"project_id": w.proj}]}).sort("_id").to_list() for c in COLLS}


def on_validated(db: AsyncDatabase[Doc], w: World, inject: bool = False) -> Callable[[AsyncClientSession], Awaitable[None]]:
    async def apply(s: AsyncClientSession) -> None:
        await transition(s, db["delegations"], w.deleg, 1, DelegationState.INTEGRATING)
        await append_event(s, w.task, db["tasks"], "delegation.integrating", "runner", {"delegation_id": w.deleg})
        final = await integration_is_final(s, db, w.task, w.deleg)
        await enqueue(s, db, JobKind.INTEGRATE, {"delegation_id": w.deleg, "final": final}, f"{w.task}:integrate:{w.deleg}", w.proj, w.task)
        if inject:
            raise RuntimeError("injected")
    return apply


async def test_unreachable_target_raises_before_touching_db(db: AsyncDatabase[Doc]) -> None:
    w = await world(db)
    before = await snapshot(db, w)
    async with db.client.start_session() as s:
        with pytest.raises(IllegalTransition):
            await transition(s, db["tasks"], w.task, 1, TaskState.DRAFT)
    assert await snapshot(db, w) == before


async def test_illegal_source_state_and_stale_revision_raise_conflict(db: AsyncDatabase[Doc]) -> None:
    w = await world(db)
    before = await snapshot(db, w)
    async with db.client.start_session() as s:
        with pytest.raises(Conflict) as e:
            await transition(s, db["tasks"], w.task, 1, TaskState.APPROVED)
        assert (e.value.entity, e.value.id, e.value.revision) == ("tasks", w.task, 1)
        with pytest.raises(Conflict):
            await transition(s, db["tasks"], w.task, 0, TaskState.COMPLETED)
        assert await snapshot(db, w) == before
        await transition(s, db["tasks"], w.task, 1, TaskState.COMPLETED, {"final_artifact_id": "art_x"})
    task = await db["tasks"].find_one({"_id": w.task}) or {}
    assert (task["status"], task["revision"], task["final_artifact_id"]) == (TaskState.COMPLETED, 2, "art_x")


async def test_complete_commits_job_transitions_events_and_successor(db: AsyncDatabase[Doc]) -> None:
    w = await world(db)
    await complete(db, w.job, "w1", 3, on_validated(db, w), result_ref="val_x")
    job = await db["jobs"].find_one({"_id": w.job}) or {}
    deleg = await db["delegations"].find_one({"_id": w.deleg}) or {}
    successor = await db["jobs"].find_one({"dedupe_key": f"{w.task}:integrate:{w.deleg}"}) or {}
    events = await db["events"].find({"stream_id": w.task}).to_list()
    assert (job["status"], job["result_ref"]) == (JobStatus.COMPLETED, "val_x")
    assert (deleg["status"], deleg["revision"]) == (DelegationState.INTEGRATING, 2)
    assert (successor["status"], successor["kind"], successor["payload"]["final"]) == (JobStatus.QUEUED, JobKind.INTEGRATE, True)
    assert [(e["sequence"], e["project_id"], e["task_id"]) for e in events] == [(1, w.proj, w.task)]


async def test_integration_not_final_while_an_assignment_is_unaccepted(db: AsyncDatabase[Doc]) -> None:
    w = await world(db, keys=("schema", "service", "routes"))
    async with db.client.start_session() as s:
        assert not await integration_is_final(s, db, w.task, w.deleg)


async def test_exception_inside_complete_leaves_no_partial_writes(db: AsyncDatabase[Doc]) -> None:
    w = await world(db)
    before = await snapshot(db, w)
    with pytest.raises(RuntimeError, match="injected"):
        await complete(db, w.job, "w1", 3, on_validated(db, w, inject=True))
    assert await snapshot(db, w) == before


@pytest.mark.parametrize(("owner", "fence"), [("w1", 2), ("w2", 3)])
async def test_stale_lease_is_rejected_and_changes_nothing(db: AsyncDatabase[Doc], owner: str, fence: int) -> None:
    w, called = await world(db), False

    async def apply(s: AsyncClientSession) -> None:
        nonlocal called
        called = True

    before = await snapshot(db, w)
    with pytest.raises(LeaseLost):
        await complete(db, w.job, owner, fence, apply)
    assert not called
    assert await snapshot(db, w) == before


async def test_concurrent_appends_are_gapless_and_leave_revision_alone(db: AsyncDatabase[Doc]) -> None:
    w, n = await world(db), 20

    async def one() -> None:
        async with db.client.start_session() as s:
            await s.with_transaction(lambda s: append_event(s, w.task, db["tasks"], "tick", "tester", {}))

    async with asyncio.TaskGroup() as tg:
        for _ in range(n):
            tg.create_task(one())
    seqs = [e["sequence"] for e in await db["events"].find({"stream_id": w.task}).sort("sequence").to_list()]
    task = await db["tasks"].find_one({"_id": w.task}) or {}
    assert seqs == list(range(1, n + 1))
    assert (task["event_seq"], task["revision"]) == (n, 1)


@pytest.mark.parametrize(("project_epoch", "expected"), [(2, ValidationDecision.INCONCLUSIVE), (1, ValidationDecision.PASS)])
async def test_stale_epoch_at_acceptance_is_inconclusive(db: AsyncDatabase[Doc], project_epoch: int, expected: ValidationDecision) -> None:
    w = await world(db, project_epoch=project_epoch)
    val = new_id(P.VALIDATION_RUN)

    async def apply(s: AsyncClientSession) -> None:
        worker, project = await db["worker_runs"].find_one({"_id": w.worker}, session=s), await db["projects"].find_one({"_id": w.proj}, session=s)
        assert worker and project
        decision = accept_validation(ValidationDecision.PASS, worker, project)
        await db["validation_runs"].insert_one({"_id": val, "schema_version": 1, "project_id": w.proj, "task_id": w.task, "decision": decision,
                                                "created_at": datetime.now(UTC)}, session=s)

    await complete(db, w.job, "w1", 3, apply)
    assert (await db["validation_runs"].find_one({"_id": val}) or {})["decision"] == expected
