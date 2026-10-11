from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from pymongo.asynchronous.client_session import AsyncClientSession
from pymongo.asynchronous.database import AsyncDatabase

from controller.db import Doc
from controller.states import SCHEMA_VERSION, DelegationState, JobKind, JobStatus, Prefix, new_id


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
