import asyncio
from enum import StrEnum

from pymongo import ASCENDING, IndexModel
from pymongo.asynchronous.database import AsyncDatabase

from controller import db
from controller.db import Doc
from controller.states import (
    DelegationState,
    EvolutionRunState,
    IntegrationRunState,
    JobKind,
    JobStatus,
    Prefix,
    TaskState,
    ValidationDecision,
    WorkerRunState,
)

DATE: Doc = {"bsonType": "date"}
OPT_DATE: Doc = {"bsonType": ["date", "null"]}
INT: Doc = {"bsonType": ["int", "long"], "minimum": 0}
STR: Doc = {"bsonType": "string"}
STRICT: Doc = {"validationLevel": "strict", "validationAction": "error"}


def ref(*prefixes: Prefix) -> Doc:
    return {"bsonType": "string", "pattern": f"^({'|'.join(prefixes)})_"}


def enum(e: type[StrEnum], *extra: None) -> Doc:
    return {"enum": [*(m.value for m in e), *extra]}


def schema(prefix: Prefix, required: Doc, optional: Doc | None = None) -> Doc:
    props = {"_id": ref(prefix), "schema_version": {"bsonType": ["int", "long"], "minimum": 1}, **required}
    return {"$jsonSchema": {"bsonType": "object", "required": ["_id", "schema_version", *required], "properties": props | (optional or {})}}


def ix(*fields: str, **opts: object) -> IndexModel:
    return IndexModel([(f, ASCENDING) for f in fields], **opts)


def exists(field: str) -> Doc:
    return {field: {"$exists": True}}


P = Prefix
PROJ, TASK = ref(P.PROJECT), ref(P.TASK)

COLLECTIONS: dict[str, tuple[Doc, list[IndexModel]]] = {
    "projects": (
        schema(P.PROJECT, {"created_at": DATE}, {"event_seq": INT, "validated_harness_ids": {"bsonType": "array", "items": ref(P.HARNESS_VERSION)}}),
        [ix("repository.registered_root_id", unique=True, partialFilterExpression=exists("repository.registered_root_id"))],
    ),
    "tasks": (
        schema(P.TASK, {"project_id": PROJ, "status": enum(TaskState), "created_at": DATE},
               {"event_seq": INT, "approval": {"bsonType": ["object", "null"], "properties": {"approved_at": DATE}}}),
        [ix("project_id", "status", "created_at"), ix("parent_session_id", "status")],
    ),
    "delegations": (
        schema(P.DELEGATION, {"project_id": PROJ, "task_id": TASK, "assignment_key": STR, "status": enum(DelegationState), "attempt": INT}),
        [ix("task_id", "assignment_key", unique=True), ix("task_id", "status")],
    ),
    "worker_runs": (
        schema(P.WORKER_RUN, {"project_id": PROJ, "task_id": TASK, "delegation_id": ref(P.DELEGATION), "attempt": INT,
                              "status": enum(WorkerRunState)}, {"started_at": OPT_DATE, "finished_at": OPT_DATE}),
        [ix("delegation_id", "attempt", unique=True), ix("task_id", "status")],
    ),
    "artifacts": (
        schema(P.ARTIFACT, {"project_id": PROJ, "kind": STR, "created_at": DATE}),
        [ix("project_id", "tree_hash"), ix("task_id", "kind")],
    ),
    "validation_runs": (
        schema(P.VALIDATION_RUN, {"project_id": PROJ, "created_at": DATE}, {"decision": enum(ValidationDecision, None), "input_digest": STR}),
        [ix("task_id", "created_at"), ix("input_digest", "status"),
         ix("inputs.cache_key", unique=True, partialFilterExpression=exists("inputs.cache_key"))],
    ),
    "harness_versions": (
        schema(P.HARNESS_VERSION, {"project_id": PROJ, "version": INT, "created_at": DATE}),
        [ix("project_id", "version", unique=True), ix("project_id", "parent_id")],
    ),
    "evolution_runs": (
        schema(P.EVOLUTION_RUN, {"project_id": PROJ, "status": enum(EvolutionRunState), "created_at": DATE}),
        [ix("project_id", "status")],
    ),
    "integration_runs": (
        schema(P.INTEGRATION_RUN, {"project_id": PROJ, "task_id": TASK, "status": enum(IntegrationRunState)}),
        [ix("task_id", "status")],
    ),
    "jobs": (
        schema(P.JOB, {"kind": enum(JobKind), "status": enum(JobStatus), "dedupe_key": STR, "available_at": DATE, "created_at": DATE},
               {"lease": {"bsonType": "object", "properties": {"expires_at": OPT_DATE}}}),
        [ix("dedupe_key", unique=True), ix("status", "available_at"), ix("status", "lease.expires_at")],
    ),
    "events": (
        schema(P.EVENT, {"project_id": PROJ, "stream_id": ref(P.TASK, P.PROJECT), "sequence": INT, "type": STR, "created_at": DATE}),
        [ix("stream_id", "sequence", unique=True),
         ix("project_id", "actor_id", "source_event_id", unique=True, partialFilterExpression=exists("source_event_id"))],
    ),
}


async def _apply(database: AsyncDatabase[Doc], name: str, validator: Doc, indexes: list[IndexModel], present: bool) -> None:
    if present:
        await database.command("collMod", name, validator=validator, **STRICT)
    else:
        await database.create_collection(name, validator=validator, **STRICT)
    await database[name].create_indexes(indexes)


async def setup(database: AsyncDatabase[Doc]) -> None:
    existing = set(await database.list_collection_names())
    async with asyncio.TaskGroup() as tg:
        for name, (validator, indexes) in COLLECTIONS.items():
            tg.create_task(_apply(database, name, validator, indexes, name in existing))


async def main() -> None:
    async with db.client() as c:
        await setup(db.database(c))


if __name__ == "__main__":
    asyncio.run(main())
