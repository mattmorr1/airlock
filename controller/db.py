import os
from typing import Any

import certifi
from pymongo import AsyncMongoClient
from pymongo.asynchronous.database import AsyncDatabase

type Doc = dict[str, Any]


def client() -> AsyncMongoClient[Doc]:
    uri = os.environ["AIRLOCK_MONGO_URI"]
    # uv/python.org builds on macOS lack the system CA store; any tls option implies TLS, so only for srv URIs.
    tls = {"tlsCAFile": certifi.where()} if uri.startswith("mongodb+srv://") else {}
    return AsyncMongoClient(uri, tz_aware=True, **tls)


def database(c: AsyncMongoClient[Doc]) -> AsyncDatabase[Doc]:
    return c[os.environ.get("AIRLOCK_DB", "airlock")]
