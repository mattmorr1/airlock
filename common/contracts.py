"""Pydantic document contracts — the frozen interface across the jobs queue.

These mirror the JSON documents in spec section 7. ``extra="forbid"`` enforces
the spec rule "Use Pydantic to reject unknown fields on API contracts". Person
A writes `tasks`/`delegations`/`jobs`/`events`; Person B writes the rest. Both
agree on these shapes and never on each other's internals.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .enums import (
    ArtifactKind,
    CheckStatus,
    EvolutionState,
    IntegrationState,
    JobKind,
    JobStatus,
    ValidationDecision,
    ValidationKind,
    WorkerState,
)
from .ids import utcnow


class Doc(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True)
    schema_version: int = 1


# --------------------------------------------------------------------------- #
# Jobs queue payloads (the async boundary between A and B)                      #
# --------------------------------------------------------------------------- #
class LaunchPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    worker_run_id: str
    delegation_id: str
    task_id: str
    project_id: str
    base_artifact_id: str
    harness_version_id: str
    contract_version: int
    constraint_epoch: int
    objective: str
    file_scope: list[str]
    model_requested: str = "sonnet"
    context_manifest: list[str] = Field(default_factory=list)
    max_turns: int = 20
    max_runtime_seconds: int = 600


class ValidatePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    validation_run_id: str


class IntegratePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    integration_run_id: str


class EvolvePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    evolution_run_id: str


class JobDoc(Doc):
    id: str = Field(alias="_id")
    project_id: str
    task_id: str | None = None
    kind: JobKind
    payload: dict[str, Any]
    status: JobStatus = JobStatus.QUEUED
    dedupe_key: str
    available_at: datetime = Field(default_factory=utcnow)
    attempts: int = 0
    lease: dict[str, Any] = Field(default_factory=lambda: {"owner": None, "expires_at": None, "fence": 0})
    result_ref: dict[str, Any] | None = None
    created_at: datetime = Field(default_factory=utcnow)


# --------------------------------------------------------------------------- #
# Artifacts (Person B writes; immutable)                                        #
# --------------------------------------------------------------------------- #
class ArtifactDoc(Doc):
    id: str = Field(alias="_id")
    project_id: str
    task_id: str | None = None
    worker_run_id: str | None = None
    kind: ArtifactKind
    base_artifact_id: str | None = None
    tree_hash: str
    patch_hash: str | None = None
    controller_commit_sha: str | None = None
    changed_files: list[str] = Field(default_factory=list)
    storage: dict[str, Any]
    size_bytes: int = 0
    created_by: str
    created_at: datetime = Field(default_factory=utcnow)


# --------------------------------------------------------------------------- #
# Worker runs (Person B writes)                                                 #
# --------------------------------------------------------------------------- #
class WorkerRunDoc(Doc):
    id: str = Field(alias="_id")
    project_id: str
    task_id: str
    delegation_id: str
    attempt: int = 1
    status: WorkerState = WorkerState.QUEUED
    revision: int = 1
    execution: dict[str, Any]
    claude_session_id: str | None = None
    input_artifact_id: str
    harness_version_id: str
    constraint_epoch: int
    model_requested: str = "sonnet"
    context_manifest: list[str] = Field(default_factory=list)
    output_artifact_id: str | None = None
    budget: dict[str, Any] = Field(default_factory=dict)
    metrics: dict[str, Any] = Field(default_factory=dict)
    started_at: datetime | None = None
    finished_at: datetime | None = None


# --------------------------------------------------------------------------- #
# Validation runs (Person B writes)                                             #
# --------------------------------------------------------------------------- #
class Check(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True)
    id: str
    status: CheckStatus
    detail: str | None = None
    evidence_artifact_id: str | None = None


class ValidationRunDoc(Doc):
    id: str = Field(alias="_id")
    project_id: str
    task_id: str | None = None
    kind: ValidationKind
    status: str = "queued"
    inputs: dict[str, Any]
    checks: list[Check] = Field(default_factory=list)
    behavior_delta: dict[str, Any] = Field(default_factory=dict)
    decision: ValidationDecision | None = None
    coverage: dict[str, Any] = Field(default_factory=dict)
    observer_ok: bool = True
    elapsed_ms: int = 0
    created_at: datetime = Field(default_factory=utcnow)


# --------------------------------------------------------------------------- #
# Integration runs (Person B writes)                                            #
# --------------------------------------------------------------------------- #
class IntegrationRunDoc(Doc):
    id: str = Field(alias="_id")
    project_id: str
    task_id: str
    status: IntegrationState = IntegrationState.QUEUED
    revision: int = 1
    base_artifact_id: str
    candidate_artifact_ids: list[str]
    composition_order: list[str] = Field(default_factory=list)
    result_artifact_id: str | None = None
    validation_run_id: str | None = None
    integration_fence: int = 0
    conflicts: list[dict[str, Any]] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)


# --------------------------------------------------------------------------- #
# Harness versions (Person B writes candidates; Person A flips the pointer)     #
# --------------------------------------------------------------------------- #
class HarnessVersionDoc(Doc):
    id: str = Field(alias="_id")
    project_id: str
    version: int
    parent_id: str | None = None
    config_digest: str
    created_by_evolution_run_id: str | None = None
    scope: dict[str, Any] = Field(default_factory=dict)
    config: dict[str, Any]
    created_at: datetime = Field(default_factory=utcnow)


# --------------------------------------------------------------------------- #
# Evolution runs (Person B writes evidence/metrics; A promotes)                 #
# --------------------------------------------------------------------------- #
class EvolutionRunDoc(Doc):
    id: str = Field(alias="_id")
    project_id: str
    trigger_validation_run_id: str | None = None
    status: EvolutionState = EvolutionState.PROPOSED
    revision: int = 1
    baseline_harness_version_id: str
    candidate_harness_version_id: str | None = None
    hypothesis: str = ""
    changes: list[str] = Field(default_factory=list)
    evaluation: dict[str, Any] = Field(default_factory=dict)
    promotion: dict[str, Any] | None = None
    created_at: datetime = Field(default_factory=utcnow)
