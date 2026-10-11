"""Collections, indexes, and schema validators (spec section 8).

Shared schema — the frozen contract both halves of Airlock agree on. Authored
here so either side can stand up a database; Person A refines validators as the
API contracts settle. Controller-level invariants (cross-document ownership,
DAG consistency, allowed transitions) live in application code, not here —
MongoDB schema validation does not implement those relationships.
"""
from __future__ import annotations

from pymongo import ASCENDING
from pymongo.database import Database
from pymongo.errors import CollectionInvalid

from common.db import COLLECTIONS, get_db

# Minimal $jsonSchema validators: required identifiers, schema versions, and
# Date fields. Kept lenient (moderate level) so controller logic owns the richer
# cross-document rules.
VALIDATORS: dict[str, dict] = {
    "projects": {"required": ["_id", "schema_version"]},
    "tasks": {"required": ["_id", "schema_version", "project_id", "status"]},
    "delegations": {"required": ["_id", "schema_version", "project_id", "task_id", "assignment_key"]},
    "worker_runs": {"required": ["_id", "schema_version", "project_id", "task_id", "delegation_id", "status"]},
    "artifacts": {"required": ["_id", "schema_version", "project_id", "kind", "tree_hash"]},
    "validation_runs": {"required": ["_id", "schema_version", "project_id", "kind"]},
    "harness_versions": {"required": ["_id", "schema_version", "project_id", "version", "config_digest"]},
    "evolution_runs": {"required": ["_id", "schema_version", "project_id", "status", "baseline_harness_version_id"]},
    "integration_runs": {"required": ["_id", "schema_version", "project_id", "task_id", "status"]},
    "jobs": {"required": ["_id", "schema_version", "project_id", "kind", "status", "dedupe_key"]},
    "events": {"required": ["_id", "schema_version", "project_id", "sequence", "type"]},
}


def _json_schema(required: list[str]) -> dict:
    return {
        "$jsonSchema": {
            "bsonType": "object",
            "required": required,
            "properties": {"schema_version": {"bsonType": "int"}},
        }
    }


def ensure_collections(db: Database | None = None) -> None:
    db = get_db() if db is None else db
    for name in COLLECTIONS:
        validator = _json_schema(VALIDATORS[name]["required"])
        try:
            db.create_collection(name, validator=validator, validationLevel="moderate")
        except CollectionInvalid:
            # Already exists: update the validator in place.
            db.command({"collMod": name, "validator": validator, "validationLevel": "moderate"})


def ensure_indexes(db: Database | None = None) -> None:
    db = get_db() if db is None else db

    db.projects.create_index(
        [("repository.registered_root_id", ASCENDING)],
        unique=True,
        partialFilterExpression={"repository.registered_root_id": {"$exists": True}},
        name="uniq_registered_root",
    )

    db.tasks.create_index([("project_id", 1), ("status", 1), ("created_at", 1)], name="by_project_status")
    db.tasks.create_index([("parent_session_id", 1), ("status", 1)], name="by_parent_session")

    db.delegations.create_index([("task_id", 1), ("assignment_key", 1)], unique=True, name="uniq_assignment")
    db.delegations.create_index([("task_id", 1), ("status", 1)], name="by_task_status")

    db.worker_runs.create_index([("delegation_id", 1), ("attempt", 1)], unique=True, name="uniq_attempt")
    db.worker_runs.create_index([("task_id", 1), ("status", 1)], name="by_task_status")

    db.artifacts.create_index([("project_id", 1), ("tree_hash", 1)], name="by_tree_hash")
    db.artifacts.create_index([("task_id", 1), ("kind", 1)], name="by_task_kind")

    db.validation_runs.create_index([("task_id", 1), ("created_at", 1)], name="by_task_created")
    # Exact-input cache key for completed runs (spec section 8): not just a diff
    # hash — tree, baseline, harness, suite, contract, runner image, seed, epoch.
    db.validation_runs.create_index(
        [("inputs.cache_key", 1)],
        unique=True,
        partialFilterExpression={"inputs.cache_key": {"$exists": True}},
        name="uniq_input_cache",
    )

    db.harness_versions.create_index([("project_id", 1), ("version", 1)], unique=True, name="uniq_version")
    db.harness_versions.create_index([("project_id", 1), ("parent_id", 1)], name="by_parent")

    db.evolution_runs.create_index([("project_id", 1), ("status", 1)], name="by_project_status")
    db.integration_runs.create_index([("task_id", 1), ("status", 1)], name="by_task_status")

    db.jobs.create_index([("dedupe_key", 1)], unique=True, name="uniq_dedupe")
    db.jobs.create_index([("status", 1), ("available_at", 1)], name="by_ready")
    db.jobs.create_index([("status", 1), ("lease.expires_at", 1)], name="by_lease_expiry")

    db.events.create_index([("task_id", 1), ("sequence", 1)], unique=True, name="uniq_task_seq")
    db.events.create_index(
        [("project_id", 1), ("actor_id", 1), ("source_event_id", 1)],
        unique=True,
        partialFilterExpression={"source_event_id": {"$exists": True}},
        name="uniq_source_event",
    )


def setup(db: Database | None = None) -> None:
    db = get_db() if db is None else db
    ensure_collections(db)
    ensure_indexes(db)


if __name__ == "__main__":
    setup()
    print("airlock schema ready")
