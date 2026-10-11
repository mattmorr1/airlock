import os
import uuid
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager

import pytest
from pymongo.asynchronous.database import AsyncDatabase

from controller.db import Doc, client
from mongo.setup import setup


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


@asynccontextmanager
async def throwaway() -> AsyncGenerator[AsyncDatabase[Doc]]:
    if not os.environ.get("AIRLOCK_MONGO_URI"):
        pytest.skip("AIRLOCK_MONGO_URI is unset; these tests need the Atlas cluster")
    async with client() as c:
        database = c[f"airlock_test_{uuid.uuid4().hex}"]
        try:
            await setup(database)
            yield database
        finally:
            await c.drop_database(database.name)


@pytest.fixture(scope="session")
async def db() -> AsyncIterator[AsyncDatabase[Doc]]:
    async with throwaway() as database:
        yield database
