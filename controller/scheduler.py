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
from controller.states import SCHEMA_VERSION, DelegationState, JobKind, JobStatus, Prefix, append_event, new_id, transition

log = logging.getLogger(__name__)
LEASE, HEARTBEAT_SECONDS, MAX_JOB_ATTEMPTS = timedelta(seconds=30), 10.0, 3


class LeaseLost(Exception):
    pass


async def complete[R](db: AsyncDatabase[Doc], job_id: str, owner: str, fence: int, apply: Callable[[AsyncClientSession], Awaitable[R]],
                      result_ref: str | None = None) -> R:
    """Complete a leased job and run `apply` in one transaction; `apply` must be DB-only, since it is retried on transient errors."""
    async def run(session: AsyncClientSession) -> R:
        res = await db["jobs"].update_one({"_id": job_id, "lease.owner": owner, "lease.fence": fence, "status": JobStatus.RUNNING},
                                          {"$set": {"status": JobStatus.COMPLETED, "result_ref": result_ref}}, session=session)
        if res.matched_count == 0:
            raise LeaseLost(f"job {job_id} is not running under owner {owner} at fence {fence}")
        return await apply(session)

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


async def integration_is_final(session: AsyncClientSession, db: AsyncDatabase[Doc], task_id: str, delegation_id: str) -> bool:
    """True iff every other Assignment in the task's Contract has an accepted Delegation."""
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
    """Requeue with full-jitter backoff, or mark `failed` once `attempts` reaches MAX_JOB_ATTEMPTS; raises LeaseLost if not held."""
    if (job := await db["jobs"].find_one(_held(job_id, owner, fence), {"attempts": 1})) is None:
        raise LeaseLost(f"job {job_id} is not running under owner {owner} at fence {fence}")
    # attempts only changes on claim, which also bumps the fence, so the guarded update below cannot act on a stale read.
    n, now = job["attempts"], datetime.now(UTC)
    set_: Doc = ({"status": JobStatus.FAILED} if n >= MAX_JOB_ATTEMPTS else
                 {"status": JobStatus.QUEUED, "available_at": now + timedelta(seconds=random.uniform(0, min(60, 2 * 2**n)))})
    if (await db["jobs"].update_one(_held(job_id, owner, fence), {"$set": {**set_, "last_error": error}})).matched_count == 0:
        raise LeaseLost(f"job {job_id} lease lost while failing")
    return set_["status"]


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
    enqueues its integrate job keyed by the task's next `integration_seq`; payload `final` is true iff every other Assignment is accepted."""
    did, tid = delegation["_id"], delegation["task_id"]
    await transition(session, db["delegations"], did, delegation["revision"], DelegationState.INTEGRATING)
    task = await db["tasks"].find_one_and_update({"_id": tid}, {"$inc": {"integration_seq": 1}}, {"integration_seq": 1},
                                                 return_document=ReturnDocument.AFTER, session=session)
    if task is None:
        raise LookupError(f"task {tid} not found")
    seq, final = task["integration_seq"], await integration_is_final(session, db, tid, did)
    payload = {"task_id": tid, "delegation_id": did, "attempt": delegation["attempt"], "integration_seq": seq, "final": final}
    if (job_id := await enqueue(session, db, JobKind.INTEGRATE, payload, f"{tid}:integrate:{seq}", delegation["project_id"], tid)) is None:
        raise RuntimeError(f"integrate job {tid}:integrate:{seq} already exists")
    await append_event(session, tid, db["tasks"], "delegation.integrating", "controller", payload | {"job_id": job_id})
    return job_id
