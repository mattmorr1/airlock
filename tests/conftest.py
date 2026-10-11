import os
import uuid
from collections.abc import AsyncIterator

import pytest
from pymongo.asynchronous.database import AsyncDatabase

from controller.db import Doc, client
from mongo.setup import setup


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(scope="session")
async def db() -> AsyncIterator[AsyncDatabase[Doc]]:
    if not os.environ.get("AIRLOCK_MONGO_URI"):
        pytest.skip("AIRLOCK_MONGO_URI is unset; these tests need the Atlas cluster")
    async with client() as c:
        database = c[f"airlock_test_{uuid.uuid4().hex}"]
        try:
            await setup(database)
            yield database
        finally:
            await c.drop_database(database.name)
