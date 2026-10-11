"""Person B's execution-plane job handlers, plugged into Person A's seam.

A's `run_worker(db, owner, dispatch)` claims jobs and calls these; each reports
through A's `complete(db, job_id, owner, fence, apply)`, so B never writes
`status`/`revision` by hand — all entity transitions go through A's
`transition()`/`on_validation_pass()`/`promote()` inside the completion
transaction. The heavy work (materialize, run the agent, freeze, boot the app,
run the suite, compose, evaluate) is B's existing SYNC engine, run off the event
loop via `asyncio.to_thread`; its artifact/validation docs are written before
`complete`, idempotently (GridFS/Docker work can't be in a transaction).

Bridge note: B's engine uses a sync pymongo client (`common.db`) pointed at the
same database as A's async client (`AIRLOCK_DB`). Only non-state-machine docs
(artifacts, validation_runs, harness_versions) are written that way; every
state-machine transition happens through A's async helpers.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from tempfile import mkdtemp

from pymongo.asynchronous.database import AsyncDatabase

from controller.db import Doc
from controller.policies import accept_validation_guarded, promote
from controller.scheduler import complete, enqueue, on_integration_pass, on_validation_pass
from controller.states import (
    SCHEMA_VERSION,
    DelegationState,
    EvolutionRunState,
    IntegrationRunState,
    JobKind,
    Prefix,
    TaskState,
    ValidationDecision,
    WorkerRunState,
    append_event,
    new_id,
    transition,
)


async def _walk(session, coll, id_: str, rev: int, states, final_set: Doc | None = None) -> int:
    """Apply a chain of transitions within one transaction, tracking revision."""
    for i, st in enumerate(states):
        await transition(session, coll, id_, rev, st, final_set if i == len(states) - 1 else None)
        rev += 1
    return rev


# --------------------------------------------------------------------------- #
# launch: run the coding worker, freeze its output, queue validation           #
# --------------------------------------------------------------------------- #
def _launch_sync(wr: Doc, dele: Doc) -> Doc:
    from controller import artifacts, snapshots
    from controller.dev_fixtures import FIXTURES
    from controller.workers import ClaudeSDKWorker

    checkout = Path(mkdtemp(prefix=f"ckout-{wr['_id'][-8:]}-"))
    base_tree = artifacts.get_tree(wr["input_artifact_id"])
    artifacts.materialize(wr["input_artifact_id"], checkout)
    fixture = wr.get("dev_fixture") or dele.get("dev_fixture") or os.environ.get("AIRLOCK_DEV_FIXTURE")
    if fixture == "auto":  # repair demo: first attempt is faulty, the repair attempt is fixed
        fixture = "bulk_faulty" if wr.get("attempt", 1) == 1 else "bulk_fixed"
    if fixture:  # dev/demo convenience: a scripted edit stands in for the SDK agent
        FIXTURES[fixture](checkout, {})
    else:
        ClaudeSDKWorker().run(checkout, {"objective": dele.get("objective", ""), "file_scope": dele.get("file_scope", [])})
    return snapshots.freeze_worker_output(
        checkout_dir=checkout, base_tree=base_tree, project_id=wr["project_id"], task_id=wr["task_id"],
        worker_run_id=wr["_id"], base_artifact_id=wr["input_artifact_id"],
        allowed_scope=dele.get("file_scope", []), created_by="supervisor_01")


async def handle_launch(db: AsyncDatabase[Doc], job: Doc) -> None:
    owner, fence = job["lease"]["owner"], job["lease"]["fence"]
    p = job["payload"]
    wr = await db["worker_runs"].find_one({"_id": p["worker_run_id"]})
    dele = await db["delegations"].find_one({"_id": p["delegation_id"]})
    if wr is None or dele is None:
        raise LookupError("worker_run or delegation missing for launch")

    frozen = await asyncio.to_thread(_launch_sync, wr, dele)
    art_id = frozen["artifact"]["_id"]
    vp = {"delegation_id": dele["_id"], "worker_run_id": wr["_id"], "candidate_artifact_id": art_id,
          "base_artifact_id": wr["input_artifact_id"], "harness_version_id": wr["harness_version_id"]}

    async def apply(s) -> None:
        await _walk(s, db["worker_runs"], wr["_id"], wr["revision"],
                    [WorkerRunState.STARTING, WorkerRunState.RUNNING, WorkerRunState.SNAPSHOTTING, WorkerRunState.FINISHED],
                    final_set={"output_artifact_id": art_id, "finished_at": datetime.now(UTC)})
        await transition(s, db["delegations"], dele["_id"], dele["revision"], DelegationState.VALIDATING)
        jid = await enqueue(s, db, JobKind.VALIDATE, vp, f"{wr['_id']}:validate", wr["project_id"], wr["task_id"])
        await append_event(s, wr["task_id"], db["tasks"], "worker.finished", "supervisor",
                           {"worker_run_id": wr["_id"], "artifact_id": art_id, "scope_violations": frozen["scope_violations"], "validate_job": jid})

    await complete(db, job["_id"], owner, fence, apply, result_ref=art_id)


# --------------------------------------------------------------------------- #
# validate: run the trusted suite, record the verdict, pass or repair          #
# --------------------------------------------------------------------------- #
def _validate_sync(p: Doc) -> Doc:
    from controller import artifacts, harness
    from runner.bulk_execute import run_bulk_suite
    from runner.execute import run_suite

    config = harness.get_config(p["harness_version_id"])
    d = Path(mkdtemp(prefix="val-"))
    artifacts.materialize(p["candidate_artifact_id"], d)
    if config.get("runner") == "bulk":
        return run_bulk_suite(d, config, seed=103, run_tag=p["candidate_artifact_id"][-8:])
    return run_suite(d, config, seed=103)


async def handle_validate(db: AsyncDatabase[Doc], job: Doc) -> None:
    owner, fence = job["lease"]["owner"], job["lease"]["fence"]
    p = job["payload"]
    wr = await db["worker_runs"].find_one({"_id": p["worker_run_id"]})
    result = await asyncio.to_thread(_validate_sync, p)

    val_id = new_id(Prefix.VALIDATION_RUN)
    digest = "sha256:" + hashlib.sha256(json.dumps(p, sort_keys=True).encode()).hexdigest()
    await db["validation_runs"].insert_one({
        "_id": val_id, "schema_version": SCHEMA_VERSION, "project_id": wr["project_id"], "task_id": wr["task_id"],
        "kind": "candidate", "status": "completed", "decision": result["decision"], "input_digest": digest,
        "checks": result["checks"], "behavior_delta": result["behavior_delta"], "coverage": result["coverage"],
        "observer_ok": result["observer_ok"], "elapsed_ms": result["elapsed_ms"],
        "inputs": {"candidate_artifact_id": p["candidate_artifact_id"], "harness_version_id": p["harness_version_id"]},
        "created_at": datetime.now(UTC)})

    async def apply(s) -> None:
        dele = await db["delegations"].find_one({"_id": p["delegation_id"]}, session=s)
        decision = await accept_validation_guarded(s, db, ValidationDecision(result["decision"]), wr)
        if decision == ValidationDecision.PASS:
            await on_validation_pass(s, db, dele)
        else:
            await transition(s, db["delegations"], dele["_id"], dele["revision"], DelegationState.REPAIR_REQUIRED,
                             {"last_decision": decision})
            await append_event(s, wr["task_id"], db["tasks"], "validation.repair_required", "runner",
                               {"validation_run_id": val_id, "decision": decision})

    await complete(db, job["_id"], owner, fence, apply, result_ref=val_id)


# --------------------------------------------------------------------------- #
# integrate: compose onto the task tree, re-validate, accept or repair         #
# --------------------------------------------------------------------------- #
def _integrate_sync(dele: Doc, base_art: str, candidate_art: str, hv_id: str) -> Doc:
    from controller import artifacts, harness
    from controller.integration import compose
    from controller.snapshots import diff_trees
    from runner.bulk_execute import run_bulk_suite
    from runner.execute import run_suite

    base_tree = artifacts.get_tree(base_art)
    cdoc = artifacts.get_doc(candidate_art)
    ancestor = artifacts.get_tree(cdoc["base_artifact_id"]) if cdoc.get("base_artifact_id") else base_tree
    combined, conflicts = compose(base_tree, [{"artifact_id": candidate_art, "ancestor_tree": ancestor,
                                               "cand_tree": artifacts.get_tree(candidate_art)}])
    empty = {"combined_artifact_id": None, "decision": None, "checks": [], "behavior_delta": {}, "coverage": {}}
    if conflicts:
        return {"conflicts": conflicts, **empty}
    art = artifacts.put_tree(combined, kind="integrated", project_id=dele["project_id"], task_id=dele["task_id"],
                             base_artifact_id=base_art, changed_files=diff_trees(base_tree, combined)["changed_files"],
                             created_by="integrator_01")
    d = Path(mkdtemp(prefix="int-"))
    artifacts.materialize(art["_id"], d)
    config = harness.get_config(hv_id)
    res = run_bulk_suite(d, config, seed=103, run_tag="int" + art["_id"][-6:]) if config.get("runner") == "bulk" \
        else run_suite(d, config, seed=103)
    return {"conflicts": [], "combined_artifact_id": art["_id"], "decision": res["decision"],
            "checks": res["checks"], "behavior_delta": res["behavior_delta"], "coverage": res["coverage"]}


async def handle_integrate(db: AsyncDatabase[Doc], job: Doc) -> None:
    owner, fence = job["lease"]["owner"], job["lease"]["fence"]
    p = job["payload"]
    dele = await db["delegations"].find_one({"_id": p["delegation_id"]})
    wr = await db["worker_runs"].find_one({"_id": dele["active_worker_run_id"]})
    res = await asyncio.to_thread(_integrate_sync, dele, dele["resolved_input_artifact_id"],
                                  wr["output_artifact_id"], wr["harness_version_id"])
    now = datetime.now(UTC)

    # Persist the combined validation run (non-state-machine doc) before completion.
    val_id = None
    if res["combined_artifact_id"] is not None:
        val_id = new_id(Prefix.VALIDATION_RUN)
        await db["validation_runs"].insert_one({
            "_id": val_id, "schema_version": SCHEMA_VERSION, "project_id": dele["project_id"], "task_id": dele["task_id"],
            "kind": "integration", "status": "completed", "decision": res["decision"], "checks": res["checks"],
            "behavior_delta": res["behavior_delta"], "coverage": res["coverage"],
            "inputs": {"candidate_artifact_id": res["combined_artifact_id"], "harness_version_id": wr["harness_version_id"]},
            "created_at": now})

    async def apply(s) -> None:
        d = await db["delegations"].find_one({"_id": dele["_id"]}, session=s)
        if res["conflicts"] or res["decision"] != ValidationDecision.PASS.value:
            status = IntegrationRunState.CONFLICTED if res["conflicts"] else IntegrationRunState.FAILED
            await db["integration_runs"].insert_one({
                "_id": new_id(Prefix.INTEGRATION_RUN), "schema_version": SCHEMA_VERSION, "project_id": dele["project_id"],
                "task_id": dele["task_id"], "delegation_id": dele["_id"], "status": status, "revision": 1,
                "integration_fence": p["integration_seq"], "result_artifact_id": res["combined_artifact_id"],
                "conflicts": res["conflicts"], "validation_run_id": val_id, "created_at": now}, session=s)
            await transition(s, db["delegations"], dele["_id"], d["revision"], DelegationState.REPAIR_REQUIRED,
                             {"reason": "conflict" if res["conflicts"] else "combined_validation"})
            return
        # A's helper: accept the integration + delegation and complete the task if this integration is final.
        await on_integration_pass(s, db, d, p["integration_seq"], res["combined_artifact_id"], val_id)

    await complete(db, job["_id"], owner, fence, apply, result_ref=val_id)


# --------------------------------------------------------------------------- #
# evolve: generate a candidate harness, paired-evaluate, promote if eligible   #
# --------------------------------------------------------------------------- #
def _evolve_sync(evo: Doc) -> Doc:
    from controller import harness
    from evolution import evaluate as ev
    from evolution.propose import propose_config

    baseline_cfg = harness.get_config(evo["baseline_harness_version_id"])
    cfg, hypothesis, changes = propose_config(baseline_cfg, {})
    candidate = harness.create_harness_version(project_id=evo["project_id"], config=cfg,
                                               parent_id=evo["baseline_harness_version_id"],
                                               created_by_evolution_run_id=evo["_id"])
    apps = evo["evaluation"]["applications"]
    baseline = ev._run_matrix(evo["baseline_harness_version_id"], apps)
    cand = ev._run_matrix(candidate["_id"], apps)
    eligible = (cand["protected_invariant_failures"] == 0
                and cand["escaped_failures"] < baseline["escaped_failures"]
                and cand["legitimate_blocks"] <= baseline["legitimate_blocks"]
                and cand["total_elapsed_ms"] <= max(1, baseline["total_elapsed_ms"]) * ev.OVERHEAD_FACTOR)
    metrics = {"comparison_set_size": len(apps) * len(ev.SEEDS), "seeds": list(ev.SEEDS),
               "baseline": {k: baseline[k] for k in ("escaped_failures", "legitimate_blocks", "protected_invariant_failures", "total_elapsed_ms")},
               "candidate": {k: cand[k] for k in ("escaped_failures", "legitimate_blocks", "protected_invariant_failures", "total_elapsed_ms")},
               "escaped_reduction": baseline["escaped_failures"] - cand["escaped_failures"]}
    return {"candidate_hv": candidate["_id"], "hypothesis": hypothesis, "changes": changes,
            "metrics": metrics, "eligible": eligible}


async def handle_evolve(db: AsyncDatabase[Doc], job: Doc) -> None:
    owner, fence = job["lease"]["owner"], job["lease"]["fence"]
    evo = await db["evolution_runs"].find_one({"_id": job["payload"]["evolution_run_id"]})
    out = await asyncio.to_thread(_evolve_sync, evo)
    # record evaluation evidence (non-state fields) before completion
    await db["evolution_runs"].update_one({"_id": evo["_id"]}, {"$set": {
        "candidate_harness_version_id": out["candidate_hv"], "hypothesis": out["hypothesis"],
        "changes": out["changes"], "evaluation.metrics": out["metrics"]}})

    async def apply(s) -> None:
        e = await db["evolution_runs"].find_one({"_id": evo["_id"]}, session=s)
        rev = await _walk(s, db["evolution_runs"], evo["_id"], e["revision"],
                          [EvolutionRunState.GENERATING, EvolutionRunState.EVALUATING])
        final = EvolutionRunState.ELIGIBLE if out["eligible"] else EvolutionRunState.REJECTED
        await transition(s, db["evolution_runs"], evo["_id"], rev, final)
        await append_event(s, evo["project_id"], db["projects"], f"evolution.{final}", "evaluator",
                           {"evolution_run_id": evo["_id"], "metrics": out["metrics"]})
        if out["eligible"]:
            await promote(s, db, evo["_id"], "promotion_controller")

    await complete(db, job["_id"], owner, fence, apply, result_ref=evo["_id"])


# --------------------------------------------------------------------------- #
# plan: materialize the repo for the planner and run A's plan job             #
# --------------------------------------------------------------------------- #
def _materialize_base(base_artifact_id: str) -> str:
    from controller import artifacts
    checkout = Path(mkdtemp(prefix="plan-"))
    artifacts.materialize(base_artifact_id, checkout)
    return str(checkout)


async def _stub_propose(request: str, checkout: Path, errors=None) -> dict:
    """Deterministic fallback planner (no SDK): the bulk-update contract."""
    return {
        "constraints": ["An update may only touch the caller's own items", "All-or-nothing: either every update applies or none"],
        "acceptance": ["a valid batch updates every listed item",
                       "a mixed-owner batch is rejected AND leaves every affected row unchanged",
                       "an identical retry with the same Idempotency-Key returns the original result",
                       "reusing a key with a different payload returns 409"],
        "interfaces": {"endpoint": "PATCH /api/v1/items/bulk", "idempotency": "Idempotency-Key request header"},
        "assignments": [{"key": "bulk", "objective": "Implement PATCH /api/v1/items/bulk: atomic bulk update with idempotency",
                         "depends_on": [], "file_scope": ["app/**"], "profile_key": "bounded-items-worker"}],
    }


async def handle_plan(db: AsyncDatabase[Doc], job: Doc) -> None:
    """Run A's plan job with a materialized checkout; real SDK planner by default,
    deterministic stub when AIRLOCK_STUB_PLAN is set."""
    from controller.planner import run_plan_job, sdk_propose
    owner, fence = job["lease"]["owner"], job["lease"]["fence"]
    task = await db["tasks"].find_one({"_id": job["task_id"]})
    checkout = Path(await asyncio.to_thread(_materialize_base, task["base_artifact_id"]))
    propose = _stub_propose if os.environ.get("AIRLOCK_STUB_PLAN") else sdk_propose
    await run_plan_job(db, job, owner, fence, checkout, propose)
