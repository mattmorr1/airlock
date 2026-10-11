"""Bulk item update — FIXED variant (the repair).

All-or-nothing via a MongoDB multi-document transaction: validate every item's
ownership first, and only then apply updates, so a rejected batch leaves every
affected row unchanged. Idempotency-Key is honored: identical retries return the
original result, a reused key with a different payload is 409, and the unique
index guarantees concurrent identical requests apply once.
"""
from __future__ import annotations

import hashlib
import json

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel
from pymongo.errors import DuplicateKeyError

from .auth import CurrentUser
from .db import get_client, get_db

router = APIRouter(prefix="/api/v1/items")


class BulkItemUpdate(BaseModel):
    id: str
    title: str | None = None
    description: str | None = None


class BulkUpdateRequest(BaseModel):
    updates: list[BulkItemUpdate]


async def setup(db) -> None:
    await db.idempotency.create_index("key", unique=True)


def _digest(body: BulkUpdateRequest) -> str:
    return hashlib.sha256(json.dumps(body.model_dump(), sort_keys=True).encode()).hexdigest()


@router.patch("/bulk")
async def bulk_update(body: BulkUpdateRequest, idempotency_key: str = Header(default=""),
                      user: str = CurrentUser):
    if len(body.updates) > 100:
        raise HTTPException(status_code=400, detail="too many updates")
    db = get_db()
    digest = _digest(body)

    # Idempotency replay / conflict check.
    if idempotency_key:
        prior = await db.idempotency.find_one({"key": idempotency_key})
        if prior is not None:
            if prior.get("request_digest") != digest:
                raise HTTPException(status_code=409, detail="idempotency key reused with different payload")
            if prior.get("result") is not None:
                return prior["result"]

    ids = [u.id for u in body.updates]
    client = get_client()
    async with client.start_session() as session:
        try:
            async with await session.start_transaction():
                # Validate ALL ownership first; abort (rollback) if any fail.
                owned = {d["_id"] async for d in db.items.find(
                    {"_id": {"$in": ids}, "owner_id": user}, session=session)}
                missing = [i for i in ids if i not in owned]
                if missing:
                    raise HTTPException(status_code=403, detail=f"not your items: {missing}")
                for u in body.updates:
                    changes = {k: v for k, v in {"title": u.title, "description": u.description}.items()
                               if v is not None}
                    if changes:
                        await db.items.update_one({"_id": u.id}, {"$set": changes}, session=session)
                result = {"updated": ids}
                if idempotency_key:
                    await db.idempotency.insert_one(
                        {"key": idempotency_key, "request_digest": digest, "result": result},
                        session=session)
        except DuplicateKeyError:
            # Concurrent identical request won the key; return its stored result.
            prior = await db.idempotency.find_one({"key": idempotency_key})
            if prior and prior.get("result") is not None:
                return prior["result"]
            raise HTTPException(status_code=409, detail="concurrent request in progress")
    return result
