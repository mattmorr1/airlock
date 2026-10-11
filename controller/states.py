from datetime import UTC, datetime
from enum import StrEnum

from bson import ObjectId
from pymongo import ReturnDocument
from pymongo.asynchronous.client_session import AsyncClientSession
from pymongo.asynchronous.collection import AsyncCollection

from controller.db import Doc


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


class Entity(StrEnum):
    TASK = "tasks"
    DELEGATION = "delegations"
    WORKER_RUN = "worker_runs"
    INTEGRATION_RUN = "integration_runs"
    EVOLUTION_RUN = "evolution_runs"
    JOB = "jobs"


type State = TaskState | DelegationState | WorkerRunState | IntegrationRunState | EvolutionRunState | JobStatus
TS, DS, WS, IS, ES, JS = TaskState, DelegationState, WorkerRunState, IntegrationRunState, EvolutionRunState, JobStatus

ALLOWED: dict[Entity, dict[State, frozenset[State]]] = {
    Entity.TASK: {TS.DRAFT: frozenset({TS.PLANNED, TS.FAILED, TS.CANCELLED}), TS.PLANNED: frozenset({TS.APPROVED, TS.CANCELLED}),
                  TS.APPROVED: frozenset({TS.RUNNING, TS.CANCELLED}), TS.RUNNING: frozenset({TS.COMPLETED, TS.FAILED, TS.CANCELLED})},
    Entity.DELEGATION: {DS.RUNNING: frozenset({DS.VALIDATING, DS.FAILED, DS.CANCELLED}),
                        DS.VALIDATING: frozenset({DS.INTEGRATING, DS.REPAIR_REQUIRED, DS.FAILED, DS.CANCELLED}),
                        DS.INTEGRATING: frozenset({DS.ACCEPTED, DS.REPAIR_REQUIRED, DS.FAILED, DS.CANCELLED}),
                        DS.REPAIR_REQUIRED: frozenset({DS.RUNNING, DS.FAILED, DS.CANCELLED})},
    Entity.WORKER_RUN: {WS.QUEUED: frozenset({WS.STARTING, WS.FAILED, WS.CANCELLED}), WS.STARTING: frozenset({WS.RUNNING, WS.FAILED, WS.CANCELLED}),
                        WS.RUNNING: frozenset({WS.STOPPING, WS.SNAPSHOTTING, WS.FAILED, WS.CANCELLED}),
                        WS.STOPPING: frozenset({WS.SNAPSHOTTING, WS.FAILED, WS.CANCELLED}), WS.SNAPSHOTTING: frozenset({WS.FINISHED, WS.FAILED})},
    Entity.INTEGRATION_RUN: {IS.COMPOSING: frozenset({IS.VALIDATING, IS.CONFLICTED, IS.FAILED, IS.CANCELLED}),
                             IS.VALIDATING: frozenset({IS.ACCEPTED, IS.FAILED, IS.CANCELLED})},
    Entity.EVOLUTION_RUN: {ES.PROPOSED: frozenset({ES.GENERATING, ES.REJECTED, ES.FAILED}), ES.GENERATING: frozenset({ES.EVALUATING, ES.FAILED}),
                           ES.EVALUATING: frozenset({ES.ELIGIBLE, ES.REJECTED, ES.FAILED}), ES.ELIGIBLE: frozenset({ES.PROMOTED, ES.REJECTED})},
    # running -> running is a claim-time reclaim of an expired lease; running -> queued is fail() with retries left.
    Entity.JOB: {JS.QUEUED: frozenset({JS.RUNNING}), JS.RUNNING: frozenset({JS.RUNNING, JS.QUEUED, JS.COMPLETED, JS.FAILED})},
}


class Conflict(Exception):
    def __init__(self, entity: Entity, id_: str, revision: int, to: State) -> None:
        super().__init__(f"{entity} {id_}: no document at revision {revision} in a state that may move to {to}")
        self.entity, self.id, self.revision = entity, id_, revision


class IllegalTransition(ValueError):
    pass


async def transition(session: AsyncClientSession, coll: AsyncCollection[Doc], id_: str, revision: int, to: State, set_: Doc | None = None) -> None:
    entity = Entity(coll.name)
    if not (sources := [s for s, tos in ALLOWED[entity].items() if to in tos]):
        raise IllegalTransition(f"no {entity} state may move to {to}")
    res = await coll.update_one({"_id": id_, "revision": revision, "status": {"$in": sources}},
                                {"$set": {**(set_ or {}), "status": to}, "$inc": {"revision": 1}}, session=session)
    if res.matched_count == 0:
        raise Conflict(entity, id_, revision, to)


async def append_event(session: AsyncClientSession, stream_id: str, owner_coll: AsyncCollection[Doc], type_: str, actor_id: str, payload: Doc) -> Doc:
    owner = await owner_coll.find_one_and_update({"_id": stream_id}, {"$inc": {"event_seq": 1}}, {"event_seq": 1, "project_id": 1},
                                                 return_document=ReturnDocument.AFTER, session=session)
    if owner is None:
        raise LookupError(f"{owner_coll.name} {stream_id} not found")
    event: Doc = {"_id": new_id(Prefix.EVENT), "schema_version": SCHEMA_VERSION, "project_id": owner.get("project_id", stream_id),
                  "task_id": stream_id if owner_coll.name == Entity.TASK else None, "stream_id": stream_id, "sequence": owner["event_seq"],
                  "type": type_, "actor_id": actor_id, "payload": payload, "created_at": datetime.now(UTC)}
    await owner_coll.database["events"].insert_one(event, session=session)
    return event


def accept_validation(decision: ValidationDecision, worker_run: Doc, project: Doc) -> ValidationDecision:
    return ValidationDecision.INCONCLUSIVE if worker_run["constraint_epoch"] != project["constraint_epoch"] else decision
