"""Paired independent evaluation of harness candidates (spec section 10).

Evaluates the baseline and candidate harness on the SAME frozen application
artifacts, with fresh canary values and multiple seeds, and at least one benign
application. Detector replay alone only proves improved detection; the labeled
set here includes a clean app so we also check legitimate-task acceptance is not
regressed.

Promotion thresholds are predeclared for this demo:
  - no regression on protected invariants (benign app still accepted),
  - strictly fewer escaped failures on the fixed comparison set,
  - no decrease in legitimate-task acceptance on that set,
  - bounded execution overhead.

Report counts and suite size, not generalized percentages from one case. The
evaluator — not the evolver — writes metrics and decides eligibility. It never
promotes; Person A's promotion controller advances the active pointer.
"""
from __future__ import annotations

from pathlib import Path
from tempfile import mkdtemp

from common.config import get_settings
from common.db import col
from common.enums import EvolutionState, ValidationDecision
from controller import artifacts, harness

SEEDS = (101, 202, 303)
OVERHEAD_FACTOR = 3.0  # candidate may cost up to 3x baseline wall-clock


_BUGGY_LABELS = {"buggy", "leaky", "has_partial_write"}


def _run_matrix(harness_version_id: str, applications: list[dict]) -> dict:
    """Run one harness across labeled apps × seeds. Returns summarized metrics.

    Runner-agnostic: the harness config's `runner` field selects the login or
    bulk runner. Labels are the trusted holdout's, not evolver-supplied."""
    from common.hashing import sha256_hex

    config = harness.get_config(harness_version_id)
    if config.get("runner") == "bulk":
        from runner.bulk_execute import run_bulk_suite
        def _run(run_dir, seed, tag):
            return run_bulk_suite(run_dir, config, seed=seed, run_tag=tag)
    else:
        from runner.execute import run_suite
        def _run(run_dir, seed, tag):
            return run_suite(run_dir, config, seed=seed, canary_salt=f"fresh-{seed}")

    escaped = 0          # app truly has the defect, harness failed to catch (decision == pass)
    false_block = 0      # clean app blocked (decision != pass)
    protected_fail = 0   # a clean app had any failing check
    total_ms = 0
    runs = []

    for app in applications:
        label = app["label"]
        tag = sha256_hex((harness_version_id + app["artifact_id"]).encode())[:8]
        for seed in SEEDS:
            run_dir = Path(mkdtemp(prefix="evo-", dir=str(get_settings().ensure_work_root())))
            artifacts.materialize(app["artifact_id"], run_dir)
            res = _run(run_dir, seed, f"{seed}_{tag}")
            decision = res["decision"]

            if label in _BUGGY_LABELS and decision == ValidationDecision.PASS.value:
                escaped += 1
            if label == "clean":
                if decision != ValidationDecision.PASS.value:
                    false_block += 1
                if any(c["status"] == "fail" for c in res["checks"]):
                    protected_fail += 1

            total_ms += res["elapsed_ms"]
            runs.append({"artifact_id": app["artifact_id"], "label": label, "seed": seed,
                         "decision": decision, "elapsed_ms": res["elapsed_ms"]})

    return {
        "escaped_failures": escaped,
        "legitimate_blocks": false_block,
        "protected_invariant_failures": protected_fail,
        "total_elapsed_ms": total_ms,
        "runs": runs,
    }


def evaluate_run(evolution_run_id: str) -> dict:
    run = col("evolution_runs").find_one({"_id": evolution_run_id})
    if run is None:
        raise KeyError(f"unknown evolution run {evolution_run_id!r}")
    applications = run["evaluation"]["applications"]
    if not applications:
        raise RuntimeError("evolution run has no labeled applications to evaluate")

    baseline = _run_matrix(run["baseline_harness_version_id"], applications)
    candidate = _run_matrix(run["candidate_harness_version_id"], applications)

    comparison = len(applications) * len(SEEDS)
    eligible = (
        candidate["protected_invariant_failures"] == 0
        and candidate["escaped_failures"] < baseline["escaped_failures"]
        and candidate["legitimate_blocks"] <= baseline["legitimate_blocks"]
        and candidate["total_elapsed_ms"] <= max(1, baseline["total_elapsed_ms"]) * OVERHEAD_FACTOR
    )

    metrics = {
        "comparison_set_size": comparison,
        "seeds": list(SEEDS),
        "baseline": {k: baseline[k] for k in
                     ("escaped_failures", "legitimate_blocks", "protected_invariant_failures", "total_elapsed_ms")},
        "candidate": {k: candidate[k] for k in
                      ("escaped_failures", "legitimate_blocks", "protected_invariant_failures", "total_elapsed_ms")},
        "escaped_reduction": baseline["escaped_failures"] - candidate["escaped_failures"],
    }

    col("evolution_runs").update_one(
        {"_id": evolution_run_id},
        {"$set": {
            "status": EvolutionState.ELIGIBLE.value if eligible else EvolutionState.REJECTED.value,
            "evaluation.metrics": metrics,
            "evaluation.baseline_run_ids": baseline["runs"],
            "evaluation.candidate_run_ids": candidate["runs"],
        }},
    )
    return {"evolution_run_id": evolution_run_id,
            "status": "eligible" if eligible else "rejected", "metrics": metrics}
