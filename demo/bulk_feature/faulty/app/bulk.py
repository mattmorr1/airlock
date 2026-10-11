"""Bulk item update — SEEDED FAULTY variant (labeled, for the demo).

This is the flawed implementation Airlock is meant to catch. It updates items
one at a time and checks ownership *inside the loop*, with NO transaction. When
it reaches an item the caller does not own, it returns 403 — but the earlier
items have already been written. The HTTP response says "rejected," yet the
database has changed. A status-only test passes; the data is corrupted.

Bundles the three assignments (A idempotency, B service, C route) in one file
for the harness-evolution demo; the integration demo uses the 3-file split.
"""
from __future__ import annotations

import hashlib
import json

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from .auth import CurrentUser
from .db import get_db

router = APIRouter(prefix="/api/v1/items")


class BulkItemUpdate(BaseModel):
    id: str
    title: str | None = None
    description: str | None = None


class BulkUpdateRequest(BaseModel):
    updates: list[BulkItemUpdate]


async def setup(db) -> None:
    # A: idempotency storage exists, but the faulty service never uses it.
    await db.idempotency.create_index("key", unique=True)


def _digest(body: BulkUpdateRequest) -> str:
    return hashlib.sha256(json.dumps(body.model_dump(), sort_keys=True).encode()).hexdigest()


@router.patch("/bulk")
async def bulk_update(body: BulkUpdateRequest, idempotency_key: str = Header(default=""),
                      user: str = CurrentUser):
    if len(body.updates) > 100:
        raise HTTPException(status_code=400, detail="too many updates")
    db = get_db()
    applied: list[str] = []
    for u in body.updates:
        item = await db.items.find_one({"_id": u.id})
        if item is None or item["owner_id"] != user:
            # BUG: raise only now — earlier items were already written, no rollback.
            raise HTTPException(status_code=403, detail=f"not your item: {u.id}")
        changes = {k: v for k, v in {"title": u.title, "description": u.description}.items() if v is not None}
        if changes:
            await db.items.update_one({"_id": u.id}, {"$set": changes})
        applied.append(u.id)
    return {"updated": applied}
