from enum import StrEnum

from bson import ObjectId


class TaskState(StrEnum):
    DRAFT = "draft"
    PLANNED = "planned"
    APPROVED = "approved"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class DelegationState(StrEnum):
    RUNNING = "running"
    VALIDATING = "validating"
    INTEGRATING = "integrating"
    ACCEPTED = "accepted"
    REPAIR_REQUIRED = "repair_required"
    FAILED = "failed"
    CANCELLED = "cancelled"


class WorkerRunState(StrEnum):
    QUEUED = "queued"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    SNAPSHOTTING = "snapshotting"
    FINISHED = "finished"
    FAILED = "failed"
    CANCELLED = "cancelled"


class IntegrationRunState(StrEnum):
    COMPOSING = "composing"
    VALIDATING = "validating"
    ACCEPTED = "accepted"
    CONFLICTED = "conflicted"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ValidationDecision(StrEnum):
    PASS = "pass"
    REPAIR_REQUIRED = "repair_required"
    REVIEW_REQUIRED = "review_required"
    INCONCLUSIVE = "inconclusive"


class EvolutionRunState(StrEnum):
    PROPOSED = "proposed"
    GENERATING = "generating"
    EVALUATING = "evaluating"
    ELIGIBLE = "eligible"
    PROMOTED = "promoted"
    REJECTED = "rejected"
    FAILED = "failed"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class JobKind(StrEnum):
    PLAN = "plan"
    LAUNCH = "launch"
    VALIDATE = "validate"
    INTEGRATE = "integrate"
    EVOLVE = "evolve"
    COMMAND_RECEIPT = "command_receipt"


class Prefix(StrEnum):
    PROJECT = "proj"
    TASK = "task"
    DELEGATION = "del"
    WORKER_RUN = "worker"
    ARTIFACT = "art"
    VALIDATION_RUN = "val"
    HARNESS_VERSION = "hv"
    EVOLUTION_RUN = "evo"
    INTEGRATION_RUN = "int"
    JOB = "job"
    EVENT = "evt"


SCHEMA_VERSION = 1


def new_id(prefix: Prefix) -> str:
    return f"{prefix}_{ObjectId()}"
