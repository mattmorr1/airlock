from collections.abc import MutableMapping
from datetime import UTC, datetime
from functools import reduce
from typing import Any

import pytest
from pymongo.asynchronous.database import AsyncDatabase
from pymongo.errors import WriteError

from controller.db import Doc
from controller.states import (
    DelegationState,
    EvolutionRunState,
    IntegrationRunState,
    JobKind,
    JobStatus,
    TaskState,
    ValidationDecision,
    WorkerRunState,
    new_id,
)
from controller.states import Prefix as P
from mongo.setup import COLLECTIONS, setup

pytestmark = pytest.mark.anyio
MISSING = object()


def valid(name: str) -> Doc:
    proj, task, now = new_id(P.PROJECT), new_id(P.TASK), datetime.now(UTC)
    owned = {"project_id": proj, "task_id": task}
    docs: dict[str, Doc] = {
        "projects": {"_id": proj, "created_at": now, "event_seq": 0, "validated_harness_ids": [new_id(P.HARNESS_VERSION)]},
        "tasks": {"_id": task, "project_id": proj, "status": TaskState.DRAFT, "event_seq": 0,
                  "approval": {"actor_id": "op", "approved_at": now}, "created_at": now},
        "delegations": {"_id": new_id(P.DELEGATION), **owned, "assignment_key": "service", "status": DelegationState.RUNNING, "attempt": 1},
        "worker_runs": {"_id": new_id(P.WORKER_RUN), **owned, "delegation_id": new_id(P.DELEGATION), "attempt": 1, "status": WorkerRunState.QUEUED,
                        "started_at": now, "finished_at": None},
        "artifacts": {"_id": new_id(P.ARTIFACT), **owned, "kind": "code_snapshot", "created_at": now},
        "validation_runs": {"_id": new_id(P.VALIDATION_RUN), **owned, "decision": ValidationDecision.PASS, "input_digest": "sha256:x", "created_at": now},
        "harness_versions": {"_id": new_id(P.HARNESS_VERSION), "project_id": proj, "version": 1, "created_at": now},
        "evolution_runs": {"_id": new_id(P.EVOLUTION_RUN), "project_id": proj, "status": EvolutionRunState.PROPOSED, "created_at": now},
        "integration_runs": {"_id": new_id(P.INTEGRATION_RUN), **owned, "status": IntegrationRunState.COMPOSING},
        "jobs": {"_id": new_id(P.JOB), "project_id": proj, "kind": JobKind.PLAN, "status": JobStatus.QUEUED, "dedupe_key": new_id(P.JOB),
                 "available_at": now, "lease": {"owner": None, "expires_at": None, "fence": 0}, "created_at": now},
        "events": {"_id": new_id(P.EVENT), **owned, "stream_id": task, "sequence": 1, "type": "task.created", "actor_id": "op",
                   "source_event_id": new_id(P.EVENT), "created_at": now},
    }
    return {"schema_version": 1, "extra": {"anything": [1, "two"]}} | docs[name]


def mutate(doc: Doc, path: str, value: object) -> Doc:
    *parents, leaf = path.split(".")
    target: Doc = reduce(lambda d, k: d[k], parents, doc)
    if value is MISSING:
        del target[leaf]
    else:
        target[leaf] = value
    return doc


DATE_FIELDS = [(c, "created_at") for c in COLLECTIONS if "created_at" in valid(c)] + [
    ("tasks", "approval.approved_at"), ("worker_runs", "started_at"), ("worker_runs", "finished_at"), ("jobs", "available_at"), ("jobs", "lease.expires_at")]
ENUM_FIELDS = [("tasks", "status"), ("delegations", "status"), ("worker_runs", "status"), ("integration_runs", "status"), ("evolution_runs", "status"),
               ("jobs", "status"), ("jobs", "kind"), ("validation_runs", "decision")]
REJECTED: list[tuple[str, str, Any]] = [
    *((c, "schema_version", MISSING) for c in COLLECTIONS),
    *((c, f, "bogus") for c, f in ENUM_FIELDS),
    *((c, f, "2026-10-11T19:30:00Z") for c, f in DATE_FIELDS),
    *((c, "_id", "wrong_prefix") for c in COLLECTIONS),
    ("delegations", "task_id", MISSING), ("worker_runs", "delegation_id", MISSING), ("events", "stream_id", new_id(P.DELEGATION)),
    ("jobs", "dedupe_key", MISSING), ("events", "sequence", "1"),
]

EXPECTED_INDEXES: dict[str, set[tuple[tuple[str, ...], bool, str | None]]] = {
    "projects": {(("repository.registered_root_id",), True, "repository.registered_root_id")},
    "tasks": {(("project_id", "status", "created_at"), False, None), (("parent_session_id", "status"), False, None)},
    "delegations": {(("task_id", "assignment_key"), True, None), (("task_id", "status"), False, None)},
    "worker_runs": {(("delegation_id", "attempt"), True, None), (("task_id", "status"), False, None)},
    "artifacts": {(("project_id", "tree_hash"), False, None), (("task_id", "kind"), False, None)},
    "validation_runs": {(("task_id", "created_at"), False, None), (("input_digest", "status"), False, None)},
    "harness_versions": {(("project_id", "version"), True, None), (("project_id", "parent_id"), False, None)},
    "evolution_runs": {(("project_id", "status"), False, None)},
    "integration_runs": {(("task_id", "status"), False, None)},
    "jobs": {(("dedupe_key",), True, None), (("status", "available_at"), False, None), (("status", "lease.expires_at"), False, None)},
    "events": {(("stream_id", "sequence"), True, None), (("project_id", "actor_id", "source_event_id"), True, "source_event_id")},
}


async def snapshot(db: AsyncDatabase[Doc]) -> dict[str, tuple[MutableMapping[str, Any], MutableMapping[str, Any]]]:
    return {c: (await db[c].options(), await db[c].index_information()) for c in sorted(await db.list_collection_names())}


async def test_throwaway_database(db: AsyncDatabase[Doc]) -> None:
    assert db.name.startswith("airlock_test_")


async def test_setup_is_idempotent(db: AsyncDatabase[Doc]) -> None:
    before = await snapshot(db)
    await setup(db)
    assert await snapshot(db) == before
    assert set(before) == set(COLLECTIONS)
    assert all(o["validationLevel"] == "strict" and o["validationAction"] == "error" for o, _ in before.values())


@pytest.mark.parametrize("name", COLLECTIONS)
async def test_valid_document_with_extra_fields_accepted(db: AsyncDatabase[Doc], name: str) -> None:
    doc = valid(name)
    await db[name].insert_one(doc)
    assert (await db[name].find_one({"_id": doc["_id"]}) or {})["extra"] == {"anything": [1, "two"]}


@pytest.mark.parametrize(("name", "path", "value"), REJECTED, ids=lambda v: "missing" if v is MISSING else str(v))
async def test_invalid_document_rejected(db: AsyncDatabase[Doc], name: str, path: str, value: object) -> None:
    with pytest.raises(WriteError) as e:
        await db[name].insert_one(mutate(valid(name), path, value))
    assert e.value.code == 121


async def test_every_spec_index_exists(db: AsyncDatabase[Doc]) -> None:
    actual = {
        c: {(tuple(f for f, _ in i["key"]), bool(i.get("unique")), next(iter(i.get("partialFilterExpression", {})), None))
            for n, i in info.items() if n != "_id_"}
        for c, (_, info) in (await snapshot(db)).items()
    }
    assert actual == EXPECTED_INDEXES
