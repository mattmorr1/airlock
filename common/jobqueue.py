"""Durable job queue: claim, heartbeat, complete (spec section 7.10, 11).

Person A's scheduler enqueues jobs; Person B's worker loop claims and runs them.
Both sides share this protocol:

- Claim atomically with ``find_one_and_update`` and increment a fencing token.
- Heartbeat the lease; reclaim expired leases.
- Condition completion on owner *and* fencing token.
- Delivery is at-least-once: handlers must be idempotent. Model inference can
  still be duplicated after an ambiguous crash — we record that possibility
  rather than claiming exactly-once execution.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any, Iterable

from pymongo import ReturnDocument

from .config import get_settings
from .db import col
from .enums import JobKind, JobStatus
from .ids import new_id, utcnow


def enqueue(
    *,
    kind: JobKind | str,
    payload: dict[str, Any],
    dedupe_key: str,
    project_id: str,
    task_id: str | None = None,
) -> str:
    """Idempotent enqueue. Returns the job id for new or existing duplicate.

    The unique index on ``dedupe_key`` makes retries land on the same job
    instead of creating unrelated duplicate work (spec section 11).
    """
    now = utcnow()
    job_id = new_id("job")
    doc = {
        "_id": job_id,
        "schema_version": 1,
        "project_id": project_id,
        "task_id": task_id,
        "kind": str(kind),
        "payload": payload,
        "status": JobStatus.QUEUED.value,
        "dedupe_key": dedupe_key,
        "available_at": now,
        "attempts": 0,
        "lease": {"owner": None, "expires_at": None, "fence": 0},
        "result_ref": None,
        "created_at": now,
    }
    existing = col("jobs").find_one_and_update(
        {"dedupe_key": dedupe_key},
        {"$setOnInsert": doc},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    return existing["_id"]


def claim(owner: str, kinds: Iterable[JobKind | str] | None = None) -> dict[str, Any] | None:
    """Atomically claim one ready job, taking a lease and bumping the fence."""
    now = utcnow()
    ttl = get_settings().lease_ttl_seconds
    query: dict[str, Any] = {
        "status": JobStatus.QUEUED.value,
        "available_at": {"$lte": now},
    }
    if kinds is not None:
        query["kind"] = {"$in": [str(k) for k in kinds]}
    return col("jobs").find_one_and_update(
        query,
        {
            "$set": {
                "status": JobStatus.RUNNING.value,
                "lease.owner": owner,
                "lease.expires_at": now + timedelta(seconds=ttl),
            },
            "$inc": {"attempts": 1, "lease.fence": 1},
        },
        sort=[("available_at", 1)],
        return_document=ReturnDocument.AFTER,
    )


def heartbeat(job_id: str, owner: str, fence: int) -> bool:
    """Extend the lease. Fails (False) if we no longer own this fencing token."""
    now = utcnow()
    ttl = get_settings().lease_ttl_seconds
    res = col("jobs").update_one(
        {"_id": job_id, "lease.owner": owner, "lease.fence": fence},
        {"$set": {"lease.expires_at": now + timedelta(seconds=ttl)}},
    )
    return res.modified_count == 1


def complete(job_id: str, owner: str, fence: int, result_ref: dict[str, Any] | None = None) -> bool:
    res = col("jobs").update_one(
        {"_id": job_id, "lease.owner": owner, "lease.fence": fence},
        {"$set": {"status": JobStatus.DONE.value, "result_ref": result_ref, "lease.expires_at": None}},
    )
    return res.modified_count == 1


def fail(job_id: str, owner: str, fence: int, error: str, retryable: bool = False) -> bool:
    now = utcnow()
    update: dict[str, Any] = {
        "$set": {"result_ref": {"error": error, "retryable": retryable}, "lease.expires_at": None}
    }
    if retryable:
        update["$set"]["status"] = JobStatus.QUEUED.value
        update["$set"]["available_at"] = now + timedelta(seconds=5)
    else:
        update["$set"]["status"] = JobStatus.FAILED.value
    res = col("jobs").update_one(
        {"_id": job_id, "lease.owner": owner, "lease.fence": fence}, update
    )
    return res.modified_count == 1


def reclaim_expired() -> int:
    """Return expired running jobs to the queue so another owner can claim them."""
    now = utcnow()
    res = col("jobs").update_many(
        {"status": JobStatus.RUNNING.value, "lease.expires_at": {"$lt": now}},
        {"$set": {"status": JobStatus.QUEUED.value, "lease.owner": None, "lease.expires_at": None}},
    )
    return res.modified_count
