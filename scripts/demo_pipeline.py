"""End-to-end demo of Person B's execution plane, driven through the job queue.

The enqueue / project-registration / pointer-flip steps here STAND IN for
Person A (the control plane). Everything they trigger — worker launch, freeze,
validation, integration, evolution, evaluation — is Person B's real code,
exercised exactly as the job worker will run it in production.

Walks the acceptance checks in spec section 14.

Run:  AIRLOCK_MONGO_URI=... AIRLOCK_EXECUTION_MODE=local python scripts/demo_pipeline.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.db import col
from common.enums import JobKind
from common.ids import new_id
from common.jobqueue import enqueue
from controller import artifacts, harness, snapshots, validation
from controller.jobrunner import serve
from evolution import propose

PROJECT = "proj_demo"


def banner(title: str) -> None:
    print(f"\n{'='*68}\n{title}\n{'='*68}")


def register_project_and_baseline() -> str:
    """[Person A] Register the project, capture the starting tree, set harness v1."""
    base_tree = snapshots.read_dir_tree(Path("demo-app"))
    base = artifacts.put_tree(base_tree, kind="baseline", project_id=PROJECT, created_by="operator_01")
    hv1 = harness.bootstrap_v1(PROJECT)
    col("projects").update_one(
        {"_id": PROJECT},
        {"$set": {"active_harness_version_id": hv1["_id"], "schema_version": 1,
                  "name": "auth-demo", "baseline_artifact_id": base["_id"],
                  "repository": {"registered_root_id": "repo_demo", "default_branch": "main"},
                  "harness_generation": 1, "constraint_epoch": 1, "revision": 1}},
        upsert=True,
    )
    print(f"project registered; baseline={base['_id']} active_harness=v1 ({hv1['_id']})")
    return base["_id"]


def launch_payload(worker_run_id, delegation_id, base_artifact_id, hv_id, fixture, scope):
    return {
        "worker_run_id": worker_run_id, "delegation_id": delegation_id,
        "task_id": "task_auth_01", "project_id": PROJECT,
        "base_artifact_id": base_artifact_id, "harness_version_id": hv_id,
        "contract_version": 1, "constraint_epoch": 1,
        "objective": "auth work", "file_scope": scope, "dev_fixture": fixture,
    }


def main() -> None:
    from mongo.setup import setup
    setup()
    base_id = register_project_and_baseline()
    active_hv = col("projects").find_one({"_id": PROJECT})["active_harness_version_id"]

    banner("CHECK 1-2: launch a worker, freeze output, validate real behavior (harness v1)")
    enqueue(kind=JobKind.LAUNCH,
            payload=launch_payload("worker_A", "del_A", base_id, active_hv, "noop", ["src/auth/**"]),
            dedupe_key="demo:launch:A", project_id=PROJECT, task_id="task_auth_01")
    serve(max_jobs=1)
    wr = col("worker_runs").find_one({"_id": "worker_A"})
    print(f"worker_A status={wr['status']} output={wr['output_artifact_id']} (frozen, not accepted)")

    val_id = validation.create_validation_run(
        project_id=PROJECT, task_id="task_auth_01", kind="candidate",
        candidate_artifact_id=wr["output_artifact_id"], base_artifact_id=base_id,
        harness_version_id=active_hv)
    enqueue(kind=JobKind.VALIDATE, payload={"validation_run_id": val_id},
            dedupe_key=f"demo:validate:{val_id}", project_id=PROJECT, task_id="task_auth_01")
    serve(max_jobs=1)
    v1 = col("validation_runs").find_one({"_id": val_id})
    print(f"harness v1 decision={v1['decision']}  <-- the credential leak ESCAPED (success-only harness)")

    banner("CHECK 3: compose two independent outputs, re-evaluate the combined revision")
    base_tree = artifacts.get_tree(base_id)
    tB = dict(base_tree); tB["src/auth/service.py"] = base_tree["src/auth/service.py"].replace(
        b'logger.error("login failed email=%s password=%s", email, password)',
        b'logger.error("login failed email=%s", email)')
    artB = artifacts.put_tree(tB, kind="code_snapshot", project_id=PROJECT, task_id="task_auth_01",
                              base_artifact_id=base_id, changed_files=["src/auth/service.py"], created_by="supervisor_01")
    tC = dict(base_tree); tC["src/auth/extra.py"] = b"# independent helper\n"
    artC = artifacts.put_tree(tC, kind="code_snapshot", project_id=PROJECT, task_id="task_auth_01",
                              base_artifact_id=base_id, changed_files=["src/auth/extra.py"], created_by="supervisor_01")
    ir_id = "int_demo_01"
    col("integration_runs").replace_one({"_id": ir_id}, {
        "_id": ir_id, "schema_version": 1, "project_id": PROJECT, "task_id": "task_auth_01",
        "status": "queued", "revision": 1, "base_artifact_id": base_id,
        "candidate_artifact_ids": [artB["_id"], artC["_id"]], "integration_fence": 0, "conflicts": []}, upsert=True)
    enqueue(kind=JobKind.INTEGRATE, payload={"integration_run_id": ir_id},
            dedupe_key="demo:integrate:01", project_id=PROJECT, task_id="task_auth_01")
    serve(max_jobs=1)
    ir = col("integration_runs").find_one({"_id": ir_id})
    print(f"integration status={ir['status']} combined={ir['result_artifact_id']} "
          f"validation={col('validation_runs').find_one({'_id': ir['validation_run_id']})['decision']}")

    banner("CHECK 4-5: escaped failure triggers an evolution candidate; evaluator compares harnesses")
    leaky = {"artifact_id": base_id, "label": "leaky"}
    clean = {"artifact_id": artB["_id"], "label": "clean"}
    evo_id = propose.open_run(project_id=PROJECT, baseline_harness_version_id=active_hv,
                              trigger_validation_run_id=val_id, applications=[leaky, clean])
    enqueue(kind=JobKind.EVOLVE, payload={"evolution_run_id": evo_id},
            dedupe_key=f"demo:evolve:{evo_id}", project_id=PROJECT, task_id="task_auth_01")
    serve(max_jobs=1)
    evo = col("evolution_runs").find_one({"_id": evo_id})
    m = evo["evaluation"]["metrics"]
    print(f"evolution status={evo['status']}")
    print(f"  baseline escaped={m['baseline']['escaped_failures']}  "
          f"candidate escaped={m['candidate']['escaped_failures']}  "
          f"(reduction={m['escaped_reduction']}, set size={m['comparison_set_size']})")

    banner("CHECK 6: promotion flips the pointer; the NEXT delegation records the new version")
    # [Person A] promotion controller would do this atomically; shown here for effect.
    cand_hv = evo["candidate_harness_version_id"]
    col("projects").update_one({"_id": PROJECT},
        {"$set": {"active_harness_version_id": cand_hv}, "$inc": {"harness_generation": 1}})
    enqueue(kind=JobKind.LAUNCH,
            payload=launch_payload("worker_D", "del_D", artB["_id"], cand_hv, "noop", ["src/auth/**"]),
            dedupe_key="demo:launch:D", project_id=PROJECT, task_id="task_auth_01")
    serve(max_jobs=1)
    wrD = col("worker_runs").find_one({"_id": "worker_D"})
    print(f"worker_D pinned harness_version={wrD['harness_version_id']} (== promoted candidate: {wrD['harness_version_id']==cand_hv})")

    banner("CHECK 7-8: legitimate case passes under v2; bad candidate is inconclusive; retries are idempotent")
    v2_on_clean = validation.create_validation_run(
        project_id=PROJECT, task_id="task_auth_01", kind="candidate",
        candidate_artifact_id=artB["_id"], base_artifact_id=base_id, harness_version_id=cand_hv)
    enqueue(kind=JobKind.VALIDATE, payload={"validation_run_id": v2_on_clean},
            dedupe_key=f"demo:validate:{v2_on_clean}", project_id=PROJECT, task_id="task_auth_01")
    serve(max_jobs=1)
    print(f"legitimate (fixed) app under v2 decision="
          f"{col('validation_runs').find_one({'_id': v2_on_clean})['decision']}  <-- still succeeds")

    broken_tree = dict(base_tree); broken_tree["app.py"] = b"this is not valid python :("
    broken = artifacts.put_tree(broken_tree, kind="code_snapshot", project_id=PROJECT, task_id="task_auth_01",
                                base_artifact_id=base_id, changed_files=["app.py"], created_by="supervisor_01")
    bad_val = validation.create_validation_run(
        project_id=PROJECT, task_id="task_auth_01", kind="candidate",
        candidate_artifact_id=broken["_id"], base_artifact_id=base_id, harness_version_id=cand_hv)
    enqueue(kind=JobKind.VALIDATE, payload={"validation_run_id": bad_val},
            dedupe_key=f"demo:validate:{bad_val}", project_id=PROJECT, task_id="task_auth_01")
    serve(max_jobs=1)
    print(f"bad candidate (won't start) decision="
          f"{col('validation_runs').find_one({'_id': bad_val})['decision']}  <-- inconclusive, NOT pass")

    j1 = enqueue(kind=JobKind.LAUNCH,
                 payload=launch_payload("worker_A", "del_A", base_id, active_hv, "noop", ["src/auth/**"]),
                 dedupe_key="demo:launch:A", project_id=PROJECT, task_id="task_auth_01")
    print(f"re-enqueue same dedupe_key -> same job id: {j1}  (no duplicate assignment)")
    n_worker_a_artifacts = col("artifacts").count_documents({"worker_run_id": "worker_A"})
    print(f"worker_A produced {n_worker_a_artifacts} artifact(s) despite retry (content-addressed, idempotent)")

    print("\nAll acceptance checks demonstrated.")


if __name__ == "__main__":
    main()
