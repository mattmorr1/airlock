import asyncio
import logging
from typing import Self
from weakref import WeakValueDictionary

from pymongo.asynchronous.change_stream import AsyncChangeStream
from pymongo.asynchronous.database import AsyncDatabase

from controller.db import Doc

log = logging.getLogger(__name__)
PIPELINE: list[Doc] = [{"$match": {"operationType": "insert"}}, {"$project": {"fullDocument.stream_id": 1}}]
REOPEN_DELAY = 1.0


class Doorbell:
    """One change stream per process on `events` inserts, used only to wake waiters per stream_id; data is always re-read by sequence."""

    def __init__(self, db: AsyncDatabase[Doc]) -> None:
        self.db = db
        self.stream: AsyncChangeStream[Doc] | None = None
        self.task: asyncio.Task[None] | None = None
        self._bells: WeakValueDictionary[str, asyncio.Event] = WeakValueDictionary()

    async def __aenter__(self) -> Self:
        self.task = asyncio.create_task(self._run(), name="doorbell")
        return self

    async def __aexit__(self, *_: object) -> None:
        if self.task:
            self.task.cancel()
            await asyncio.wait({self.task})

    def arm(self, stream_id: str) -> asyncio.Event:
        """The Event the next insert on `stream_id` sets. Arm before reading, so an insert between the read and the wait still rings."""
        if (ev := self._bells.get(stream_id)) is None:
            ev = self._bells[stream_id] = asyncio.Event()
        return ev

    @staticmethod
    async def wait(ev: asyncio.Event, within: float) -> bool:
        """True if `ev` rang within `within` seconds."""
        try:
            async with asyncio.timeout(within):
                await ev.wait()
        except TimeoutError:
            return False
        return True

    def _ring_all(self) -> None:
        bells, self._bells = list(self._bells.values()), WeakValueDictionary()
        for ev in bells:
            ev.set()

    async def _run(self) -> None:
        while True:
            try:
                async with await self.db["events"].watch(PIPELINE) as self.stream:
                    self._ring_all()  # inserts before the stream opened were missed; every waiter re-reads
                    async for change in self.stream:
                        if (ev := self._bells.pop(change["fullDocument"]["stream_id"], None)) is not None:
                            ev.set()
                log.warning("events change stream closed; reopening")
            except Exception:
                log.exception("events change stream failed; reopening")
            self._ring_all()
            # ponytail: fixed reopen delay, exponential backoff if Atlas outages make this log noisy
            await asyncio.sleep(REOPEN_DELAY)


async def read_after(db: AsyncDatabase[Doc], stream_id: str, cursor: int, limit: int) -> list[Doc]:
    return await db["events"].find({"stream_id": stream_id, "sequence": {"$gt": cursor}}).sort("sequence", 1).limit(limit).to_list()
