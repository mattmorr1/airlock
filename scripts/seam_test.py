"""Prove B's handlers work against A's REAL seam (jobs lib, state machine, complete).

Hand-seeds the docs A's delegate route would create (bypassing the SDK planner
+ HTTP API), then drives B's launch/validate/integrate/evolve handlers via A's
claim()/complete(), asserting the state machine advances exactly as A defines.

Run:
  AIRLOCK_MONGO_URI="mongodb+srv://..." AIRLOCK_DB=airlock_seamtest \
  TARGET_MONGO_URI="mongodb://127.0.0.1:27018/?replicaSet=rs0" \
  AIRLOCK_EXECUTION_MODE=local python scripts/seam_test.py
"""
from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path
from tempfile import mkdtemp

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from controller import artifacts, db as dbmod, harness, snapshots
from controller.dev_fixtures import FIXTURES
from controller.handlers import handle_evolve, handle_integrate, handle_launch, handle_validate
from controller.scheduler import claim, enqueue
from controller.states import (
    DelegationState, JobKind, Prefix, SCHEMA_VERSION, TaskState, WorkerRunState, new_id,
)
from evolution.propose import open_run
from mongo.setup import setup

OWNER = "seamtest-worker"


def app_artifact(fixture: str, pid: str) -> str:
    """Freeze target-app + a bulk variant into an artifact (sync)."""
    ck = Path(mkdtemp(prefix=f"app-{fixture}-"))
    for rel, content in snapshots.read_dir_tree(Path("target-app")).items():
        p = ck / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
    FIXTURES[fixture](ck, {})
    return artifacts.put_tree(snapshots.read_dir_tree(ck), kind="code_snapshot", project_id=pid,
                              base_artifact_id=None, created_by="supervisor_01")["_id"]


async def drive(db, kind, handler) -> dict:
    job = await claim(db, OWNER, [kind])
    assert job is not None, f"no {kind} job to claim"
    await handler(db, job)
    return job


async def main() -> None:
    async with dbmod.client() as c:
        db = dbmod.database(c)
        await setup(db)
        pid, tid = new_id(Prefix.PROJECT), new_id(Prefix.TASK)
        did, wid = new_id(Prefix.DELEGATION), new_id(Prefix.WORKER_RUN)
        now = datetime.now(UTC)

        base = artifacts.put_tree(snapshots.read_dir_tree(Path("target-app")), kind="baseline",
                                  project_id=pid, created_by="operator_01")
        hv1 = harness.bootstrap_bulk_v1(pid)

        await db["projects"].insert_one({
            "_id": pid, "schema_version": SCHEMA_VERSION, "name": "seamtest", "created_at": now,
            "repository": {"registered_root_id": "repo_seam", "default_branch": "main"},
            "baseline_artifact_id": base["_id"], "active_harness_version_id": hv1["_id"],
            "harness_generation": 1, "constraint_epoch": 1, "validated_harness_ids": [hv1["_id"]],
            "event_seq": 0, "revision": 1})
        assignment = {"key": "bulk", "objective": "Add PATCH /items/bulk", "depends_on": [],
                      "file_scope": ["app/**"], "profile_key": "bounded-items-worker"}
        await db["tasks"].insert_one({
            "_id": tid, "schema_version": SCHEMA_VERSION, "project_id": pid, "status": TaskState.RUNNING,
            "request": "bulk update", "revision": 1, "contract_version": 1, "event_seq": 0, "integration_seq": 0,
            "contract": {"assignments": [assignment]}, "contract_digest": "sha256:seam",
            "approval": {"actor_id": "operator", "constraint_epoch": 1, "approved_at": now},
            "base_artifact_id": base["_id"], "delegation_ids": [did], "final_artifact_id": None, "created_at": now})
        await db["delegations"].insert_one({
            "_id": did, "schema_version": SCHEMA_VERSION, "project_id": pid, "task_id": tid, "assignment_key": "bulk",
            "objective": assignment["objective"], "depends_on": [], "file_scope": ["app/**"],
            "profile_key": "bounded-items-worker", "status": DelegationState.RUNNING, "attempt": 1, "revision": 1,
            "resolved_input_artifact_id": base["_id"], "contract_version": 1, "active_worker_run_id": wid,
            "accepted_artifact_id": None, "created_at": now})
        await db["worker_runs"].insert_one({
            "_id": wid, "schema_version": SCHEMA_VERSION, "project_id": pid, "task_id": tid, "delegation_id": did,
            "attempt": 1, "status": WorkerRunState.QUEUED, "revision": 1, "input_artifact_id": base["_id"],
            "harness_version_id": hv1["_id"], "constraint_epoch": 1, "dev_fixture": "bulk_faulty",
            "output_artifact_id": None, "started_at": None, "finished_at": None, "created_at": now})
        async with c.start_session() as s:
            await enqueue(s, db, JobKind.LAUNCH, {"worker_run_id": wid, "delegation_id": did}, f"{wid}:launch", pid, tid)

        print("seeded; driving B handlers through A's seam...\n")

        await drive(db, JobKind.LAUNCH, handle_launch)
        wr = await db["worker_runs"].find_one({"_id": wid})
        dele = await db["delegations"].find_one({"_id": did})
        print(f"after LAUNCH : worker_run={wr['status']} output={wr['output_artifact_id']} delegation={dele['status']}")
        assert wr["status"] == WorkerRunState.FINISHED and wr["output_artifact_id"]
        assert dele["status"] == DelegationState.VALIDATING

        await drive(db, JobKind.VALIDATE, handle_validate)
        dele = await db["delegations"].find_one({"_id": did})
        val = await db["validation_runs"].find_one({"task_id": tid})
        print(f"after VALIDATE: validation decision={val['decision']} (v1 lets the partial write escape) delegation={dele['status']}")
        assert dele["status"] == DelegationState.INTEGRATING

        await drive(db, JobKind.INTEGRATE, handle_integrate)
        dele = await db["delegations"].find_one({"_id": did})
        task = await db["tasks"].find_one({"_id": tid})
        ir = await db["integration_runs"].find_one({"task_id": tid})
        print(f"after INTEGRATE: integration={ir['status']} delegation={dele['status']} task={task['status']}")
        assert dele["status"] == DelegationState.ACCEPTED and task["status"] == TaskState.COMPLETED

        # Evolution: the escaped partial write drives a harness candidate.
        clean = app_artifact("bulk_fixed", pid)
        buggy = wr["output_artifact_id"]
        evo = open_run(project_id=pid, baseline_harness_version_id=hv1["_id"],
                       applications=[{"artifact_id": buggy, "label": "buggy"}, {"artifact_id": clean, "label": "clean"}])
        await db["evolution_runs"].update_one({"_id": evo}, {"$set": {"constraint_epoch": 1}})
        async with c.start_session() as s:
            await enqueue(s, db, JobKind.EVOLVE, {"evolution_run_id": evo}, f"{evo}:evolve", pid, tid)
        await drive(db, JobKind.EVOLVE, handle_evolve)
        e = await db["evolution_runs"].find_one({"_id": evo})
        proj = await db["projects"].find_one({"_id": pid})
        m = e["evaluation"]["metrics"]
        print(f"after EVOLVE  : evolution={e['status']} baseline_escaped={m['baseline']['escaped_failures']} "
              f"candidate_escaped={m['candidate']['escaped_failures']} active_harness_flipped={proj['active_harness_version_id']==e['candidate_harness_version_id']}")
        assert e["status"] in ("eligible", "promoted")
        assert proj["active_harness_version_id"] == e["candidate_harness_version_id"]

        print("\nSEAM OK: B's handlers drove A's state machine launch -> validate -> integrate -> evolve -> promote.")
        await c.drop_database(db.name)
        print(f"dropped throwaway db {db.name}")


if __name__ == "__main__":
    asyncio.run(main())
