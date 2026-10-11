"""Person B's job-worker process: A's run_worker driving B's handlers.

Registers B's execution handlers for the launch/validate/integrate/evolve job
kinds and runs A's scheduler loop (which owns claiming, leasing, heartbeats,
and completion). The plan job is Person A's and is served by a process that has
the Claude Agent SDK and a checkout; it is intentionally not registered here.

Run:  AIRLOCK_MONGO_URI=... AIRLOCK_DB=airlock \
      TARGET_MONGO_URI=mongodb://127.0.0.1:27018/?replicaSet=rs0 \
      python -m controller.worker_main
"""
from __future__ import annotations

import asyncio

from controller import db as dbmod
from controller.handlers import handle_evolve, handle_integrate, handle_launch, handle_validate
from controller.scheduler import run_worker
from controller.states import JobKind, Prefix, new_id


async def main() -> None:
    owner = new_id(Prefix.WORKER_RUN)  # a unique lease owner for this process
    async with dbmod.client() as c:
        db = dbmod.database(c)
        dispatch = {
            JobKind.LAUNCH: lambda job: handle_launch(db, job),
            JobKind.VALIDATE: lambda job: handle_validate(db, job),
            JobKind.INTEGRATE: lambda job: handle_integrate(db, job),
            JobKind.EVOLVE: lambda job: handle_evolve(db, job),
        }
        print(f"B job-worker {owner} up; handling {[k.value for k in dispatch]}")
        await run_worker(db, owner, dispatch)


if __name__ == "__main__":
    asyncio.run(main())
