"""Drive the entire Airlock flow against a LIVE B worker daemon + UI.

Stands in for Person A's API/planner/delegate: seeds a project, an
approved task+contract, a delegation, and a QUEUED worker_run, then enqueues
the launch job and lets the running B worker daemon (A's run_worker + B's
handlers) process launch -> validate -> integrate automatically. Then it
triggers evolution and watches promotion. The only stubbed seams are the LLM
planner (we seed the contract) and the SDK coder (scripted bulk_faulty fixture).

Run (with the daemon + UI already up on the same AIRLOCK_DB):
  AIRLOCK_MONGO_URI=... AIRLOCK_DB=airlock_fullflow \
  TARGET_MONGO_URI=mongodb://127.0.0.1:27018/?replicaSet=rs0 \
  AIRLOCK_EXECUTION_MODE=local python scripts/run_fullflow.py
"""
from __future__ import annotations

import asyncio
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from tempfile import mkdtemp

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from controller import artifacts, db as dbmod, harness, snapshots
from controller.dev_fixtures import FIXTURES
from controller.scheduler import enqueue
from controller.states import DelegationState, JobKind, SCHEMA_VERSION, TaskState, WorkerRunState
from evolution.propose import open_run
from mongo.setup import setup

PID, TID, DID, WID = "proj_fullflow", "task_fullflow", "del_fullflow", "worker_fullflow"
UI = f"http://127.0.0.1:8099/?project={PID}"
COLLS = ["tasks", "delegations", "worker_runs", "jobs", "validation_runs",
         "integration_runs", "evolution_runs", "events", "artifacts", "harness_versions"]


def app_artifact(fixture: str) -> str:
    ck = Path(mkdtemp(prefix=f"app-{fixture}-"))
    for rel, content in snapshots.read_dir_tree(Path("target-app")).items():
        p = ck / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
    FIXTURES[fixture](ck, {})
    return artifacts.put_tree(snapshots.read_dir_tree(ck), kind="code_snapshot", project_id=PID,
                              base_artifact_id=None, created_by="supervisor_01")["_id"]


async def poll(db, coll, _id, field, done, label, timeout=240):
    for _ in range(timeout):
        doc = await db[coll].find_one({"_id": _id})
        cur = doc.get(field) if doc else None
        if cur in done:
            print(f"  {label}: {cur}")
            return doc
        await asyncio.sleep(1)
    raise TimeoutError(f"{label} stuck at {cur}")


async def main() -> None:
    async with dbmod.client() as c:
        db = dbmod.database(c)
        await setup(db)
        for coll in COLLS:
            await db[coll].delete_many({"project_id": PID})
        await db["projects"].delete_one({"_id": PID})

        now = datetime.now(UTC)
        base = artifacts.put_tree(snapshots.read_dir_tree(Path("target-app")), kind="baseline",
                                  project_id=PID, created_by="operator_01")
        hv1 = harness.bootstrap_bulk_v1(PID)
        await db["projects"].insert_one({
            "_id": PID, "schema_version": SCHEMA_VERSION, "name": "fullflow", "created_at": now,
            "repository": {"registered_root_id": "repo_full", "default_branch": "main"},
            "baseline_artifact_id": base["_id"], "active_harness_version_id": hv1["_id"],
            "harness_generation": 1, "constraint_epoch": 1, "validated_harness_ids": [hv1["_id"]],
            "event_seq": 0, "revision": 1})
        a = {"key": "bulk", "objective": "Add PATCH /items/bulk", "depends_on": [],
             "file_scope": ["app/**"], "profile_key": "bounded-items-worker"}
        await db["tasks"].insert_one({
            "_id": TID, "schema_version": SCHEMA_VERSION, "project_id": PID, "status": TaskState.RUNNING,
            "request": "atomic bulk item updates with idempotency", "revision": 1, "contract_version": 1,
            "event_seq": 0, "integration_seq": 0, "contract": {"assignments": [a]}, "contract_digest": "sha256:full",
            "approval": {"actor_id": "operator", "constraint_epoch": 1, "approved_at": now},
            "base_artifact_id": base["_id"], "delegation_ids": [DID], "final_artifact_id": None, "created_at": now})
        await db["delegations"].insert_one({
            "_id": DID, "schema_version": SCHEMA_VERSION, "project_id": PID, "task_id": TID, "assignment_key": "bulk",
            "objective": a["objective"], "depends_on": [], "file_scope": ["app/**"], "profile_key": a["profile_key"],
            "status": DelegationState.RUNNING, "attempt": 1, "revision": 1, "resolved_input_artifact_id": base["_id"],
            "contract_version": 1, "active_worker_run_id": WID, "accepted_artifact_id": None, "created_at": now})
        await db["worker_runs"].insert_one({
            "_id": WID, "schema_version": SCHEMA_VERSION, "project_id": PID, "task_id": TID, "delegation_id": DID,
            "attempt": 1, "status": WorkerRunState.QUEUED, "revision": 1, "input_artifact_id": base["_id"],
            "harness_version_id": hv1["_id"], "constraint_epoch": 1, "dev_fixture": "bulk_faulty",
            "output_artifact_id": None, "started_at": None, "finished_at": None, "created_at": now})
        async with c.start_session() as s:
            await enqueue(s, db, JobKind.LAUNCH, {"worker_run_id": WID, "delegation_id": DID}, f"{WID}:launch", PID, TID)

        print(f"seeded. watch live: {UI}")
        subprocess.run(["open", UI], check=False)

        print("\nthe B worker daemon is now processing launch -> validate -> integrate ...")
        await poll(db, "worker_runs", WID, "status", {WorkerRunState.FINISHED}, "worker_run")
        await poll(db, "delegations", DID, "status",
                   {DelegationState.ACCEPTED, DelegationState.REPAIR_REQUIRED}, "delegation")
        task = await poll(db, "tasks", TID, "status", {TaskState.COMPLETED, TaskState.FAILED}, "task")
        wr = await db["worker_runs"].find_one({"_id": WID})

        print("\ntriggering evolution (the escaped partial write drives a harness candidate) ...")
        clean = app_artifact("bulk_fixed")
        evo = open_run(project_id=PID, baseline_harness_version_id=hv1["_id"],
                       applications=[{"artifact_id": wr["output_artifact_id"], "label": "buggy"},
                                     {"artifact_id": clean, "label": "clean"}])
        await db["evolution_runs"].update_one({"_id": evo}, {"$set": {"constraint_epoch": 1}})
        async with c.start_session() as s:
            await enqueue(s, db, JobKind.EVOLVE, {"evolution_run_id": evo}, f"{evo}:evolve", PID, TID)
        e = await poll(db, "evolution_runs", evo, "status", {"promoted", "rejected", "eligible"}, "evolution")
        proj = await db["projects"].find_one({"_id": PID})
        m = e["evaluation"]["metrics"]
        print(f"\nRESULT: task={task['status']} | evolution={e['status']} "
              f"| baseline escaped={m['baseline']['escaped_failures']} candidate escaped={m['candidate']['escaped_failures']} "
              f"| active harness flipped v1->v2: {proj['active_harness_version_id'] == e['candidate_harness_version_id']}")
        print(f"keep exploring in the UI: {UI}")


if __name__ == "__main__":
    asyncio.run(main())
