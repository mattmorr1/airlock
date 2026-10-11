"""Validation runs: exact inputs, observed outcomes, cached verdicts (spec 7.6).

Creates and executes validation runs. The cache key is the full exact-input
digest (tree, baseline, harness, suite, contract, runner image, seed, epoch) —
not a diff hash — so a completed verdict can be safely reused. A timeout,
observer failure, or missing scenario yields `inconclusive`, never pass.
"""
from __future__ import annotations

from pathlib import Path
from tempfile import mkdtemp

from pymongo.errors import DuplicateKeyError

from common.config import get_settings
from common.db import col
from common.enums import ValidationDecision
from common.hashing import sha256_hex
from common.ids import new_id, utcnow
from controller import artifacts, harness
from runner.execute import run_suite


def runner_image_digest() -> str:
    s = get_settings()
    return "local-dev" if s.execution_mode == "local" else s.runner_image


def cache_key(
    *, candidate_tree_hash: str, base_artifact_id: str, harness_version_id: str,
    suite_version_id: str, contract_version: int, constraint_epoch: int, seed: int,
) -> str:
    raw = ":".join([
        candidate_tree_hash, base_artifact_id, harness_version_id, suite_version_id,
        str(contract_version), runner_image_digest(), str(seed), str(constraint_epoch),
    ])
    return "ck:" + sha256_hex(raw.encode("utf-8"))


def create_validation_run(
    *,
    project_id: str,
    task_id: str | None,
    kind: str,
    candidate_artifact_id: str,
    base_artifact_id: str,
    harness_version_id: str,
    suite_version_id: str = "suite_auth_001",
    contract_version: int = 1,
    constraint_epoch: int = 1,
    seed: int = 103,
    canary_salt: str | None = None,
    integration_run_id: str | None = None,
) -> str:
    """Insert a queued validation run (or return a cached completed one)."""
    cand = artifacts.get_doc(candidate_artifact_id)
    ck = cache_key(
        candidate_tree_hash=cand["tree_hash"], base_artifact_id=base_artifact_id,
        harness_version_id=harness_version_id, suite_version_id=suite_version_id,
        contract_version=contract_version, constraint_epoch=constraint_epoch, seed=seed,
    )
    cached = col("validation_runs").find_one({"inputs.cache_key": ck, "status": "completed"})
    if cached is not None:
        return cached["_id"]

    run_id = new_id("val")
    doc = {
        "_id": run_id,
        "schema_version": 1,
        "project_id": project_id,
        "task_id": task_id,
        "kind": kind,
        "status": "queued",
        "inputs": {
            "baseline_artifact_id": base_artifact_id,
            "candidate_artifact_id": candidate_artifact_id,
            "candidate_tree_hash": cand["tree_hash"],
            "harness_version_id": harness_version_id,
            "contract_version": contract_version,
            "constraint_epoch": constraint_epoch,
            "suite_version_id": suite_version_id,
            "runner_image_digest": runner_image_digest(),
            "seed": seed,
            "canary_salt": canary_salt,
            "cache_key": ck,
            "integration_run_id": integration_run_id,
        },
        "checks": [],
        "behavior_delta": {},
        "decision": None,
        "coverage": {},
        "observer_ok": True,
        "elapsed_ms": 0,
        "created_at": utcnow(),
    }
    try:
        col("validation_runs").insert_one(doc)
    except DuplicateKeyError:
        existing = col("validation_runs").find_one({"inputs.cache_key": ck})
        return existing["_id"]
    return run_id


def execute_validation(validation_run_id: str) -> dict:
    """Run the suite for a queued validation run and persist the verdict."""
    doc = col("validation_runs").find_one({"_id": validation_run_id})
    if doc is None:
        raise KeyError(f"unknown validation run {validation_run_id!r}")
    inputs = doc["inputs"]

    config = harness.get_config(inputs["harness_version_id"])
    run_dir = Path(mkdtemp(prefix="val-", dir=str(get_settings().ensure_work_root())))
    artifacts.materialize(inputs["candidate_artifact_id"], run_dir)

    if config.get("runner") == "bulk":
        from runner.bulk_execute import run_bulk_suite
        result = run_bulk_suite(run_dir, config, seed=inputs["seed"], run_tag=validation_run_id)
    else:
        result = run_suite(run_dir, config, seed=inputs["seed"], canary_salt=inputs.get("canary_salt"))

    col("validation_runs").update_one(
        {"_id": validation_run_id},
        {"$set": {
            "status": "completed",
            "checks": result["checks"],
            "behavior_delta": result["behavior_delta"],
            "decision": result["decision"],
            "coverage": result["coverage"],
            "observer_ok": result["observer_ok"],
            "elapsed_ms": result["elapsed_ms"],
        }},
    )
    return {"validation_run_id": validation_run_id, "decision": result["decision"], **result}
