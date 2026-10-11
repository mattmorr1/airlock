"""Harness versions: immutable executable configuration (spec section 7.7).

Harness versions carry no mutable `active` flag — the project pointer selects
the active version (Person A owns that pointer). Static controller code
interprets the config fields. An inspection name must map to a real implemented
capability; we validate that here against the runner's registry.
"""
from __future__ import annotations

import json

from common.db import col
from common.hashing import sha256_hex
from common.ids import new_id, utcnow
from runner.bulk_inspectors import INSPECTORS as _BULK_INSPECTORS
from runner.inspectors import INSPECTORS as _LOGIN_INSPECTORS

# An inspection name must map to a real implemented capability in one of the
# runner registries. The `runner` field on a config selects which one runs.
_ALL_INSPECTORS = set(_LOGIN_INSPECTORS) | set(_BULK_INSPECTORS)

# Login-demo baseline harness: success-path only, no log inspection.
V1_CONFIG = {
    "runner": "login",
    "delegation": {"isolated_coding": True, "max_parallel_workers": 2},
    "context": {"include_patterns": ["src/auth/**", "db/migrations/**"], "max_bytes": 50000},
    "tools": {"profile_key": "bounded-auth-worker", "publish": "deny"},
    "validation": {
        "inspections": ["http_responses", "database_state"],
        "scenario_artifact_ids": [],
        "required_tags": ["happy_path"],
    },
}

# Bulk-demo baseline harness: valid batches + status codes only. This is the
# harness the partial write gets past — as naive as the agent's own tests.
BULK_V1_CONFIG = {
    "runner": "bulk",
    "delegation": {"isolated_coding": True, "max_parallel_workers": 3},
    "context": {"include_patterns": ["app/**"], "max_bytes": 50000},
    "tools": {"profile_key": "bounded-items-worker", "publish": "deny"},
    "validation": {
        "inspections": ["http_responses"],
        "scenario_artifact_ids": [],
        "required_tags": ["happy_path"],
    },
}


def config_digest(config: dict) -> str:
    return "sha256:" + sha256_hex(json.dumps(config, sort_keys=True).encode("utf-8"))


def _validate_config(config: dict) -> None:
    inspections = config.get("validation", {}).get("inspections", [])
    unknown = [i for i in inspections if i not in _ALL_INSPECTORS]
    if unknown:
        raise ValueError(f"config names unimplemented inspectors: {unknown}")


def next_version(project_id: str) -> int:
    last = col("harness_versions").find_one(
        {"project_id": project_id}, sort=[("version", -1)], projection={"version": 1}
    )
    return (last["version"] + 1) if last else 1


def create_harness_version(
    *,
    project_id: str,
    config: dict,
    version: int | None = None,
    parent_id: str | None = None,
    created_by_evolution_run_id: str | None = None,
    scope: dict | None = None,
) -> dict:
    _validate_config(config)
    version = version or next_version(project_id)
    digest = config_digest(config)

    existing = col("harness_versions").find_one({"project_id": project_id, "version": version})
    if existing is not None:
        return existing

    doc = {
        "_id": new_id("hv"),
        "schema_version": 1,
        "project_id": project_id,
        "version": version,
        "parent_id": parent_id,
        "config_digest": digest,
        "created_by_evolution_run_id": created_by_evolution_run_id,
        "scope": scope or {"task_family": "authentication", "runner_language": "python"},
        "config": config,
        "created_at": utcnow(),
    }
    col("harness_versions").insert_one(doc)
    return doc


def get_config(harness_version_id: str) -> dict:
    doc = col("harness_versions").find_one({"_id": harness_version_id})
    if doc is None:
        raise KeyError(f"unknown harness version {harness_version_id!r}")
    return doc["config"]


def bootstrap_v1(project_id: str) -> dict:
    """Create (idempotently) the login-demo baseline harness v1."""
    return create_harness_version(project_id=project_id, config=V1_CONFIG, version=1)


def bootstrap_bulk_v1(project_id: str) -> dict:
    """Create (idempotently) the bulk-demo baseline harness v1."""
    return create_harness_version(project_id=project_id, config=BULK_V1_CONFIG, version=1)
