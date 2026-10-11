"""Structured harness-improvement proposals (spec section 10).

Given an escaped-failure signal, the evolver proposes a candidate harness
configuration plus a hypothesis and a prediction. Proposals are schema-
constrained data: they are validated by the controller and never directly
update the active-harness pointer. The evolver may only change mutable
components within the fixed permission ceiling:

  - context-selection patterns and budgets
  - worker-profile selection within a fixed permission ceiling
  - required scenario tags and scheduling
  - a generated scenario artifact (executed in the sandbox)
  - inspection selection from implemented capabilities

It may NOT touch the evaluator, its thresholds, the holdout, the permission
boundary, or the audit/state authority.

The deterministic proposer below is the hackathon-safe default. A Claude Agent
SDK structured-output variant can replace `propose_config` while keeping the
same validated contract.
"""
from __future__ import annotations

from common.db import col
from common.enums import EvolutionState
from common.ids import new_id, utcnow
from controller import harness


def propose_config(baseline_config: dict, trigger_evidence: dict) -> tuple[dict, str, list[str]]:
    """Return (candidate_config, hypothesis, changes).

    The escaped failure was a credential leak on an exception path that a
    success-only harness never exercised or inspected. The proposal exercises
    the error path and enables the implemented sensitive-log inspector, and
    includes the logging module in worker context.
    """
    candidate = {
        **baseline_config,
        "context": {
            **baseline_config.get("context", {}),
            "include_patterns": sorted(
                set(baseline_config.get("context", {}).get("include_patterns", []))
                | {"src/auth/**", "**/logging*.py"}
            ),
        },
        "validation": {
            **baseline_config.get("validation", {}),
            "required_tags": sorted(
                set(baseline_config.get("validation", {}).get("required_tags", [])) | {"error_path"}
            ),
            "inspections": sorted(
                set(baseline_config.get("validation", {}).get("inspections", [])) | {"sensitive_logs"}
            ),
        },
    }
    hypothesis = (
        "Failure-path execution and log inspection catch credential leakage "
        "missed by success-only evaluation."
    )
    changes = ["Add an error-path scenario tag", "Enable sensitive-log inspection",
               "Include the logging module in worker context"]
    return candidate, hypothesis, changes


def propose(
    *,
    project_id: str,
    baseline_harness_version_id: str,
    trigger_validation_run_id: str | None = None,
    applications: list[dict] | None = None,
    holdout_suite_version_id: str = "holdout_auth_001",
) -> str:
    """Create an evolution run with a generated candidate harness version.

    `applications` are the trusted holdout's labeled application artifacts
    ([{artifact_id, label}]); their labels come from the holdout and are
    inaccessible to the evolver's config generation.
    """
    baseline_config = harness.get_config(baseline_harness_version_id)
    trigger = col("validation_runs").find_one({"_id": trigger_validation_run_id}) or {}

    run_id = new_id("evo")
    col("evolution_runs").insert_one({
        "_id": run_id,
        "schema_version": 1,
        "project_id": project_id,
        "trigger_validation_run_id": trigger_validation_run_id,
        "status": EvolutionState.GENERATING.value,
        "revision": 1,
        "baseline_harness_version_id": baseline_harness_version_id,
        "candidate_harness_version_id": None,
        "hypothesis": "",
        "changes": [],
        "evaluation": {
            "holdout_suite_version_id": holdout_suite_version_id,
            "applications": applications or [],
            "baseline_run_ids": [],
            "candidate_run_ids": [],
            "metrics": None,
        },
        "promotion": None,
        "created_at": utcnow(),
    })

    candidate_config, hypothesis, changes = propose_config(baseline_config, trigger.get("behavior_delta", {}))
    candidate = harness.create_harness_version(
        project_id=project_id,
        config=candidate_config,
        parent_id=baseline_harness_version_id,
        created_by_evolution_run_id=run_id,
    )

    col("evolution_runs").update_one(
        {"_id": run_id},
        {"$set": {
            "status": EvolutionState.EVALUATING.value,
            "candidate_harness_version_id": candidate["_id"],
            "hypothesis": hypothesis,
            "changes": changes,
        }},
    )
    return run_id
