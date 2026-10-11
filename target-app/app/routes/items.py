"""Single-item CRUD. The bulk endpoint is intentionally absent in the baseline —
workers A/B/C add `PATCH /api/v1/items/bulk`."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ..auth import CurrentUser
from ..db import get_db
from ..models import ItemCreate, ItemUpdate

router = APIRouter(prefix="/api/v1/items")


@router.get("")
async def list_items(user: str = CurrentUser):
    items = [doc async for doc in get_db().items.find({"owner_id": user})]
    return {"items": items}


@router.post("")
async def create_item(body: ItemCreate, user: str = CurrentUser):
    doc = {"owner_id": user, "title": body.title, "description": body.description}
    res = await get_db().items.insert_one(doc)
    return {"id": str(res.inserted_id)}


@router.get("/{item_id}")
async def get_item(item_id: str, user: str = CurrentUser):
    doc = await get_db().items.find_one({"_id": item_id})
    if doc is None or doc["owner_id"] != user:
        raise HTTPException(status_code=404, detail="not found")
    return doc


@router.patch("/{item_id}")
async def update_item(item_id: str, body: ItemUpdate, user: str = CurrentUser):
    changes = {k: v for k, v in body.model_dump().items() if v is not None}
    doc = await get_db().items.find_one({"_id": item_id})
    if doc is None or doc["owner_id"] != user:
        raise HTTPException(status_code=404, detail="not found")
    if changes:
        await get_db().items.update_one({"_id": item_id}, {"$set": changes})
    return {"ok": True}
