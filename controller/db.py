import os
from typing import Any

from pymongo import AsyncMongoClient
from pymongo.asynchronous.database import AsyncDatabase

type Doc = dict[str, Any]


def client() -> AsyncMongoClient[Doc]:
    return AsyncMongoClient(os.environ["AIRLOCK_MONGO_URI"], tz_aware=True)


def database(c: AsyncMongoClient[Doc]) -> AsyncDatabase[Doc]:
    return c[os.environ.get("AIRLOCK_DB", "airlock")]
