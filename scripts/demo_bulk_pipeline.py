"""End-to-end bulk-update demo, driven through the job queue (stands in for A).

Airlock state goes to the Atlas cluster (AIRLOCK_MONGO_URI); the target app
under test uses a SEPARATE Mongo (TARGET_MONGO_URI). The enqueue /
registration / pointer-flip steps stand in for Person A; everything they
trigger is Person B's real code.

Run:
  AIRLOCK_MONGO_URI="mongodb+srv://..."  \
  TARGET_MONGO_URI="mongodb://127.0.0.1:27018/?replicaSet=rs0" \
  python scripts/demo_bulk_pipeline.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.db import col
from common.enums import JobKind
from common.jobqueue import enqueue
from controller import artifacts, harness, snapshots, validation
from controller.jobrunner import serve
from evolution import propose

PROJECT = "proj_bulk"


def banner(t):
    print(f"\n{'='*70}\n{t}\n{'='*70}")


def register():
    tree = snapshots.read_dir_tree(Path("target-app"))
    base = artifacts.put_tree(tree, kind="baseline", project_id=PROJECT, created_by="operator_01")
    hv1 = harness.bootstrap_bulk_v1(PROJECT)
    col("projects").update_one(
        {"_id": PROJECT},
        {"$set": {"active_harness_version_id": hv1["_id"], "schema_version": 1, "name": "items-demo",
                  "baseline_artifact_id": base["_id"],
                  "repository": {"registered_root_id": "repo_items", "default_branch": "main"},
                  "harness_generation": 1, "constraint_epoch": 1, "revision": 1}},
        upsert=True)
    print(f"registered project; baseline={base['_id']} active_harness=v1 ({hv1['_id']})")
    return base["_id"], hv1["_id"]


def launch(worker_run_id, delegation_id, base_id, hv_id, fixture):
    enqueue(kind=JobKind.LAUNCH,
            payload={"worker_run_id": worker_run_id, "delegation_id": delegation_id,
                     "task_id": "task_bulk_01", "project_id": PROJECT, "base_artifact_id": base_id,
                     "harness_version_id": hv_id, "contract_version": 1, "constraint_epoch": 1,
                     "objective": "bulk update feature", "file_scope": ["app/**"], "dev_fixture": fixture},
            dedupe_key=f"bulk:launch:{worker_run_id}", project_id=PROJECT, task_id="task_bulk_01")
    serve(max_jobs=1)
    return col("worker_runs").find_one({"_id": worker_run_id})["output_artifact_id"]


def validate(candidate_id, base_id, hv_id, tag):
    vid = validation.create_validation_run(project_id=PROJECT, task_id="task_bulk_01", kind="candidate",
                                           candidate_artifact_id=candidate_id, base_artifact_id=base_id,
                                           harness_version_id=hv_id)
    enqueue(kind=JobKind.VALIDATE, payload={"validation_run_id": vid},
            dedupe_key=f"bulk:validate:{tag}:{vid}", project_id=PROJECT, task_id="task_bulk_01")
    serve(max_jobs=1)
    return col("validation_runs").find_one({"_id": vid})


def main():
    from mongo.setup import setup
    setup()
    base_id, v1 = register()

    banner("CHECK 1-2: workers build the feature; freeze; validate under harness v1 (status-only)")
    faulty = launch("worker_faulty", "del_faulty", base_id, v1, "bulk_faulty")
    fixed = launch("worker_fixed", "del_fixed", base_id, v1, "bulk_fixed")
    print(f"faulty candidate={faulty}  fixed candidate={fixed}")
    vf = validate(faulty, base_id, v1, "faulty-v1")
    print(f"harness v1 on FAULTY -> decision={vf['decision']}  (partial write ESCAPES a status-only harness)")

    banner("CHECK 4-5: escaped failure triggers evolution; evaluator compares v1 vs v2")
    evo = propose.open_run(project_id=PROJECT, baseline_harness_version_id=v1,
                           trigger_validation_run_id=vf["_id"],
                           applications=[{"artifact_id": faulty, "label": "buggy"},
                                         {"artifact_id": fixed, "label": "clean"}])
    enqueue(kind=JobKind.EVOLVE, payload={"evolution_run_id": evo},
            dedupe_key=f"bulk:evolve:{evo}", project_id=PROJECT, task_id="task_bulk_01")
    serve(max_jobs=1)
    e = col("evolution_runs").find_one({"_id": evo})
    m = e["evaluation"]["metrics"]
    print(f"evolution -> {e['status']}  | v2 changes: {e['changes']}")
    print(f"  baseline escaped={m['baseline']['escaped_failures']}  candidate escaped={m['candidate']['escaped_failures']}"
          f"  (reduction={m['escaped_reduction']}, set size={m['comparison_set_size']}, legit blocks={m['candidate']['legitimate_blocks']})")

    banner("CHECK 6-7: promote v2; repair the app; verify under v2")
    cand_hv = e["candidate_harness_version_id"]
    col("projects").update_one({"_id": PROJECT}, {"$set": {"active_harness_version_id": cand_hv},
                                                  "$inc": {"harness_generation": 1}})
    vfx = validate(fixed, base_id, cand_hv, "fixed-v2")
    print(f"harness v2 on FIXED  -> decision={vfx['decision']}  (legitimate repair still passes)")
    vff = validate(faulty, base_id, cand_hv, "faulty-v2")
    rows = next((c for c in vff["checks"] if c["id"] == "rows_unchanged_on_rejection"), {})
    print(f"harness v2 on FAULTY -> decision={vff['decision']}  (now CAUGHT); rows_unchanged={rows.get('status')}")

    print("\nDone. Inspect state with:  python scripts/monitor.py proj_bulk")


if __name__ == "__main__":
    main()
