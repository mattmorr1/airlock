import asyncio
import logging
import random
from collections.abc import Awaitable, Callable, Coroutine, Iterable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, NoReturn

from pymongo import ReturnDocument
from pymongo.asynchronous.client_session import AsyncClientSession
from pymongo.asynchronous.database import AsyncDatabase

from controller.db import Doc
from controller.states import (
    ALLOWED,
    SCHEMA_VERSION,
    DelegationState,
    Entity,
    IntegrationRunState,
    JobKind,
    JobStatus,
    Prefix,
    State,
    TaskState,
    WorkerRunState,
    append_event,
    new_id,
    transition,
)

log = logging.getLogger(__name__)
LEASE, HEARTBEAT_SECONDS, MAX_JOB_ATTEMPTS = timedelta(seconds=30), 10.0, 3
PARKED = datetime(9999, 1, 1, tzinfo=UTC)  # available_at of an integrate job waiting on its task's previous integration


class LeaseLost(Exception):
    pass


async def complete[R](db: AsyncDatabase[Doc], job_id: str, owner: str, fence: int, apply: Callable[[AsyncClientSession], Awaitable[R]],
                      result_ref: str | None = None) -> R:
    """Complete a leased job and run `apply` in one transaction; `apply` must be DB-only, since it is retried on transient errors.
    Completing an integrate job releases the task's next parked integration."""
    async def run(session: AsyncClientSession) -> R:
        job = await db["jobs"].find_one_and_update(_held(job_id, owner, fence), {"$set": {"status": JobStatus.COMPLETED, "result_ref": result_ref}},
                                                   {"kind": 1, "task_id": 1}, session=session)
        if job is None:
            raise LeaseLost(f"job {job_id} is not running under owner {owner} at fence {fence}")
        res = await apply(session)
        if job["kind"] == JobKind.INTEGRATE:
            await _integration_done(session, db, job["task_id"])
        return res

    async with db.client.start_session() as session:
        return await session.with_transaction(run)


async def enqueue(session: AsyncClientSession, db: AsyncDatabase[Doc], kind: JobKind, payload: Doc, dedupe_key: str, project_id: str,
                  task_id: str | None = None, available_at: datetime | None = None) -> str | None:
    """Insert a queued job unless one with `dedupe_key` exists; returns the new job id, or None on a dedupe hit."""
    now = datetime.now(UTC)
    job = {"_id": new_id(Prefix.JOB), "schema_version": SCHEMA_VERSION, "project_id": project_id, "task_id": task_id, "kind": kind,
           "payload": payload, "status": JobStatus.QUEUED, "dedupe_key": dedupe_key, "available_at": available_at or now, "attempts": 0,
           "lease": {"owner": None, "expires_at": None, "fence": 0}, "result_ref": None, "created_at": now}
    res = await db["jobs"].update_one({"dedupe_key": dedupe_key}, {"$setOnInsert": job}, upsert=True, session=session)
    return res.upserted_id


async def integration_is_final(session: AsyncClientSession | None, db: AsyncDatabase[Doc], task_id: str, delegation_id: str) -> bool:
    """True iff every other Assignment in the task's Contract has an accepted Delegation. The integrate handler calls it when its job
    runs (session may be None): a task's integrations run one at a time, so the answer holds until that job completes."""
    task = await db["tasks"].find_one({"_id": task_id}, {"contract.assignments.key": 1}, session=session)
    if task is None:
        raise LookupError(f"task {task_id} not found")
    accepted = await db["delegations"].count_documents({"task_id": task_id, "_id": {"$ne": delegation_id}, "status": DelegationState.ACCEPTED},
                                                       session=session)
    return accepted == len(task["contract"]["assignments"]) - 1


async def claim(db: AsyncDatabase[Doc], owner: str, kinds: Iterable[JobKind]) -> Doc | None:
    """Lease the oldest runnable job of `kinds`, reclaiming an expired lease; the returned doc carries the new `lease.fence`."""
    now = datetime.now(UTC)
    return await db["jobs"].find_one_and_update(
        {"kind": {"$in": list(kinds)}, "$or": [{"status": JobStatus.QUEUED, "available_at": {"$lte": now}},
                                                {"status": JobStatus.RUNNING, "lease.expires_at": {"$lt": now}}]},
        {"$set": {"status": JobStatus.RUNNING, "lease.owner": owner, "lease.expires_at": now + LEASE}, "$inc": {"lease.fence": 1, "attempts": 1}},
        sort=[("available_at", 1)], return_document=ReturnDocument.AFTER)


def _held(job_id: str, owner: str, fence: int) -> Doc:
    return {"_id": job_id, "lease.owner": owner, "lease.fence": fence, "status": JobStatus.RUNNING}


async def heartbeat(db: AsyncDatabase[Doc], job_id: str, owner: str, fence: int) -> bool:
    """Extend the lease; False means it was lost and the caller must abandon the job and discard its result."""
    res = await db["jobs"].update_one(_held(job_id, owner, fence), {"$set": {"lease.expires_at": datetime.now(UTC) + LEASE}})
    return res.matched_count == 1


async def fail(db: AsyncDatabase[Doc], job_id: str, owner: str, fence: int, error: str) -> JobStatus:
    """Requeue with full-jitter backoff, or once `attempts` reaches MAX_JOB_ATTEMPTS mark it `failed` and fail its owner in the same
    transaction; raises LeaseLost if not held."""
    async def run(s: AsyncClientSession) -> JobStatus:
        if (job := await db["jobs"].find_one(_held(job_id, owner, fence), session=s)) is None:
            raise LeaseLost(f"job {job_id} is not running under owner {owner} at fence {fence}")
        n, now = job["attempts"], datetime.now(UTC)
        set_: Doc = ({"status": JobStatus.FAILED} if n >= MAX_JOB_ATTEMPTS else
                     {"status": JobStatus.QUEUED, "available_at": now + timedelta(seconds=random.uniform(0, min(60, 2 * 2**n)))})
        await db["jobs"].update_one({"_id": job_id}, {"$set": {**set_, "last_error": error}}, session=s)
        if set_["status"] == JobStatus.FAILED:
            await _fail_owner(s, db, job, error)
        return set_["status"]

    async with db.client.start_session() as session:
        return await session.with_transaction(run)


async def _fail_owner(s: AsyncClientSession, db: AsyncDatabase[Doc], job: Doc, error: str) -> None:
    p, tid = job["payload"], job["task_id"]
    match job["kind"]:
        case JobKind.PLAN:
            moves: list[tuple[str, str, State, Doc]] = [("tasks", tid, TaskState.FAILED, {})]
        case JobKind.LAUNCH:
            moves = [("worker_runs", p["worker_run_id"], WorkerRunState.FAILED, {}), ("delegations", p["delegation_id"], DelegationState.FAILED, {})]
        case JobKind.VALIDATE:
            moves = [("delegations", p["delegation_id"], DelegationState.REPAIR_REQUIRED, {"validation_decision": "inconclusive"})]
        case JobKind.INTEGRATE:
            moves = [("delegations", p["delegation_id"], DelegationState.REPAIR_REQUIRED, {})]
            await _integration_done(s, db, tid)
        case _:
            return
    for coll, id_, to, set_ in moves:
        if (doc := await db[coll].find_one({"_id": id_}, {"status": 1, "revision": 1}, session=s)) is None:
            raise LookupError(f"{coll} {id_} not found")
        if to in ALLOWED[Entity(coll)].get(doc["status"], frozenset()):  # an owner that already moved on (e.g. cancelled) keeps its state
            await transition(s, db[coll], id_, doc["revision"], to, set_)
    await append_event(s, tid, db["tasks"], "job.failed", "controller",
                       {"job_id": job["_id"], "kind": job["kind"], "error": error, "owners": {id_: to for _, id_, to, _ in moves}})
    if job["kind"] == JobKind.LAUNCH:  # every contract assignment is required, so a failed delegation fails its task
        await close_task(s, db, tid, TaskState.FAILED, "controller", {"reason": "delegation_failed", "delegation_id": p["delegation_id"]})


async def close_task(s: AsyncClientSession, db: AsyncDatabase[Doc], task_id: str, to: TaskState, actor_id: str, payload: Doc) -> bool:
    """Move an open task to failed/cancelled and cancel its unfinished delegations and worker runs; False if it was already terminal."""
    if (task := await db["tasks"].find_one({"_id": task_id}, {"status": 1, "revision": 1}, session=s)) is None:
        raise LookupError(f"task {task_id} not found")
    if to not in ALLOWED[Entity.TASK].get(task["status"], frozenset()):
        return False
    for coll, cancelled in (("delegations", DelegationState.CANCELLED), ("worker_runs", WorkerRunState.CANCELLED)):
        for d in await db[coll].find({"task_id": task_id}, {"status": 1, "revision": 1}, session=s).to_list():
            if cancelled in ALLOWED[Entity(coll)].get(d["status"], frozenset()):
                await transition(s, db[coll], d["_id"], d["revision"], cancelled)
    await transition(s, db["tasks"], task_id, task["revision"], to)
    await append_event(s, task_id, db["tasks"], f"task.{to.value}", actor_id, payload)
    return True


async def _integration_done(s: AsyncClientSession, db: AsyncDatabase[Doc], task_id: str) -> None:
    # The task write makes this conflict with a concurrent on_validation_pass, so no job is parked behind a finished integration.
    await db["tasks"].update_one({"_id": task_id}, {"$inc": {"integrations_done": 1}}, session=s)
    await db["jobs"].find_one_and_update({"task_id": task_id, "kind": JobKind.INTEGRATE, "status": JobStatus.QUEUED, "available_at": PARKED},
                                         {"$set": {"available_at": datetime.now(UTC)}}, sort=[("payload.integration_seq", 1)], session=s)


type Handler = Callable[[Doc], Coroutine[Any, Any, object]]


async def _run(db: AsyncDatabase[Doc], owner: str, job: Doc, handler: Handler, every: float) -> None:
    jid, fence = job["_id"], job["lease"]["fence"]
    work = asyncio.create_task(handler(job))

    async def beat() -> None:
        try:
            while True:
                await asyncio.sleep(every)
                if not await heartbeat(db, jid, owner, fence):
                    return
        finally:
            work.cancel()

    beater = asyncio.create_task(beat())
    try:
        await asyncio.wait([work])
    finally:
        work.cancel()
        beater.cancel()
        await asyncio.wait([work, beater])
    if work.cancelled():
        log.warning("job %s abandoned: lease lost at fence %s", jid, fence, exc_info=beater.exception() if beater.done() and not beater.cancelled() else None)
    elif isinstance(e := work.exception(), LeaseLost):
        log.warning("job %s: %s", jid, e)
    elif e is not None:
        log.error("job %s failed at fence %s", jid, fence, exc_info=e)
        await fail(db, jid, owner, fence, repr(e))


async def run_worker(db: AsyncDatabase[Doc], owner: str, dispatch: Mapping[JobKind, Handler], poll: float = 1.0,
                     heartbeat_every: float = HEARTBEAT_SECONDS) -> NoReturn:
    """Claim and run jobs forever; each handler gets the claimed job doc and must report through `complete` under its lease."""
    while True:
        if (job := await claim(db, owner, dispatch)) is None:
            await asyncio.sleep(poll)
            continue
        try:
            await _run(db, owner, job, dispatch[job["kind"]], heartbeat_every)
        except LeaseLost as e:
            log.warning("job %s: %s", job["_id"], e)


async def on_validation_pass(session: AsyncClientSession, db: AsyncDatabase[Doc], delegation: Doc) -> str:
    """Call inside the validate job's `complete` with `delegation` as read in that transaction: moves it validating -> integrating and
    enqueues its integrate job keyed by the task's next `integration_seq`, PARKED while an earlier integration is unfinished."""
    did, tid = delegation["_id"], delegation["task_id"]
    await transition(session, db["delegations"], did, delegation["revision"], DelegationState.INTEGRATING)
    task = await db["tasks"].find_one_and_update({"_id": tid}, {"$inc": {"integration_seq": 1}}, {"integration_seq": 1, "integrations_done": 1},
                                                 return_document=ReturnDocument.AFTER, session=session)
    if task is None:
        raise LookupError(f"task {tid} not found")
    seq = task["integration_seq"]
    payload = {"task_id": tid, "delegation_id": did, "attempt": delegation["attempt"], "integration_seq": seq}
    parked = PARKED if seq - 1 > task.get("integrations_done", 0) else None
    if (job_id := await enqueue(session, db, JobKind.INTEGRATE, payload, f"{tid}:integrate:{seq}", delegation["project_id"], tid, parked)) is None:
        raise RuntimeError(f"integrate job {tid}:integrate:{seq} already exists")
    await append_event(session, tid, db["tasks"], "delegation.integrating", "controller", payload | {"job_id": job_id})
    return job_id


async def on_integration_pass(session: AsyncClientSession, db: AsyncDatabase[Doc], delegation: Doc, integration_seq: int, result_artifact_id: str,
                              validation_run_id: str) -> TaskState | None:
    """Call inside the integrate job's `complete` once the composed tree validated, with `delegation` as read in that transaction: records
    the accepted integration run, accepts the delegation, and completes the task if this integration is final (then returns COMPLETED)."""
    did, tid = delegation["_id"], delegation["task_id"]
    await transition(session, db["delegations"], did, delegation["revision"], DelegationState.ACCEPTED, {"accepted_artifact_id": result_artifact_id})
    await db["integration_runs"].insert_one({
        "_id": new_id(Prefix.INTEGRATION_RUN), "schema_version": SCHEMA_VERSION, "project_id": delegation["project_id"], "task_id": tid,
        "delegation_id": did, "status": IntegrationRunState.ACCEPTED, "revision": 1, "integration_fence": integration_seq,
        "result_artifact_id": result_artifact_id, "validation_run_id": validation_run_id, "created_at": datetime.now(UTC)}, session=session)
    await append_event(session, tid, db["tasks"], "delegation.accepted", "controller",
                       {"delegation_id": did, "integration_seq": integration_seq, "artifact_id": result_artifact_id})
    if not await integration_is_final(session, db, tid, did):
        return None
    if (task := await db["tasks"].find_one({"_id": tid}, {"revision": 1}, session=session)) is None:
        raise LookupError(f"task {tid} not found")
    final = {"final_artifact_id": result_artifact_id, "final_validation_run_id": validation_run_id}
    await transition(session, db["tasks"], tid, task["revision"], TaskState.COMPLETED, final)
    await append_event(session, tid, db["tasks"], "task.completed", "controller", final)
    return TaskState.COMPLETED
