"""Serial composition and combined validation (spec section 7.9, 9).

One integration writer per task. Sibling patches may share an original base even
though the staging tree changes after the first patch, so composition is an
explicit three-way merge against each candidate's recorded ancestor — not a
demand for identical staging hashes. Conflicts are reported (and surfaced for a
repair assignment), never silently overwritten. Final acceptance is conditioned
on the integration fence, task revision, contract version, and constraint epoch.
"""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from common.db import col
from common.enums import ArtifactKind, IntegrationState, ValidationDecision
from common.hashing import FileTree, canonical_tree_hash
from controller import artifacts, validation
from controller.snapshots import _is_text, diff_trees


def _merge_file(ancestor: bytes, current: bytes, other: bytes) -> tuple[bytes, bool]:
    """Three-way merge of a single file. Returns (merged_bytes, conflicted)."""
    if current == other:
        return current, False
    if current == ancestor:
        return other, False
    if other == ancestor:
        return current, False
    if not (_is_text(ancestor) and _is_text(current) and _is_text(other)):
        return current, True  # cannot auto-merge divergent binaries
    with tempfile.TemporaryDirectory() as d:
        cur_p, base_p, oth_p = Path(d) / "cur", Path(d) / "base", Path(d) / "oth"
        cur_p.write_bytes(current)
        base_p.write_bytes(ancestor)
        oth_p.write_bytes(other)
        res = subprocess.run(
            ["git", "merge-file", "-p", str(cur_p), str(base_p), str(oth_p)],
            capture_output=True,
        )
        # returncode 0 = clean; >0 = number of conflict hunks; <0 = error
        return res.stdout, res.returncode != 0


def compose(base_tree: FileTree, candidates: list[dict]) -> tuple[FileTree, list[dict]]:
    """Apply candidate patches serially with three-way merges.

    Each candidate dict has: artifact_id, ancestor_tree, cand_tree.
    """
    staging: FileTree = dict(base_tree)
    conflicts: list[dict] = []

    for c in candidates:
        anc, cand, aid = c["ancestor_tree"], c["cand_tree"], c["artifact_id"]
        diff = diff_trees(anc, cand)

        for f in diff["added"]:
            if f not in staging or staging[f] == cand[f]:
                staging[f] = cand[f]
            else:
                merged, conf = _merge_file(b"", staging[f], cand[f])
                staging[f] = merged
                if conf:
                    conflicts.append({"artifact_id": aid, "file": f, "type": "add/add"})

        for f in diff["modified"]:
            cur = staging.get(f, anc[f])
            merged, conf = _merge_file(anc[f], cur, cand[f])
            staging[f] = merged
            if conf:
                conflicts.append({"artifact_id": aid, "file": f, "type": "modify/modify"})

        for f in diff["deleted"]:
            if staging.get(f) == anc.get(f):
                staging.pop(f, None)
            else:
                conflicts.append({"artifact_id": aid, "file": f, "type": "delete/modify"})

    return staging, conflicts


def _active_harness_version_id(project_id: str, fallback_from_run: dict) -> str:
    if fallback_from_run.get("harness_version_id"):
        return fallback_from_run["harness_version_id"]
    project = col("projects").find_one({"_id": project_id}, projection={"active_harness_version_id": 1})
    if project and project.get("active_harness_version_id"):
        return project["active_harness_version_id"]
    latest = col("harness_versions").find_one(
        {"project_id": project_id}, sort=[("version", -1)], projection={"_id": 1}
    )
    if latest is None:
        raise RuntimeError(f"no harness version available for project {project_id}")
    return latest["_id"]


def integrate(integration_run_id: str) -> dict:
    run = col("integration_runs").find_one({"_id": integration_run_id})
    if run is None:
        raise KeyError(f"unknown integration run {integration_run_id!r}")

    # Acquire the integration fence for this writer.
    fenced = col("integration_runs").find_one_and_update(
        {"_id": integration_run_id},
        {"$inc": {"integration_fence": 1}, "$set": {"status": IntegrationState.COMPOSING.value}},
        return_document=True,
    )
    fence = fenced["integration_fence"]

    base_tree = artifacts.get_tree(run["base_artifact_id"])
    candidates = []
    for aid in run["candidate_artifact_ids"]:
        cdoc = artifacts.get_doc(aid)
        candidates.append({
            "artifact_id": aid,
            "ancestor_tree": artifacts.get_tree(cdoc["base_artifact_id"]) if cdoc.get("base_artifact_id") else base_tree,
            "cand_tree": artifacts.get_tree(aid),
        })

    combined, conflicts = compose(base_tree, candidates)

    if conflicts:
        col("integration_runs").update_one(
            {"_id": integration_run_id, "integration_fence": fence},
            {"$set": {"status": IntegrationState.CONFLICT.value, "conflicts": conflicts,
                      "composition_order": run["candidate_artifact_ids"]}},
        )
        return {"integration_run_id": integration_run_id, "status": "conflict", "conflicts": conflicts}

    combined_artifact = artifacts.put_tree(
        combined,
        kind=ArtifactKind.INTEGRATED,
        project_id=run["project_id"],
        task_id=run["task_id"],
        base_artifact_id=run["base_artifact_id"],
        changed_files=diff_trees(base_tree, combined)["changed_files"],
        created_by="integrator_01",
    )

    hv_id = _active_harness_version_id(run["project_id"], run)
    val_id = validation.create_validation_run(
        project_id=run["project_id"],
        task_id=run["task_id"],
        kind="integration",
        candidate_artifact_id=combined_artifact["_id"],
        base_artifact_id=run["base_artifact_id"],
        harness_version_id=hv_id,
        integration_run_id=integration_run_id,
    )
    verdict = validation.execute_validation(val_id)

    # Finalize conditioned on the fence (no newer integration writer).
    accepted = verdict["decision"] == ValidationDecision.PASS.value
    final_status = IntegrationState.COMPLETED.value if accepted else IntegrationState.VALIDATING.value
    updated = col("integration_runs").update_one(
        {"_id": integration_run_id, "integration_fence": fence},
        {"$set": {
            "status": final_status,
            "result_artifact_id": combined_artifact["_id"],
            "validation_run_id": val_id,
            "composition_order": run["candidate_artifact_ids"],
            "conflicts": [],
        }},
    )
    stale = updated.modified_count == 0  # a newer writer bumped the fence
    return {
        "integration_run_id": integration_run_id,
        "status": "stale" if stale else final_status,
        "result_artifact_id": combined_artifact["_id"],
        "combined_tree_hash": canonical_tree_hash(combined),
        "validation_run_id": val_id,
        "decision": verdict["decision"],
    }
