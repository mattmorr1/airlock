"""State enums and vocabularies shared across collections (spec section 7)."""
from __future__ import annotations

from enum import Enum


class StrEnum(str, Enum):
    def __str__(self) -> str:  # make json/log output the bare value
        return self.value


class TaskState(StrEnum):
    DRAFT = "draft"
    PLANNED = "planned"
    APPROVED = "approved"
    RUNNING = "running"
    AWAITING_INTEGRATION = "awaiting_integration"
    VALIDATING = "validating"
    REPAIR_REQUIRED = "repair_required"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class WorkerState(StrEnum):
    QUEUED = "queued"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    SNAPSHOTTING = "snapshotting"
    FINISHED = "finished"  # output frozen, NOT accepted
    FAILED = "failed"
    CANCELLED = "cancelled"


class ArtifactKind(StrEnum):
    BASELINE = "baseline"
    CODE_SNAPSHOT = "code_snapshot"
    INTEGRATED = "integrated"
    SCENARIO = "scenario"
    TRACE = "trace"


class CheckStatus(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    ERROR = "error"
    SKIPPED = "skipped"


class ValidationDecision(StrEnum):
    PASS = "pass"
    REPAIR_REQUIRED = "repair_required"
    REVIEW_REQUIRED = "review_required"
    # timeout / observer failure / missing scenarios => inconclusive, never pass
    INCONCLUSIVE = "inconclusive"


class ValidationKind(StrEnum):
    CANDIDATE = "candidate"
    INTEGRATION = "integration"
    HOLDOUT = "holdout"
    PARTIAL = "partial"


class EvolutionState(StrEnum):
    PROPOSED = "proposed"
    GENERATING = "generating"
    EVALUATING = "evaluating"
    ELIGIBLE = "eligible"
    PROMOTED = "promoted"
    REJECTED = "rejected"
    FAILED = "failed"


class IntegrationState(StrEnum):
    QUEUED = "queued"
    COMPOSING = "composing"
    VALIDATING = "validating"
    COMPLETED = "completed"
    CONFLICT = "conflict"
    FAILED = "failed"


class JobKind(StrEnum):
    LAUNCH = "launch"
    VALIDATE = "validate"
    INTEGRATE = "integrate"
    EVOLVE = "evolve"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


# Inspectors that are actually implemented (spec section 7.7: an inspection name
# must correspond to a real implemented capability).
IMPLEMENTED_INSPECTORS = ("http_responses", "database_state", "sensitive_logs")
