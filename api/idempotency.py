import hashlib
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from pymongo.asynchronous.client_session import AsyncClientSession
from pymongo.asynchronous.database import AsyncDatabase
from pymongo.errors import DuplicateKeyError, OperationFailure

from api.errors import ApiError
from controller.db import Doc
from controller.states import SCHEMA_VERSION, JobKind, JobStatus, Prefix, new_id

WRITE_CONFLICT = 112


class _Committed(Exception):
    pass


def digest(*parts: object) -> str:
    return "sha256:" + hashlib.sha256(json.dumps(parts, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


async def idempotent(db: AsyncDatabase[Doc], project: str, actor: str, op: str, key: str, request_digest: str,
                     fn: Callable[[AsyncClientSession], Awaitable[Doc]]) -> Doc:
    """Run `fn` and store its JSON-safe result as a command receipt in one transaction; a replay returns the stored result."""
    dedupe_key, now = f"receipt:{project}:{actor}:{op}:{key}", datetime.now(UTC)

    async def run(session: AsyncClientSession) -> Doc:
        receipt_id = new_id(Prefix.JOB)
        # The receipt goes first so a concurrent duplicate collides on it before either side mutates anything.
        try:
            await db["jobs"].insert_one({"_id": receipt_id, "schema_version": SCHEMA_VERSION, "project_id": project, "kind": JobKind.COMMAND_RECEIPT,
                                         "status": JobStatus.COMPLETED, "dedupe_key": dedupe_key, "actor_id": actor, "op": op,
                                         "request_digest": request_digest, "result": None, "available_at": now, "created_at": now}, session=session)
        except DuplicateKeyError as e:
            raise _Committed from e
        except OperationFailure as e:
            if e.code == WRITE_CONFLICT:
                raise ApiError(409, "IN_PROGRESS", f"{op} with this Idempotency-Key is still in progress", retryable=True) from e
            raise
        result = await fn(session)
        await db["jobs"].update_one({"_id": receipt_id}, {"$set": {"result": result}}, session=session)
        return result

    try:
        async with db.client.start_session() as session:
            return await session.with_transaction(run)
    except _Committed:
        prior = await db["jobs"].find_one({"dedupe_key": dedupe_key}, {"request_digest": 1, "result": 1})
    if prior is None:
        raise ApiError(409, "IN_PROGRESS", f"{op} with this Idempotency-Key is still in progress", retryable=True)
    if prior["request_digest"] != request_digest:
        raise ApiError(409, "IDEMPOTENCY_MISMATCH", f"Idempotency-Key was already used for a different {op} request")
    return prior["result"]
