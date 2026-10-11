"""PyMongo access to the control-plane database and GridFS.

Synchronous PyMongo. Person B runs in plain worker processes (not an async
event loop), so blocking calls here are fine. Keep this control-plane database
separate from the target application's test database, and never hand this
client to coding workers or the target app (spec section 2, 4).
"""
from __future__ import annotations

from functools import lru_cache

import gridfs
from pymongo import MongoClient
from pymongo.database import Database

from .config import get_settings

COLLECTIONS = (
    "projects",
    "tasks",
    "delegations",
    "worker_runs",
    "artifacts",
    "validation_runs",
    "harness_versions",
    "evolution_runs",
    "integration_runs",
    "jobs",
    "events",
)


@lru_cache(maxsize=1)
def get_client() -> MongoClient:
    s = get_settings()
    return MongoClient(s.mongo_uri, tz_aware=True)


def get_db() -> Database:
    return get_client()[get_settings().db_name]


def get_gridfs() -> gridfs.GridFSBucket:
    return gridfs.GridFSBucket(get_db(), bucket_name=get_settings().gridfs_bucket)


def col(name: str):
    if name not in COLLECTIONS:
        raise KeyError(f"unknown collection {name!r}")
    return get_db()[name]
