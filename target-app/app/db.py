"""Target app database access (its OWN MongoDB, never Airlock's).

This app uses a separate Mongo connection and database from Airlock's control
plane, with its own credentials. The runner points it at a fresh, disposable
database per validation run. Transactions require a replica set (Atlas has one;
locally we use a single-node replica set).
"""
from __future__ import annotations

import hashlib
import os

from pymongo import AsyncMongoClient

_client: AsyncMongoClient | None = None


def mongo_uri() -> str:
    return os.environ.get("TARGET_MONGO_URI", "mongodb://127.0.0.1:27018/?replicaSet=rs0")


def db_name() -> str:
    return os.environ.get("TARGET_DB", "target_app")


def get_client() -> AsyncMongoClient:
    global _client
    if _client is None:
        _client = AsyncMongoClient(mongo_uri())
    return _client


def get_db():
    return get_client()[db_name()]


def hash_password(password: str) -> str:
    return hashlib.sha256(("target-salt::" + password).encode()).hexdigest()


# Deterministic seed users. The password is a fixture, never a real secret.
SEED_USERS = [
    {"_id": "user_alice", "email": "alice@example.com", "password": "alice-pw"},
    {"_id": "user_bob", "email": "bob@example.com", "password": "bob-pw"},
]


async def seed() -> None:
    """Idempotently seed two users and a few items each.

    Item ids carry an optional TARGET_SEED_PREFIX so the runner can give each
    validation run fresh ids (spec section 10: evaluate on fresh item IDs).
    """
    db = get_db()
    for u in SEED_USERS:
        await db.users.update_one(
            {"_id": u["_id"]},
            {"$setOnInsert": {"_id": u["_id"], "email": u["email"],
                              "password_hash": hash_password(u["password"])}},
            upsert=True,
        )
    prefix = os.environ.get("TARGET_SEED_PREFIX", "")
    seed_items = [
        {"_id": f"{prefix}item_alice_1", "owner_id": "user_alice", "title": "Alice A", "description": "a"},
        {"_id": f"{prefix}item_alice_2", "owner_id": "user_alice", "title": "Alice B", "description": "b"},
        {"_id": f"{prefix}item_bob_1", "owner_id": "user_bob", "title": "Bob A", "description": "c"},
    ]
    for it in seed_items:
        await db.items.update_one({"_id": it["_id"]}, {"$setOnInsert": it}, upsert=True)
